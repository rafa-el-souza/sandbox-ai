## Purpose

This specification defines the `sandbox-ai.toml` per-host orchestrator configuration file — schema, location, Pydantic model, loader interface, and the centralized machinectl command prefix builder that consumes it.
## Requirements
### Requirement: Host Config File Location and Format
The system SHALL read per-host orchestrator configuration from a file at the canonical path `<sandbox_ai_user_home()>/config/sandbox-ai.toml` (resolved via the `per-user-state-layout` capability). The file SHALL use TOML format with a `[host]` section. There is no CLI override for the path; testing uses the `SANDBOX_AI_USER_HOME` env var.

#### Scenario: Valid host config parsed
- **WHEN** `<home>/config/sandbox-ai.toml` exists with a valid `[host]` section
- **THEN** the system parses it into a `HostConfig` Pydantic model without errors

#### Scenario: Host config not found
- **WHEN** `<home>/config/sandbox-ai.toml` does not exist
- **THEN** the loader raises `FileNotFoundError` which callers translate to a user-facing error: "No sandbox-ai.toml found at `<resolved-path>`. Run `sandbox init` to create one."

#### Scenario: Invalid TOML rejected
- **WHEN** `<home>/config/sandbox-ai.toml` contains malformed TOML syntax
- **THEN** the loader raises a parse error before any state changes occur

#### Scenario: CWD-local sandbox-ai.toml is silently ignored
- **WHEN** the loader runs and `<cwd>/sandbox-ai.toml` exists but `<home>/config/sandbox-ai.toml` does not
- **THEN** the loader ignores the CWD-local file and raises `FileNotFoundError` for the canonical path. The doctor (separately) detects the legacy file and warns the operator to migrate.

### Requirement: Host Config Schema
The `[host]` section SHALL contain `docker_unprivileged_user` (required string) and `machinectl_authentication` (string enum, default `"sudo"`). The `machinectl_authentication` field SHALL accept only the value `"sudo"` (the field is retained as a single-value seam pending its full removal; POLKIT mode is retired).

#### Scenario: Both fields present
- **WHEN** `sandbox-ai.toml` contains `[host]` with `docker_unprivileged_user = "sandbox"` and `machinectl_authentication = "sudo"`
- **THEN** the model validates successfully with `docker_unprivileged_user == "sandbox"` and `machinectl_authentication == MachinectlAuth.SUDO`

#### Scenario: Authentication defaults to sudo
- **WHEN** `sandbox-ai.toml` contains `[host]` with `docker_unprivileged_user` but omits `machinectl_authentication`
- **THEN** the model applies default `machinectl_authentication = "sudo"`

#### Scenario: Invalid authentication value rejected
- **WHEN** `sandbox-ai.toml` contains `machinectl_authentication = "pkexec"`
- **THEN** the Pydantic model raises a `ValidationError` identifying the invalid enum value

#### Scenario: Missing docker_unprivileged_user rejected
- **WHEN** `sandbox-ai.toml` contains `[host]` without `docker_unprivileged_user`
- **THEN** the Pydantic model raises a `ValidationError` identifying the missing required field

### Requirement: Path-Parameterized Loader
The `HostConfig.from_toml()` class method SHALL take no arguments and SHALL resolve the canonical path internally via `sandbox_ai_user_home()`. The previous `project_dir: str` parameter is removed.

#### Scenario: Loader uses canonical path
- **WHEN** `HostConfig.from_toml()` is called
- **THEN** the loader reads `<sandbox_ai_user_home()>/config/sandbox-ai.toml` regardless of the process CWD

#### Scenario: Loader honors SANDBOX_AI_USER_HOME for testing
- **WHEN** `HostConfig.from_toml()` is called with `SANDBOX_AI_USER_HOME=/tmp/t/.sandbox-ai` set
- **THEN** the loader reads `/tmp/t/.sandbox-ai/config/sandbox-ai.toml`

### Requirement: Pydantic Model Structure
The host config model SHALL use a `MachinectlAuth` StrEnum for the authentication field and a nested `HostSettings` model for the `[host]` section.

#### Scenario: MachinectlAuth enum members
- **WHEN** the `MachinectlAuth` enum is inspected
- **THEN** it contains exactly one member: `SUDO = "sudo"`

#### Scenario: HostSettings nested model
- **WHEN** a `HostConfig` is loaded
- **THEN** `host_config.host` is a `HostSettings` instance with `docker_unprivileged_user` and `machinectl_authentication` attributes

### Requirement: Centralized machinectl Command Prefix Builder
The system SHALL provide a `machinectl_cmd(user)` function that returns the complete machinectl shell prefix as a `list[str]`. All machinectl invocations across the CLI and doctor modules SHALL use this function.

#### Scenario: Machinectl prefix
- **WHEN** `machinectl_cmd("sandbox")` is called
- **THEN** it returns `["sudo", "machinectl", "shell", "sandbox@.host"]`

### Requirement: Module Location

