## Purpose

This specification defines the deterministic execution constraints bounding the Python CLI architecture. It dictates the invariant orchestration logic required to safely bootstrap the environment, isolate Host terminal contexts from unprivileged Daemon payloads utilizing `machinectl`, and manage asynchronous AI agent handovers strictly within POSIX containment parameters.
## Requirements
### Requirement: Python CLI Orchestrator Execution
The system SHALL execute utilizing a strict Python `typer` interface to deterministically govern the `sandbox` operational lifecycle across the following commands: `init`, `start`, `stop`, `attach`, `destroy`, `doctor`, `status`, and the `workspace` subcommand group (`workspace add | remove | rename | restore | list`). Lifecycle commands (`start`, `stop`, `attach`, `destroy`, `status`, `workspace ...`) take an explicit `<inst>` argument; CWD-based discovery is removed.

#### Scenario: Tooling Plane Bootstrapping
- **WHEN** the orchestrator initiates execution on a fresh host machine
- **THEN** the operator runs `sandbox init <inst>` (on a host already provisioned by `sudo sandbox setup` — init is setup-first and fails loud otherwise), which creates the per-user state tree at `<sandbox_ai_home()>/{config,state,instances,workspaces}/`, scaffolds the per-instance directory tree under `<sandbox_ai_home()>/instances/<inst>/`, scaffolds at least one workspace tree under `<sandbox_ai_home()>/workspaces/<inst>/<ws>/`, and writes the `.initialized` sentinel

#### Scenario: Agent Startup Sequence
- **WHEN** the human operator executes `sandbox start <inst>`
- **THEN** the CLI verifies the `.initialized` sentinel exists, runs doctor Chain 1 pre-flight (including bridge-group existence and dev supplementary-group membership), validates secret completeness, then performs a pre-lock warm state check, acquires `state.lock` and the IPAM lock, allocates a `/24` subnet quintuple, and executes the ownership-sensitive phase order owned by `orchestrator-volumes`'s `Phase Order Contract for Ownership-Sensitive Phases`: `_phase_workspace_shared_group` (per-workspace chgrp/chmod 2770/setgid + persistent default ACL — runs BEFORE named-ACL grants so chmod 2770 lands on a non-extended-ACL inode), `_phase_acl_grant` (per-workspace named-ACL grants + instance-dir set + helper-recipe parent grants), `_phase_credentials` (SSH keypair + bcrypt htpasswd generation into `secrets/`), `_phase_hydrate` (Pydantic + Jinja2 hydration with multi-workspace fan-out in compose template volumes), `_phase_grant_post_hydrate_daemon_read` (unified setfacl-as-owner pass over `RO_FILE_RECIPES + EXEC_FILE_RECIPES + RW_FILE_RECIPES + DAEMON_READ_DIRECT_FILES`), the helper-recipe phases (`_phase_helper_mkdir_chown_cache_log` for cache/log subuid-chown; `_phase_helper_cp_chown_ro_files` for ro/exec/rw consumer-uid-0-chown), and finally `_phase_compose_up` (the `compose-up` dispatcher op — `docker compose up -d --build --wait` — crossed per auth mode); progress is displayed for each phase. On all containers healthy, the CLI defaults to handing the terminal over to **core** (as `agent`) via the `cli-attach` command shape (`tlog-rec → ssh → ProxyCommand → /fwd`) when stdin is a TTY (suppress with `--no-handover`; per `cli-start`'s `Handover Default Direction` and `Handover TTY Autodetect`).

#### Scenario: Instance State Query
- **WHEN** the human operator executes `sandbox status [<inst>]`
- **THEN** the CLI displays a Rich-formatted dashboard. With `<inst>` argument: instance identity, container health, IPAM allocation, config completeness warnings, and a Workspaces section listing each workspace with its mode, path, and state (per `cli-status`). Without argument: a summary table of all registered instances.

