## Purpose

This specification defines the per-host orchestrator configuration model — the `HostConfig`/`HostSettings` Pydantic model sourced from the root-owned per-operator setup marker (`HostConfig.from_marker(operator)`), and the centralized machinectl command prefix builder that consumes it. There is no `sandbox-ai.toml` file or loader — host provisioning facts are authored by `sudo sandbox setup` into the marker and read back at runtime/doctor.
## Requirements
### Requirement: Pydantic Model Structure

The host config model SHALL use a nested `HostSettings` model for the per-operator host facts. `HostSettings` SHALL carry `docker_unprivileged_user` (`str | None` — `None` in `operator-rootless`, a valid POSIX username in `separate-user`), `docker_execution_mode` (`DockerExecutionMode`), `workspace_bridge_group` (string), and `workspace_bridge_gid` (int). There is no authentication field and no `MachinectlAuth` enum.

Because `docker_unprivileged_user` becomes optional, the model SHALL enforce its presence **conditionally**: a model-level validator SHALL require a valid `docker_unprivileged_user` when `docker_execution_mode == separate-user`, and SHALL permit `None` when `operator-rootless` (the existing POSIX-username grammar still applies when a value is present). The single owner-resolver `resolve_daemon_owner_settings` SHALL keep its `-> str` contract: its `separate-user` branch SHALL narrow the now-optional field and raise a clear error if a `separate-user` config somehow lacks the daemon user, rather than returning `None` (preserving mypy/pyright-strict).

#### Scenario: HostSettings carries the marker-sourced facts

- **WHEN** a `HostConfig` is built via `from_marker`
- **THEN** `host_config.host` is a `HostSettings` instance exposing `docker_unprivileged_user`, `docker_execution_mode`, `workspace_bridge_group`, and `workspace_bridge_gid`, with no `machinectl_authentication` attribute

#### Scenario: docker_unprivileged_user required only in separate-user

- **WHEN** a `HostSettings` is constructed with `docker_execution_mode == separate-user` and `docker_unprivileged_user is None`
- **THEN** the model validator raises a `ValidationError` requiring the daemon user in separate-user mode; the same construction with `docker_execution_mode == operator-rootless` validates with `docker_unprivileged_user is None`

#### Scenario: MachinectlAuth enum no longer exists

- **WHEN** `core.host_config` is inspected
- **THEN** there is no `MachinectlAuth` symbol — the single-value authentication enum is retired

### Requirement: Centralized machinectl Command Prefix Builder
The system SHALL provide a `machinectl_cmd(user)` function that returns the complete machinectl shell prefix as a `list[str]`. All machinectl invocations across the CLI and doctor modules SHALL use this function.

#### Scenario: Machinectl prefix
- **WHEN** `machinectl_cmd("sandbox")` is called
- **THEN** it returns `["sudo", "machinectl", "shell", "sandbox@.host"]`

### Requirement: Module Location

The `HostConfig` model, `HostSettings` model, `from_marker` builder, `machinectl_cmd` function, `pipe_cmd` function, `sudo_pipe_cmd` function, the subuid/subgid range parsers, the forward and inverse userns mappers, the `workspace_bridge_gid` helper, and the `autodetect_workspace_bridge_gid_recommendation` helper SHALL reside in `core/host_config.py`. There is no `MachinectlAuth` enum and no `from_toml` loader.

#### Scenario: Symbols reside in host_config

- **WHEN** `core.host_config` is imported
- **THEN** it exposes `HostConfig`, `HostSettings`, `from_marker`, `machinectl_cmd`, `pipe_cmd`, `sudo_pipe_cmd`, and the userns/subgid helpers, and exposes neither `MachinectlAuth` nor `HostConfig.from_toml`

### Requirement: Pipe Command Helper

The system SHALL provide a `pipe_cmd(user)` function in `core.host_config` that returns the byte-pipe-capable boundary-crossing prefix as a `list[str]`. The function SHALL return `["systemd-run", "-q", "--pipe", f"--uid={user}"]`. All unprivileged non-PTY byte-pipe boundary-crossings (e.g., the root setup-phase operator-state crossings — the SUDO-mode attach ProxyCommand crosses via `sudo_pipe_cmd`, C-010) SHALL use this function instead of constructing the prefix directly. The discipline "Never hardcode `sudo machinectl`" extends to "Never hardcode `systemd-run`."

#### Scenario: Pipe command prefix for sandbox user
- **WHEN** `pipe_cmd("sandbox")` is called
- **THEN** it returns `["systemd-run", "-q", "--pipe", "--uid=sandbox"]`

#### Scenario: Pipe command prefix for arbitrary user
- **WHEN** `pipe_cmd("claude-sandbox")` is called
- **THEN** it returns `["systemd-run", "-q", "--pipe", "--uid=claude-sandbox"]`