The `HostConfig` model, `MachinectlAuth` enum, `HostSettings` model, `machinectl_cmd` function, `pipe_cmd` function, `sudo_pipe_cmd` function, the subuid/subgid range parsers, the forward and inverse userns mappers, the `workspace_bridge_gid` helper, and the `autodetect_workspace_bridge_gid_recommendation` helper SHALL reside in `core/host_config.py`. The legacy `_resolve_sandbox_ai_home()` helper is removed.

#### Scenario: Import path
- **WHEN** other modules need host config, machinectl prefix building, pipe-cmd prefix building, subuid resolution, or bridge-group resolution
- **THEN** they import from `core.host_config`

#### Scenario: Legacy resolver absent
- **WHEN** `core.host_config` is inspected
- **THEN** there is NO `_resolve_sandbox_ai_home()` symbol; callers use `sandbox_ai_home()` from the `per-user-state-layout` capability

#### Scenario: Public symbols enumerated
- **WHEN** the public surface of `core.host_config` is inspected
- **THEN** it includes `HostConfig`, `MachinectlAuth`, `HostSettings`, `machinectl_cmd`, `pipe_cmd`, `sudo_pipe_cmd`, the subuid/subgid parsers, the forward/inverse userns mappers, `workspace_bridge_gid`, and `autodetect_workspace_bridge_gid_recommendation`

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

`HostSettings` SHALL gain an optional field `workspace_bridge_group: str = "sb-ws"`. The orchestrator SHALL resolve this name to a host gid via `grp.getgrnam` whenever it needs the workspace bridge gid; the orchestrator SHALL NOT use the name for any access-control purpose (Linux access checks operate on numeric gids).

#### Scenario: Default value is sb-ws
- **WHEN** `sandbox-ai.toml` does not specify `[host].workspace_bridge_group`
- **THEN** `host_config.host.workspace_bridge_group == "sb-ws"`

#### Scenario: Operator overrides the name
- **WHEN** `sandbox-ai.toml` contains `[host] workspace_bridge_group = "my-bridge"`
- **THEN** `host_config.host.workspace_bridge_group == "my-bridge"`

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

The execution mode is a **setup-determined fact**, not a user-editable config field. `HostSettings` SHALL retain `docker_execution_mode` only as an **in-memory carrier** (the `DockerExecutionMode` StrEnum `SEPARATE_USER = "separate-user"` / `OPERATOR_ROOTLESS = "operator-rootless"`, populated programmatically by setup, by the runtime overlay, and by `minimal_host_config`); it SHALL NOT be sourced from the toml. `HostConfig.from_toml` SHALL **reject** a `[host]` table that sets `docker_execution_mode` (a fail-fast error directing the operator to setup, not a silent override) — its authoritative **source** is the setup-written execution-mode marker (`/usr/local/libexec/sandbox-ai/setup-state.json`, per the `sandbox-setup` capability's "Execution-Mode Marker" requirement), resolved per operator.

The system SHALL provide `resolve_execution_mode(operator: str) -> DockerExecutionMode` in `core.setup_state` (the single module that owns the marker path + parsing; `core.host_config` would create an import cycle since `setup_state` imports `DockerExecutionMode` from it), reading the marker entry for `operator`. When the marker is absent or has no entry for the operator, the resolver SHALL fail closed by raising `ModeMarkerMissing` with a "run `sudo sandbox setup` first" message (parallel to the missing-`instances.json` "run sandbox init first" behavior) rather than silently defaulting; the runtime resolves the mode for the current user and overlays it onto the `HostConfig` built from the toml. `"separate-user"` selects the existing behavior (Docker as the dedicated `docker_unprivileged_user`, reached across the `machinectl` boundary); `"operator-rootless"` selects rootless Docker as the operator's own user with no boundary crossing. In `"operator-rootless"` mode the `machinectl_authentication` field is inert.

#### Scenario: execution mode resolved from the marker

- **WHEN** `resolve_execution_mode(operator)` is called and the marker records the operator as `operator-rootless`
- **THEN** it returns `DockerExecutionMode.OPERATOR_ROOTLESS`

#### Scenario: missing marker fails closed

- **WHEN** `resolve_execution_mode(operator)` is called and the marker is absent or has no entry for the operator
- **THEN** it raises a "run `sandbox setup` first" error rather than defaulting to a mode

#### Scenario: execution mode overlaid from the marker at runtime

- **WHEN** a runtime command builds its `HostConfig` from the toml and the marker records the current operator as `operator-rootless`
- **THEN** the resolved `HostConfig.host.docker_execution_mode` is `OPERATOR_ROOTLESS` (overlaid from the marker), regardless of the toml (which carries no mode), while the toml-sourced fields are preserved

#### Scenario: docker_execution_mode in the toml is rejected

- **WHEN** `sandbox-ai.toml` `[host]` contains a `docker_execution_mode` key
- **THEN** `HostConfig.from_toml` raises a fail-fast error stating the mode is setup-determined (the marker) and is not a toml field — it is NOT silently ignored

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