#### Scenario: Graceful Teardown
- **WHEN** the human operator executes `sandbox stop <inst>`
- **THEN** the CLI terminates running containers via `docker compose down` (or `down -v` with `--clean`), then revokes the `sandbox` user's named-ACL entries on the instance dir set (`<sandbox_ai_home()>/instances/<inst>/`, plus `docker/`, `config/`, `secrets/`) AND on each workspace's path (effective + default-ACL named-entry portion, per `cli-stop`). The `.sandbox.env` named-ACL entry is in the `granted-once, persistent` lifecycle and is NOT revoked at stop (per `orchestrator-volumes`'s `Environment File Read ACL` + `Acl Revoke Plan Excludes Persistent Grants`).

#### Scenario: Workspace Lifecycle Operations
- **WHEN** the human operator executes any of `sandbox workspace add | remove | rename | restore | list <inst> ...`
- **THEN** the CLI dispatches to the corresponding workspace lifecycle handler (per `cli-workspace`), which validates gates (instance must be stopped for mutating ops), applies workspace-specific filesystem operations (mkdir, rsync, atomic rename, etc.), and updates `<sandbox_ai_home()>/instances/<inst>/sandbox.toml`'s `[workspaces]` map

#### Scenario: Concurrency Collision Mitigation
- **WHEN** a background process or secondary terminal concurrently executes `sandbox start` on the same instance
- **THEN** the native `fcntl` filesystem lock (`<sandbox_ai_home()>/state/state.lock`) rejects the OS request, forcing the Orchestrator to gracefully abort the execution loop before triggering a Docker socket race condition. Per-instance backup locks (`<sandbox_ai_home()>/state/<inst>.backup.lock`) provide a parallel fail-fast for backup-vs-lifecycle conflicts.

#### Scenario: Standardized Module Execution
- **WHEN** the Orchestrator triggers via a pure `python -m cli` boundary execution
- **THEN** the system rigidly interprets a `cli/__main__.py` block seamlessly mapping execution arrays backwards explicitly into the `cli/main.py` entrypoint schema.

#### Scenario: Opaque Error Bounds Trace
- **WHEN** the orchestrator fails POSIX boundaries triggering native `CalledProcessError` exceptions
- **THEN** the system raises a `SandboxExecutionError` masking host topology variables and rendering clinical error messages identifying the failed command without leaking environment state.

### Requirement: Sub-Process Privilege Bounding
The system SHALL isolate all Docker command execution across the `dev`/`sandbox` privilege boundary using `machinectl shell <docker_unprivileged_user>@.host` for PTY-needing paths and the byte-pipe primitives `pipe_cmd` (`systemd-run -q --pipe --uid=<docker_unprivileged_user>`) / `sudo_pipe_cmd` (`sudo systemd-run -q --pipe --uid=<docker_unprivileged_user>`) for byte-pipe paths. Non-interactive dispatcher op crossings cross via `sudo_pipe_cmd` (the privileged, per-op-sudoers-authorized byte-pipe; C-009 design D2). The centralized `machinectl_cmd()` builder from `core.host_config` SHALL be called only by the three documented allowlist categories defined in the `host-config` capability: `src/core/host_config.py` (self), `src/core/dispatch.py` (the typed dispatcher's orchestration module), and `src/core/setup/*.py` (the setup-phase package — sister change `sandbox-setup`; setup runs as root and crosses the boundary before the dispatcher is installed, so it cannot route through `core.dispatch`). Every other module crossing the boundary SHALL route through `core.dispatch.invoke(op, args, host_config)` (framed ops) or `core.dispatch`'s streaming entrypoint (the streaming op — the operator-handover `ProxyCommand`, per `runtime-dispatcher`'s "Streaming ProxyCommand Entrypoint"). The operator-handover `ProxyCommand` crosses as the bare `dispatch fwd <wire>` payload via `sudo_pipe_cmd()` (sudoers-authorized, headless-capable — the F-060 fix).

The orchestrator's non-interactive Docker invocations SHALL take the shape `[*sudo_pipe_cmd(user), "/bin/bash", "-c", f"/usr/local/libexec/sandbox-ai/dispatch {op} {shlex.join(args)}"]` under separate-user rather than embedding inline bash. The outer `/bin/bash -c` wrapper is retained permanently: it is what bridges the orchestrator's argv (composed on the operator's host) into a bash environment inside the `docker_unprivileged_user` session, where the dispatcher binary at `/usr/local/libexec/sandbox-ai/dispatch` is reachable. The swap from `machinectl shell` to `sudo systemd-run --pipe` is the C-009 fix for the Debian-family stdout-delivery failure (F-063): `machinectl shell` PTY crossings do not reliably deliver stdout on apt hosts, while `sudo systemd-run --pipe` is immune everywhere; the crossed payload stays the bare `dispatch <op> <wire>` so the sister capability's per-op sudoers `Cmnd_Spec` (the pipe-spec form, also rendered from `sudo_pipe_cmd`) still matches argv-by-argv, and the inner exit is recovered from the dispatcher frame (`framed=True`), not the native `--pipe` exit (unreliable — F-064). The sister capability writes the per-op `Cmnd_Spec`s inlined directly into the operator user-spec (no shared `Cmnd_Alias` — F-020), each carrying the full `systemd-run --pipe` invocation prefix and backslash-escaped whitespace, validated empirically by V9 (`openspec/explorations/ongoing/sandbox-setup/validation.md`) and grounded in finding F-004 (sudoers-cmnd-spec-quoting-not-shell-quoting). The dispatcher binary at `/usr/local/libexec/sandbox-ai/dispatch` is `root:root` mode `0755` on disk (with `chattr +i`), but EXECUTES at runtime as the `docker_unprivileged_user` — the `sudo systemd-run --uid=<user>` crossing drops to that uid before bash execs the dispatcher.

`machinectl_cmd()` and `sudo_pipe_cmd()` deliberately emit the **relative** command names `machinectl` / `systemd-run` (not absolute paths) — this is intentional and unchanged by this capability. `sudo` resolves the relative `systemd-run` against the sudoers `secure_path` and matches the *resolved absolute path* against the rule's pipe `Cmnd_Spec`. Making the orchestrator's relative emission and the sister rule's absolute Cmnd_Spec coincide is owned entirely by `sandbox-setup` (it resolves `systemd-run` on the same secure_path basis at L0, pins that path in the rule, verifies the bridge per-host with a relative-form L3a pipe probe, and re-checks it via `setup_invariants`). This capability asserts only that the runtime invocation uses relative `systemd-run`; it does NOT pin an absolute path. See `sandbox-setup` design B-3/D2 and validation track V9e.

#### Scenario: Non-Interactive Daemon Interaction (sudo mode)
- **WHEN** the Python orchestrator needs to execute a non-interactive Docker command
- **THEN** it invokes (via `core.dispatch`): `subprocess.run(["sudo", "systemd-run", "-q", "--pipe", "--uid=<user>", "/bin/bash", "-c", "/usr/local/libexec/sandbox-ai/dispatch <op> <quoted args>"])` with `capture_output=True`; `<op>` is one of the eleven **framed** ops enumerated by `runtime-dispatcher` (the streaming `fwd` op is never executed by the orchestrator — its argv is constructed for the ssh client's ProxyCommand, per the Interactive PTY scenario below); `<quoted args>` is `shlex.join` of the op's validated args; the inner exit is recovered from the dispatcher frame (`framed=True`), not the native `--pipe` exit

#### Scenario: Interactive PTY Execution (sudo mode)
- **WHEN** the orchestrator hands the terminal to core
- **THEN** it invokes: `subprocess.run(["tlog-rec", "--writer=file", "--file-path=<host-side log path>", "--", "ssh", "-F", "/dev/null", "-i", "<inst_dir>/secrets/ipc_ssh_key", "-o", "UserKnownHostsFile=<inst_dir>/secrets/ipc_known_hosts", "-o", "StrictHostKeyChecking=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none", "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "ClearAllForwardings=yes", "-o", "PermitLocalCommand=no", "-o", "ProxyCommand=sudo systemd-run -q --pipe --uid=<docker_unprivileged_user> /bin/bash -c '/usr/local/libexec/sandbox-ai/dispatch fwd <inst> --project <project_name> --ip <core_ipc_ip>'", "-p", "9999", "-t", "agent@<core_ipc_ip>", "cd /workspaces/<ws> && exec bash -l"])` with stdin/stdout/stderr inherited; the `ProxyCommand` IS prefixed with `sudo` (it crosses via `sudo_pipe_cmd` so the per-op sudoers `Cmnd_Spec` authorizes it headlessly — C-010/F-060); the orchestrator never executes the ProxyCommand itself — the argv is constructed by `core.dispatch`'s streaming entrypoint and executed by the ssh client (the streaming op carries no orchestrator-interpreted content)

#### Scenario: Dispatcher-only routing convention
- **WHEN** any module in `src/` outside the three `host-config` allowlist categories (i.e. not `src/core/host_config.py`, not `src/core/dispatch.py`, not a `src/core/setup/*.py` setup-phase module) builds a cross-boundary invocation
- **THEN** it MUST call `core.dispatch.invoke(op, args, host_config)` (or an equivalent dispatch helper) rather than constructing the argv list with `machinectl_cmd(...)` directly; the convention meta-test in `tests/unit/test_conventions.py` enforces this at gate time

### Requirement: Host Config Loading in CLI Commands
All post-init CLI commands (`start`, `stop`, `attach`, `destroy`, `status`, all `workspace ...` subcommands) SHALL load per-host config from the setup marker via `HostConfig.from_marker(operator)` and read `docker_unprivileged_user` and `workspace_bridge_group` from the resolved `HostSettings`. There is no `machinectl_authentication` field and no `sandbox-ai.toml` file (see the `host-config` capability). The marker path is resolved internally; CWD is no longer consulted.

#### Scenario: Post-init command reads host config from the marker
- **WHEN** any post-init command runs on a host that has been set up for the invoking operator
- **THEN** `docker_unprivileged_user` (separate-user only) and `workspace_bridge_group` are sourced from the operator's marker entry via `HostConfig.from_marker(operator)`, with no toml read

#### Scenario: Post-init command on an unprovisioned host directs to setup
- **WHEN** any post-init command runs on a host that has not been set up for the invoking operator (no marker entry)
- **THEN** the CLI exits non-zero with a friendly message directing the user to run `sudo sandbox setup`, naming neither the marker nor `setup-state.json`

### Requirement: Automated AI Handover
The system SHALL deliver an interactive shell session on **core** (as `agent`) to the operator after containers are confirmed healthy. The handover uses the canonical `cli-attach` invocation (`tlog-rec → ssh → ProxyCommand → /fwd`), landing the operator in `/workspaces/<ws>` via the ssh remote command suffix.

#### Scenario: PTY Execution Bounding
- **WHEN** `docker compose up --wait` returns successfully (all healthchecks pass)
- **THEN** `state.lock` is released and the CLI executes the canonical `tlog-rec → ssh → ProxyCommand → /fwd` invocation defined by `cli-attach`'s "Terminal handed to core via ssh-through-admin" scenario, transferring terminal ownership to core's interactive shell session as `agent`

### Requirement: Rich Markup Safety in Console Output

The CLI SHALL NOT pass user-supplied or config-derived content (paths, workspace names, instance names, section names like `[host]` / `[workspaces]`, environment values, error message fragments sourced from external libraries) through Rich's markup parser unescaped. Any `console.print(...)` call whose message contains literal `[<token>]` or `[/<token>]` characters where `<token>` is not a Rich style token (color name, style keyword, or composition thereof) SHALL either:

- pass `markup=False` as a keyword argument to suppress markup parsing for that call entirely; OR
- wrap the user/config-derived fragment in `rich.markup.escape(...)` before interpolation.

This requirement applies to all `console.print` invocations across `src/cli/` and `src/core/`, including those nested inside f-strings and `Text(...)` constructions. The convention exists because Rich's markup parser silently consumes unrecognized `[token]` sequences as style tags, producing messages with missing words and double-spacing — a defect class that historically corrupted operator-facing diagnostic messages (e.g., `[host]`, `[workspaces]` references in error text).

The codebase SHALL include a regression test (e.g., `tests/unit/cli/test_markup_safety.py`) that walks `src/cli/` and `src/core/` Python source via the `ast` module, identifies every `console.print` call, extracts string-literal arguments (including `Constant` strings and `JoinedStr`/f-string component literals), greps each for `\[([a-zA-Z_][\w]*( [a-zA-Z_][\w]*)*|/[a-zA-Z_]*)\]` matches, and asserts each match is either (a) a token in an enumerated allowlist of Rich style tokens, or (b) accompanied by a `markup=False` keyword argument on the same `console.print` call. The allowlist SHALL be defined in the test file as a module-level frozenset and SHALL initially contain at least: standard Rich color names (`red`, `green`, `yellow`, `blue`, `cyan`, `magenta`, `white`, `black`, `bright_red`, `bright_green`, `bright_yellow`); style keywords (`bold`, `dim`, `italic`, `underline`, `reverse`, `blink`); observed combinations (`red bold`, `green bold`, `yellow bold`); and closing forms (`/`, `/red`, `/green`, `/yellow`, `/bold`, `/dim`). The allowlist MAY be extended as new styles enter the codebase (one-line additions reviewed in the same PR that introduces the style).

#### Scenario: Literal section name in error message uses markup=False
- **WHEN** a `console.print` call emits a message containing the literal `[host]` (e.g., diagnostic text referencing the `[host]` section of `sandbox-ai.toml`)
- **THEN** the call passes `markup=False`, OR the bracketed fragment is wrapped via `rich.markup.escape("[host]")`; the user sees the literal `[host]` rendered, not a missing token

#### Scenario: User-supplied workspace name interpolation uses escape
- **WHEN** a `console.print` f-string interpolates a workspace name that may contain characters Rich treats as style tokens (e.g., `f"Workspace [{ws.name}]: ..."`)
- **THEN** either `ws.name` is passed through `rich.markup.escape(ws.name)` before interpolation, OR the call passes `markup=False`

#### Scenario: Markup-safety regression test catches new offenders
- **WHEN** a developer adds a new `console.print(f"some text [{user_value}] more text")` call to `src/cli/` or `src/core/` without `markup=False` and without `rich.markup.escape`
- **THEN** the markup-safety test fails, identifying the file, line, and offending bracket token; the test passes only after the developer either escapes the value, passes `markup=False`, or adds the token to the allowlist with reviewer approval

#### Scenario: Allowlist covers genuine style tokens
- **WHEN** `console.print` calls use Rich style markup like `[red]`, `[bold]`, `[/red]`, `[green bold]`
- **THEN** the markup-safety test passes for these calls because the bracketed tokens are in the enumerated allowlist; no `markup=False` is required for genuine style usage

### Requirement: `sandbox setup` CLI Subcommand

The CLI SHALL expose a `setup` subcommand invoked as `sudo sandbox setup`. The subcommand SHALL be implemented in `src/cli/main.py` (alongside the existing `init`, `start`, `stop`, `attach`, `destroy`, `status`, `doctor`, `workspace ...` subcommands). The subcommand SHALL require root (`os.geteuid() == 0`); if invoked without root, exit with `sandbox setup must be run as root. Re-invoke as: sudo sandbox setup`.

The full flag surface:

| Flag | Behavior |
|---|---|
| `--operator <name>` | escape hatch for non-`sudo` privilege escalation paths; takes precedence over `$SUDO_USER` |
| `--dry-run` | run plan pass only; no mutations; exit code 0 regardless of plan content |
| `--yes` / `-y` | non-interactive apply (skip the TTY confirm prompt) |
| `--update-runsc` | run ONLY L6a phase against the current pinned registry, ignoring "already installed" skip |
| `--enable-fapolicyd-integration` | install fapolicyd (via distro package manager; verify-only refuses if absent) + register sandbox-ai binaries in its trust DB |
| `--enable-aide-integration` | drop `/etc/aide/aide.conf.d/sandbox-ai.conf` listing sandbox-ai binaries; run `aide --init` if needed |

The subcommand SHALL emit doctor-style output to stdout (plan pass + apply pass + finalization summary), per the `sandbox-setup` capability's plan/apply UX requirements. The subcommand SHALL exit 0 on full success; non-zero on any phase failure or rollback.

#### Scenario: setup subcommand requires root
- **WHEN** `sandbox setup` is invoked without root (`os.geteuid() != 0`)
- **THEN** the CLI exits non-zero with `sandbox setup must be run as root. Re-invoke as: sudo sandbox setup`

#### Scenario: --operator precedence no longer consults PKEXEC_UID
- **WHEN** `sudo sandbox setup --operator bob` runs
- **THEN** `bob` takes precedence over `$SUDO_USER`; `$PKEXEC_UID` is not consulted in the operator precedence (the vestigial polkit tier is retired)

### Requirement: Setup Bypasses Runtime Sudoers Rule

Setup SHALL invoke machinectl via `[*machinectl_cmd(...), "/bin/bash", "-c", "<cmd>"]` directly (no dispatcher routing, no sudoers rule required), because setup runs as root and root MUST cross the privilege boundary into the sandbox user's session without prior authentication setup (per V8 empirical validation). The orchestrator's runtime sudoers rule (installed by setup's L3 phase) SHALL apply only to the operator's NON-root invocations of `sandbox start`, `sandbox doctor`, etc., AFTER setup completes.

The convention meta-test from the sibling capability (`runtime-dispatcher`) SHALL NOT reject setup's phase implementations: setup's modules under `src/core/setup/*.py` MAY import `machinectl_cmd` directly. This change SHALL NOT amend or edit the meta-test. `runtime-dispatcher`'s `host-config` capability already defines `src/core/setup/*.py` as one of the three documented allowlist categories (a forward reference it ships deliberately, since it lands first per the integration order). Setup modules pass the convention check by matching that pre-existing bounded glob — single source of truth (the meta-test, owned by `runtime-dispatcher`); this change adds modules the already-installed rule permits, not an allowlist edit. (Phase-3 review B-4: an earlier draft here said the allowlist "SHALL be amended", contradicting `runtime-dispatcher`'s then-"two-entry, no globs" wording; reconciled by `runtime-dispatcher` owning the complete three-category allowlist up front.)

#### Scenario: Setup phase modules call machinectl_cmd directly
- **WHEN** a setup phase module (e.g., `src/core/setup/l5_dockerd.py`) needs to invoke `dockerd-rootless-setuptool.sh install` via machinectl
- **THEN** the module imports `machinectl_cmd` from `core.host_config` and constructs the argv directly (no `core.dispatch` routing); the meta-test does not flag it because its path matches the `src/core/setup/*.py` allowlist category already defined by `runtime-dispatcher`

#### Scenario: This change makes no edit to the convention meta-test
- **WHEN** this change's diff is reviewed
- **THEN** it contains NO modification to `tests/unit/test_conventions.py::test_machinectl_cmd_callers_restricted`; setup modules pass solely by matching the pre-existing `src/core/setup/*.py` allowlist category. The effective allowlist at runtime is `{"src/core/host_config.py", "src/core/dispatch.py"} ∪ {paths matching "src/core/setup/*.py"}`, but that union is defined entirely by `runtime-dispatcher` — this change neither widens nor restates it.