#### Scenario: Attach ProxyCommand composes the pipe primitives, never literals
- **WHEN** the implementation that constructs the attach ssh `ProxyCommand` argument (the `core.dispatch` streaming entrypoint) is inspected
- **THEN** it composes the crossing prefix via `sudo_pipe_cmd(host_config.host.docker_unprivileged_user)` rather than embedding the literal string `"systemd-run"` or its flags directly in the argv

### Requirement: Pipe Command vs Machinectl Command Distinction

The system SHALL maintain three distinct boundary-crossing primitives in `core.host_config`:

- `machinectl_cmd(user)` — for paths that require a PTY (e.g., the interactive `docker exec -it` handover) AND for the root setup-phase crossings (L5/L6/L7).
- `pipe_cmd(user)` — the **unprivileged** byte-pipe primitive `["systemd-run", "-q", "--pipe", f"--uid={user}"]`, for non-PTY byte pipes crossed by an already-privileged caller (the root setup-phase operator-state crossings). It takes no `auth` argument — `systemd-run`'s `manage-units` polkit action is the only authorization layer.
- `sudo_pipe_cmd(user)` — the **privileged**, per-op-sudoers-authorized sibling of `pipe_cmd`, returning `["sudo", *pipe_cmd(user)]` (i.e. `["sudo", "systemd-run", "-q", "--pipe", f"--uid={user}"]`). It **delegates** to `pipe_cmd` (it never re-spells the `systemd-run` literal — `pipe_cmd` stays the single sanctioned home for it). It is the separate-user dispatcher-op crossing — both the framed ops via `build_invocation` (design D2) and the streaming `fwd` op's attach ProxyCommand via the streaming entrypoint (C-010): an unprivileged operator crosses via a per-op sudoers rule that authorizes exactly this argv. It takes only `user` (no `auth` argument — the per-op sudoers rule is the only authorization layer on this path) and appends no `--unit`/`--description` (the argv must stay byte-identical to the per-op `Cmnd_Spec` the rule matches).

The choice of primitive at each call site SHALL be principled and load-bearing: PTY-needed call sites SHALL use `machinectl_cmd`; unprivileged byte-pipe call sites SHALL use `pipe_cmd`; separate-user dispatcher-op crossings SHALL use `sudo_pipe_cmd`.

Both `pipe_cmd` and `sudo_pipe_cmd` SKIP PAM by design — `systemd-run`'s `manage-units` polkit action does not invoke the PAM stack. PAM-enforced policies on the `dev → <sbuser>` transition (e.g., `pam_limits.conf` resource ceilings, custom session-class restrictions, audit subsystem login UID via `pam_loginuid`) DO NOT apply to `pipe_cmd`/`sudo_pipe_cmd` invocations. This trade-off SHALL be documented in `CLAUDE.md`'s "Privilege boundary" section alongside the existing `machinectl_cmd` discipline.

#### Scenario: CLAUDE.md documents the primitives
- **WHEN** the project's `CLAUDE.md` "Privilege boundary" section is inspected
- **THEN** it names `machinectl_cmd(user)` (for PTY paths + setup-root crossings), `pipe_cmd(user)` (unprivileged byte-pipe), and `sudo_pipe_cmd(user)` (the privileged separate-user op crossing that delegates to `pipe_cmd`), and states that the pipe primitives skip PAM whereas `machinectl_cmd` runs the full PAM stack

#### Scenario: sudo_pipe_cmd delegates to pipe_cmd
- **WHEN** `sudo_pipe_cmd(u)` and `pipe_cmd(u)` are compared
- **THEN** `sudo_pipe_cmd(u) == ["sudo", *pipe_cmd(u)]` (it prepends `sudo` to the unprivileged `pipe_cmd` rather than re-spelling the `systemd-run` literal), and contains no `--unit`, `--description`, or quoting of the uid

#### Scenario: Hardcoded systemd-run forbidden outside pipe_cmd
- **WHEN** the codebase outside `core/host_config.py` is searched for the literal string `"systemd-run"`
- **THEN** no hits appear in source paths that construct boundary-crossing argv (the only references permitted are inside `pipe_cmd` itself — `sudo_pipe_cmd` delegates to it — or in tests asserting the primitives' return values)

### Requirement: Subuid/Subgid Range Parsers

The system SHALL provide `parse_subuid_for_user(host_user: str) -> list[tuple[int, int]]` and `parse_subgid_for_user(host_user: str) -> list[tuple[int, int]]` in `core.host_config`. Each returns a list of `(first_allocated, count)` tuples for every line in `/etc/subuid` (or `/etc/subgid`) matching the given user. An empty list indicates the user has no subuid/subgid entry.

#### Scenario: Single-range subgid parsed correctly
- **WHEN** `/etc/subgid` contains `claude-sandbox:165536:65536` and `parse_subgid_for_user("claude-sandbox")` is called
- **THEN** the result is `[(165536, 65536)]`

#### Scenario: Multi-range subgid parsed correctly
- **WHEN** `/etc/subgid` contains two lines for the user (rare but legal per `man subuid`)
- **THEN** both ranges appear in the result list, in file order

#### Scenario: User with no entry returns empty list
- **WHEN** `parse_subgid_for_user("does-not-exist")` is called
- **THEN** the result is `[]`

### Requirement: Forward Userns Mapping

The system SHALL provide `host_id_for_in_container(N: int, host_user: str) -> int` and `host_gid_for_in_container(N: int, host_user: str) -> int` in `core.host_config`. The forward map for in-container uid/gid `N ≥ 1` is `first_allocated + N - 1` per the standard rootless-docker userns convention. For `N == 0`, the function returns the user's primary uid/gid (in-container root maps to the daemon owner).

#### Scenario: In-container uid 1000 maps via subuid line
- **WHEN** `/etc/subuid` has `claude-sandbox:165536:65536` and `host_id_for_in_container(1000, "claude-sandbox")` is called
- **THEN** the result is `166535` (= 165536 + 1000 - 1)

#### Scenario: In-container uid 0 maps to daemon owner
- **WHEN** `host_id_for_in_container(0, "claude-sandbox")` is called and `claude-sandbox` is uid 1001
- **THEN** the result is `1001`

#### Scenario: User with no subuid raises NoSubuidRangeError
- **WHEN** `host_id_for_in_container(1000, "no-such-user")` is called
- **THEN** `NoSubuidRangeError` is raised with a message identifying the user

#### Scenario: N exceeds the subuid range raises SubuidOutOfRangeError
- **WHEN** `host_id_for_in_container(70000, "claude-sandbox")` is called and the subuid range is `[165536, 165536+65536)`
- **THEN** `SubuidOutOfRangeError` is raised, identifying both N and the available range(s)

### Requirement: Inverse Userns Mapping

The system SHALL provide both `in_container_gid_for_host_gid(host_gid: int, host_user: str) -> int` and `in_container_uid_for_host_uid(host_uid: int, host_user: str) -> int` in `core.host_config`. Each iterates the corresponding parsed range list (`parse_subgid_for_user(host_user)` for gids, `parse_subuid_for_user(host_user)` for uids); if the input host id falls in some range `[first_allocated, first_allocated + count)`, the function returns `accumulated_offset + (host_id - first_allocated) + 1`, where `accumulated_offset` is the sum of `count` over all preceding ranges. Otherwise the function raises `SubgidOutOfRangeError` (gid path) or `SubuidOutOfRangeError` (uid path).

The uid resolver SHALL deliberately NOT special-case `host_uid == pwd.getpwnam(host_user).pw_uid` (the daemon user's primary uid). The inverse is asymmetric with `host_id_for_in_container`, which returns the user's primary uid for `N == 0`: the inverse raises `SubuidOutOfRangeError` for the daemon user's primary uid, surfacing the unexpected use case rather than silently translating to in-container `0` (which would chown to in-container root, an entirely different semantic). This asymmetry SHALL be documented in the function's docstring.

#### Scenario: Host gid in subgid range inverse-maps correctly
- **WHEN** `/etc/subgid` has `claude-sandbox:165536:65536` and `in_container_gid_for_host_gid(201665, "claude-sandbox")` is called
- **THEN** the result is `36130` (= 201665 - 165536 + 1)

#### Scenario: Host gid outside subgid range raises
- **WHEN** `in_container_gid_for_host_gid(100, "claude-sandbox")` is called (gid below the subgid range)
- **THEN** `SubgidOutOfRangeError` is raised, listing the searched ranges

#### Scenario: Multi-range subgid: matches the range containing the gid
- **WHEN** `/etc/subgid` has two ranges for the user and the host gid falls in the second
- **THEN** the inverse map is computed against the second range's `first_allocated`, with `accumulated_offset` set to the first range's `count`

#### Scenario: Host uid in subuid range inverse-maps correctly
- **WHEN** `/etc/subuid` has `claude-sandbox:165536:65536` and `in_container_uid_for_host_uid(166535, "claude-sandbox")` is called
- **THEN** the result is `1000` (= 166535 - 165536 + 1)

#### Scenario: Host uid outside subuid range raises
- **WHEN** `in_container_uid_for_host_uid(100, "claude-sandbox")` is called (uid below the subuid range)
- **THEN** `SubuidOutOfRangeError` is raised, listing the searched ranges

#### Scenario: Daemon user's primary uid is rejected by inverse
- **WHEN** `in_container_uid_for_host_uid(N, "claude-sandbox")` is called and `N == pwd.getpwnam("claude-sandbox").pw_uid` (the daemon user's primary uid, which lies outside the subuid range)
- **THEN** `SubuidOutOfRangeError` is raised; the function does NOT return `0` despite `host_id_for_in_container(0, "claude-sandbox")` returning that primary uid

#### Scenario: Multi-range subuid: matches the range containing the uid
- **WHEN** `/etc/subuid` has two ranges for the user and the host uid falls in the second
- **THEN** the inverse map is computed against the second range's `first_allocated`, with `accumulated_offset` set to the first range's `count`

#### Scenario: User without /etc/subuid entry raises
- **WHEN** `in_container_uid_for_host_uid(166535, "no-such-user")` is called
- **THEN** `NoSubuidRangeError` is raised

### Requirement: Workspace Bridge Group Configuration

`HostSettings` SHALL carry `workspace_bridge_group` (string) populated from the per-operator marker entry, **mode-scoped** per the Marker-Sourced Host Config Builder requirement (`sb-ws-<operator>` in `operator-rootless`, shared `sb-ws` in `separate-user`). There is **no operator-facing override** — the name is a setup-determined fact, not a user preference. The orchestrator SHALL resolve this name to a host gid via `grp.getgrnam` whenever it needs the workspace bridge gid; it SHALL NOT use the name for any access-control purpose (Linux access checks operate on numeric gids).

#### Scenario: operator-rootless name is per-operator

- **WHEN** the marker records operator `alice` as `operator-rootless`
- **THEN** `from_marker("alice").host.workspace_bridge_group == "sb-ws-alice"` and its gid lies in `alice`'s subgid range

#### Scenario: separate-user name is the shared default

- **WHEN** the marker records operator `bob` as `separate-user`
- **THEN** `from_marker("bob").host.workspace_bridge_group == "sb-ws"`

#### Scenario: Resolved gid drives all operations

- **WHEN** any orchestrator code path needs the workspace bridge group's gid
- **THEN** it calls `workspace_bridge_gid(host_config.host)` rather than referencing the name string

### Requirement: workspace_bridge_gid Helper

The system SHALL provide `workspace_bridge_gid(host: HostSettings) -> int` in `core.host_config` that resolves `host.workspace_bridge_group` to a host gid via `grp.getgrnam`, then validates the gid is in `host.docker_unprivileged_user`'s subgid range via `in_container_gid_for_host_gid`. The function returns the host gid (not the in-container gid; callers can compute the in-container gid via `in_container_gid_for_host_gid` if needed).

#### Scenario: Group exists and gid is in subgid range
- **WHEN** `workspace_bridge_gid(host)` is called and the configured group exists at gid `201665`, which is in the subgid range
- **THEN** the function returns `201665`

#### Scenario: Group does not exist raises WorkspaceBridgeGroupMissingError
- **WHEN** `workspace_bridge_gid(host)` is called and the configured group does not exist
- **THEN** `WorkspaceBridgeGroupMissingError` is raised, identifying the configured group name

#### Scenario: Group exists but gid is out of subgid range raises SubgidOutOfRangeError
- **WHEN** the configured group exists at a gid outside the daemon user's subgid range
- **THEN** `SubgidOutOfRangeError` is raised (propagated from `in_container_gid_for_host_gid`)

### Requirement: Autodetect Recommendation for Workspace Bridge gid

The system SHALL provide `autodetect_workspace_bridge_gid_recommendation(host_user: str, in_container_min: int = 1000) -> int` in `core.host_config`. The function picks the lowest available host gid in `host_user`'s subgid range(s) such that the corresponding in-container gid is `>= in_container_min` (default 1000, biasing above the conventional system-group range). The function is pure (no side effects) and is used by the doctor to print recommended `groupadd` commands. It does NOT mutate the host group database.

#### Scenario: Recommendation respects the in_container_min default
- **WHEN** `/etc/subgid` has `claude-sandbox:165536:65536`, no group at gid `166535`, and `autodetect_workspace_bridge_gid_recommendation("claude-sandbox")` is called
- **THEN** the recommendation is `166535` (host gid producing in-container gid 1000)

#### Scenario: Recommendation skips already-used gids
- **WHEN** the lowest candidate gid is taken by another group
- **THEN** the recommendation advances to the next available gid in the range

#### Scenario: Function does NOT create the group
- **WHEN** `autodetect_workspace_bridge_gid_recommendation` returns
- **THEN** the host group database is unchanged; only the integer recommendation is returned

#### Scenario: No subgid range raises NoSubgidRangeError
- **WHEN** the user has no subgid entry
- **THEN** `NoSubgidRangeError` is raised

#### Scenario: Subgid range fully populated raises NoFreeGidInSubgidRangeError
- **WHEN** every gid in every subgid range is already in `grp.getgrall()`
- **THEN** `NoFreeGidInSubgidRangeError` is raised

### Requirement: Dispatcher-Only `machinectl_cmd` Call Sites

After this change lands, `core.host_config.machinectl_cmd(...)` SHALL be called or imported in `src/` only by modules in the following three documented allowlist categories:

1. `src/core/host_config.py` — the helper's own definition module (self-reference, allowed).
2. `src/core/dispatch.py` — the typed dispatcher orchestration module that consumes the helper to build cross-boundary invocations.
3. Any module matching `src/core/setup/*.py` — the setup-phase package (forward reference; created and populated by the sister change `sandbox-setup`). Setup phases run as `root` and MUST cross the privilege boundary *before* the dispatcher binary is installed, so they cannot route through `core.dispatch`; the `src/core/setup/` package boundary is the trust boundary for this category. The glob is bounded to exactly this one package directory — never `src/**` or any broader pattern.

All other modules in `src/` (including `src/cli/main.py`, `src/core/doctor/checks/*`, `src/core/helper_container.py`, `src/core/actions/*`) SHALL route cross-boundary invocations through `core.dispatch` rather than building `machinectl_cmd(...)`-prefixed argv directly.

A meta-test in `tests/unit/test_conventions.py` SHALL enforce this convention by walking `src/` for `machinectl_cmd` usage and failing the gate on any caller outside the three allowlist categories above. The allowlist SHALL be defined in exactly one place (the meta-test) as the two literal module paths plus the single bounded directory glob `src/core/setup/*.py` (enumerated at test time via `pathlib`, not a free `src/**` walk). The sister change `sandbox-setup` does NOT amend this allowlist — its setup modules simply match the already-present `src/core/setup/*.py` category (which matches nothing until that change lands, since `runtime-dispatcher` lands first per the integration order). Adding any caller outside the three categories, or broadening the glob beyond the `src/core/setup/` package, is a spec change — made visible by the deliberate-violation regression test and code review, never a silent expansion. (This supersedes an earlier draft that mandated a "literal two-entry tuple, no globs"; that wording contradicted `sandbox-setup`'s legitimate need for setup phases to call `machinectl_cmd` directly — phase-3 review finding B-4. The anti-silent-expansion intent is preserved by bounding the one permitted glob to a single named package, not by forbidding globs outright.)

#### Scenario: Allowed modules call machinectl_cmd
- **WHEN** the meta-test runs against the post-refactor codebase
- **THEN** `src/core/host_config.py` (defining the function), `src/core/dispatch.py` (consuming the function), and any `src/core/setup/*.py` modules present (none until `sandbox-setup` lands) may contain `machinectl_cmd` references; the meta-test does not flag any of them

#### Scenario: New unauthorized caller fails the gate
- **WHEN** a developer adds `from core.host_config import machinectl_cmd` to a module outside the three allowlist categories (e.g. `src/cli/main.py`, `src/core/doctor/checks/foo.py`) and runs `make test` or `make coverage`
- **THEN** the meta-test fails with output naming the offending file and line, instructing the developer to route through `core.dispatch` instead

#### Scenario: Allowlist additions beyond the documented categories fail the gate
- **WHEN** a developer broadens the meta-test's allowlist beyond the three documented categories — e.g. adds a new literal module path, or widens the glob from `src/core/setup/*.py` to `src/core/**` or `src/**`
- **THEN** the deliberate-violation regression test (which asserts a known out-of-allowlist caller is rejected) and code review surface the change; the convention is documented in `CLAUDE.md`'s "Privilege boundary" section so reviewers refuse silent expansion. (A new module added *inside* `src/core/setup/` is intentionally permitted — that package is the trust boundary for category 3; gating new setup phases is a code-review concern, not this meta-test's job.)

### Requirement: Dispatcher-Compose Routing in CLAUDE.md

The repository's `CLAUDE.md` "Privilege boundary" section SHALL document that the orchestrator's cross-boundary invocations route through `core.dispatch` rather than `machinectl_cmd(...)` directly, and SHALL name the allowed direct-caller categories (the two boundary modules plus the `src/core/setup/` setup-phase package).

#### Scenario: CLAUDE.md names the dispatcher route
- **WHEN** a reader opens `CLAUDE.md` and reads the "Privilege boundary (load-bearing)" section
- **THEN** the section mentions `core.dispatch` as the canonical orchestrator-to-sandbox crossing path and notes that `machinectl_cmd(...)` is consumed only by `core.host_config` (self), `core.dispatch`, and the `src/core/setup/` setup-phase package (the latter populated by the `sandbox-setup` change; setup phases cross the boundary as root before the dispatcher exists)

### Requirement: Docker Execution Mode Selector

The execution mode is a **setup-determined fact**, not a user-editable config field. `HostSettings` SHALL carry `docker_execution_mode` (the `DockerExecutionMode` StrEnum `SEPARATE_USER = "separate-user"` / `OPERATOR_ROOTLESS = "operator-rootless"`), populated from the marker by `from_marker`, by setup, and by `minimal_host_config`. Its authoritative **source** is the setup-written marker (`/usr/local/libexec/sandbox-ai/setup-state.json`, per the `sandbox-setup` capability's marker requirement), resolved per operator.

The system SHALL provide `resolve_execution_mode(operator: str) -> DockerExecutionMode` in `core.setup_state` (the single module that owns the marker path + parsing; `core.host_config` would create an import cycle since `setup_state` imports `DockerExecutionMode` from it), reading the marker entry for `operator`. When the marker is absent or has no entry for the operator, the resolver SHALL fail closed by raising `ModeMarkerMissing` with a "run `sudo sandbox setup` first" message (parallel to the missing-`instances.json` "run sandbox init first" behavior) rather than silently defaulting. `"separate-user"` selects Docker as the dedicated `docker_unprivileged_user`, reached across the `machinectl` boundary; `"operator-rootless"` selects rootless Docker as the operator's own user with no boundary crossing.

#### Scenario: execution mode resolved from the marker

- **WHEN** `resolve_execution_mode(operator)` is called and the marker records the operator as `operator-rootless`
- **THEN** it returns `DockerExecutionMode.OPERATOR_ROOTLESS`

#### Scenario: missing marker fails closed

- **WHEN** `resolve_execution_mode(operator)` is called and the marker is absent or has no entry for the operator
- **THEN** it raises a "run `sandbox setup` first" error rather than defaulting to a mode

#### Scenario: execution mode resolved from the marker at runtime

- **WHEN** a runtime command builds its `HostConfig` via `from_marker(operator)` and the marker records the current operator as `operator-rootless`
- **THEN** the resolved `HostConfig.host.docker_execution_mode` is `OPERATOR_ROOTLESS`, sourced entirely from the marker (no toml is consulted)

### Requirement: Daemon Owner Resolution

The system SHALL provide the daemon-owner resolver in `core.host_config` returning the OS user that owns the rootless Docker daemon: in `separate-user` mode the configured `docker_unprivileged_user`; in `operator-rootless` mode the **invoking user** (the current process owner), never `docker_unprivileged_user`. It is provided in two forms sharing one implementation: `resolve_daemon_owner(host_config: HostConfig)` (the command-level alias) and `resolve_daemon_owner_settings(host: HostSettings)` (the single worker, for the internal helpers that hold only `HostSettings`); the former delegates to the latter. No `operator-rootless` code path SHALL resolve the daemon owner from `docker_unprivileged_user` (doing so would resolve to the stale default and silently corrupt on-disk ownership). All runtime owner-resolution sites (the lifecycle commands, the `compose-up` ActionContext, `workspace_bridge_gid`'s subgid validation, hydration's bridge-gid translation, the workspace shared-group phase, attach, doctor reads) SHALL route through this resolver; the worker is the sole sanctioned reader of `docker_unprivileged_user` for owner purposes.

#### Scenario: owner is the invoking user in operator-rootless

- **WHEN** `resolve_daemon_owner(...)` is called in `operator-rootless` mode
- **THEN** it returns the current (invoking) user, not `docker_unprivileged_user`

#### Scenario: owner is the dedicated user in separate-user

- **WHEN** `resolve_daemon_owner(...)` is called in `separate-user` mode
- **THEN** it returns `host.docker_unprivileged_user` (behavior unchanged)

### Requirement: Runtime Commands Refuse Root

The runtime commands (`init`, `start`, `stop`, `status`, `attach`, `destroy`, and the `workspace` subcommands) SHALL refuse to run as root (`os.geteuid() == 0`), with a message directing the operator to run as their own (non-root) account. This guarantees `sandbox_ai_home()` (`~/.sandbox-ai`) never resolves to `/root/.sandbox-ai` for a runtime command. `sandbox setup` is exempt from this guard (it legitimately runs as root in `separate-user` mode; its `operator-rootless` root-refusal is specified by the `sandbox-setup` capability).

#### Scenario: runtime command refuses root

- **WHEN** any runtime command (e.g. `sandbox start`) is invoked with `euid == 0`
- **THEN** it refuses with a "run as your operator account, not root" message and creates no `/root/.sandbox-ai` tree

### Requirement: Privileged byte-pipe crossing primitive `sudo_pipe_cmd`

`core.host_config` SHALL provide `sudo_pipe_cmd(user)` returning
`["sudo", "systemd-run", "-q", "--pipe", f"--uid={user}"]` — the privileged, per-op-sudoers-authorized
byte-pipe sibling of the unprivileged `pipe_cmd`. It SHALL NOT accept an auth argument (the SUDO sudoers rule
is the only authorization layer on this path) and SHALL NOT append `--unit` or `--description` (the argv must
stay byte-identical to the per-op `Cmnd_Spec` the sudoers rule matches).

#### Scenario: Prefix shape
- **WHEN** `sudo_pipe_cmd("sandbox")` is called
- **THEN** it returns `["sudo", "systemd-run", "-q", "--pipe", "--uid=sandbox"]`
- **AND** the list contains no `--unit`, `--description`, or quoting of the uid

#### Scenario: Distinct from the unprivileged pipe
- **WHEN** `sudo_pipe_cmd(u)` and `pipe_cmd(u)` are compared
- **THEN** `sudo_pipe_cmd` is `pipe_cmd` prefixed with `sudo` (the unprivileged `pipe_cmd` is unchanged)

### Requirement: Crossing-primitive selection by (mode, auth, op-kind)

The crossing primitive for an orchestrator→sandbox op SHALL be selected as: operator-rootless → no crossing
(local subprocess); separate-user + non-interactive (framed) op → `sudo_pipe_cmd(user)`. The
**streaming** `fwd` op (the attach ProxyCommand — a binary byte stream, so `machinectl_cmd` is never an
option) SHALL cross via `sudo_pipe_cmd(user)` under separate-user (sudoers-authorized,
headless-capable — C-010/F-060).

#### Scenario: SUDO separate-user op uses the privileged pipe
- **WHEN** a non-interactive dispatcher op runs under separate-user auth
- **THEN** the crossing prefix is `sudo_pipe_cmd(<docker_unprivileged_user>)`, not `machinectl_cmd(...)`

#### Scenario: attach ProxyCommand prefix
- **WHEN** `sandbox attach` builds its SSH ProxyCommand under separate-user
- **THEN** the crossing prefix is `sudo_pipe_cmd(user)`; the crossed payload (`dispatch fwd <wire>`) is the
  same, and `machinectl_cmd` is used in neither (the PTY's `onlcr` would corrupt the SSH stream)

### Requirement: Single-Sourced Execution-Mode Default

There SHALL be exactly ONE named execution-mode default in the system: `core.host_config.DEFAULT_PROVISIONING_MODE` (= `DockerExecutionMode.OPERATOR_ROOTLESS`). It is the mode `sandbox setup` provisions when the operator passes no `--docker-execution-mode` flag and the marker has no entry, AND the in-memory carrier default on `HostSettings.docker_execution_mode`, `minimal_host_config`'s `mode` parameter, and every other execution-mode parameter/field default across `src/` (finding F-051). A bare `DockerExecutionMode` enum member (`SEPARATE_USER` / `OPERATOR_ROOTLESS`) SHALL NOT appear as a function-parameter default or a field (`AnnAssign`) default RHS anywhere in `src/` except `src/core/host_config.py` (the module that DEFINES the constant); every such default SHALL reference `DEFAULT_PROVISIONING_MODE` instead. The carrier value is moot at runtime — `_resolve_full_host_config` always overlays the marker-resolved mode — so single-sourcing it is behavior-preserving; the point is that the codebase never carries two opposite-valued defaults that could diverge and crystallize into the public docs.

This is enforced STRUCTURALLY (mirroring the "Dispatcher-Only `machinectl_cmd` Call Sites" gate). A meta-test in `tests/unit/test_conventions.py` SHALL `ast`-walk `src/` and fail the gate on any bare `DockerExecutionMode` member that appears in a DEFAULT position — a `FunctionDef`/`AsyncFunctionDef` `args.defaults` or `args.kw_defaults` entry, or an `AnnAssign` value — outside the single-element allowlist `{src/core/host_config.py}`. The detector SHALL deliberately NOT flag the non-default positions: `Set` elements (`applies_in=frozenset({DockerExecutionMode.SEPARATE_USER})` membership sets), `Compare` operands (`mode is DockerExecutionMode.…`), or `Call` arguments (an explicit `write_mode_root_owned(…, SEPARATE_USER)` or `minimal_host_config(u, a, SEPARATE_USER)`). The allowlist SHALL be defined in exactly one place (the meta-test); broadening it is a spec change, made visible by the deliberate-violation regression and code review, never a silent expansion. The detector file-iterable + allowlist SHALL be parameters (not module state) so a deliberate-violation regression drives the SAME predicate.

#### Scenario: Named-constant references pass the gate
- **WHEN** the meta-test runs against the codebase where every execution-mode default outside `src/core/host_config.py` references `DEFAULT_PROVISIONING_MODE`
- **THEN** the gate passes; `src/core/host_config.py` (defining the constant) may spell the bare enum member and is not flagged, and the membership-set / comparison / call-argument uses of the bare member elsewhere are not flagged

#### Scenario: A new bare-literal default fails the gate naming file and line
- **WHEN** a developer writes a bare `DockerExecutionMode.SEPARATE_USER` (or `OPERATOR_ROOTLESS`) as a function-parameter default or a field-default RHS in any `src/` module other than `src/core/host_config.py` and runs `make test` or `make coverage`
- **THEN** the meta-test fails with output naming the offending file and line, instructing the developer to reference `core.host_config.DEFAULT_PROVISIONING_MODE` instead

#### Scenario: Deliberate-violation regression proves the detector bites
- **WHEN** the deliberate-violation regression test drives the shared detector against a written rogue file that contains a bare `DockerExecutionMode` member as BOTH a function-parameter default and an `AnnAssign` field default, plus the bare member in a `frozenset({…})` membership set, an `is` comparison, and a call argument
- **THEN** the detector reports exactly the two default-slot lines (the param default and the field default) and does NOT report the membership-set, comparison, or call-argument lines — proving the gate catches the regression class while preserving the structural carve-outs

### Requirement: Marker-Sourced Host Config Builder

`HostConfig` SHALL be constructed from the root-owned setup marker, not from a toml file. The system SHALL provide `HostConfig.from_marker(operator: str) -> HostConfig` which overlays the per-operator marker entry onto built-in defaults and returns a fully-resolved `HostConfig`. The marker (`/usr/local/libexec/sandbox-ai/setup-state.json`, owned by the `sandbox-setup` capability) is **per-operator keyed and mode-conditional**; each operator entry SHALL carry:

- `mode` — always present (`DockerExecutionMode`).
- `workspace_bridge_group` + `workspace_bridge_gid` — always present, **mode-scoped**: in `operator-rootless` the group name is per-operator (`sb-ws-<operator>`) with a gid in that operator's subgid range; in `separate-user` the name is the shared `sb-ws` with a gid in the single shared range. The group **name** is the load-bearing key — a group name maps to exactly one gid in `/etc/group`, so a per-operator gid requires a per-operator name.
- `docker_unprivileged_user` — present **only** in `separate-user` entries (in `operator-rootless` the daemon owner is the invoking operator, resolved intrinsically; the field is `None`).

`HostSettings` remains the in-memory carrier. There SHALL be no `sandbox-ai.toml` file and no toml loader; the marker is the single source of truth.

#### Scenario: Host config built from the marker entry

- **WHEN** `HostConfig.from_marker("alice")` is called and the marker records `alice` as `operator-rootless` with `workspace_bridge_group = "sb-ws-alice"`, `workspace_bridge_gid = 524288`
- **THEN** the returned `HostConfig.host` carries `docker_execution_mode == OPERATOR_ROOTLESS`, `workspace_bridge_group == "sb-ws-alice"`, and `docker_unprivileged_user is None`

#### Scenario: separate-user entry carries the daemon user

- **WHEN** `HostConfig.from_marker("bob")` is called and the marker records `bob` as `separate-user` with `docker_unprivileged_user = "sandbox"`, `workspace_bridge_group = "sb-ws"`
- **THEN** the returned `HostConfig.host` carries `docker_execution_mode == SEPARATE_USER`, `docker_unprivileged_user == "sandbox"`, and the shared `workspace_bridge_group == "sb-ws"`

A marker entry that lacks the mode-conditional host facts this requirement defines (e.g. a legacy C-004-era entry carrying only `mode`, written before the schema was generalized) SHALL be treated as **not fully provisioned**: `from_marker` SHALL raise the same not-set-up signal as a missing entry (directing the operator to re-run `sudo sandbox setup`, which rewrites the full entry), rather than defaulting the absent fields. This is consistent with the hard re-setup migration (there are no in-place-upgraded hosts to preserve).

#### Scenario: missing marker entry fails closed

- **WHEN** `HostConfig.from_marker(operator)` is called and the marker is absent or has no entry for `operator`
- **THEN** it raises `ModeMarkerMissing` with a "run `sudo sandbox setup` first" message rather than defaulting

#### Scenario: legacy mode-only marker entry treated as unprovisioned

- **WHEN** `HostConfig.from_marker(operator)` is called and the operator's marker entry carries only `mode` (no `workspace_bridge_group`/`workspace_bridge_gid`, and no `docker_unprivileged_user` for a separate-user entry)
- **THEN** it raises the same not-set-up signal as a missing entry, directing the operator to re-run `sudo sandbox setup` — it does NOT default the absent host facts

