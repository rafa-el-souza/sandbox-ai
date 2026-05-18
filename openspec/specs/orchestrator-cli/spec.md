## Purpose

This specification defines the deterministic execution constraints bounding the Python CLI architecture. It dictates the invariant orchestration logic required to safely bootstrap the environment, isolate Host terminal contexts from unprivileged Daemon payloads utilizing `machinectl`, and manage asynchronous AI agent handovers strictly within POSIX containment parameters.
## Requirements
### Requirement: Python CLI Orchestrator Execution
The system SHALL execute utilizing a strict Python `typer` interface to deterministically govern the `sandbox` operational lifecycle across the following commands: `init`, `start`, `stop`, `attach`, `destroy`, `doctor`, `status`, and the `workspace` subcommand group (`workspace add | remove | rename | restore | list`). Lifecycle commands (`start`, `stop`, `attach`, `destroy`, `status`, `workspace ...`) take an explicit `<inst>` argument; CWD-based discovery is removed.

#### Scenario: Tooling Plane Bootstrapping
- **WHEN** the orchestrator initiates execution on a fresh host machine
- **THEN** the operator runs `sandbox init <inst>`, which seeds `<sandbox_ai_home()>/config/sandbox-ai.toml` (TTY prompt or non-TTY fail), creates the per-user state tree at `<sandbox_ai_home()>/{config,state,instances,workspaces}/`, scaffolds the per-instance directory tree under `<sandbox_ai_home()>/instances/<inst>/`, scaffolds at least one workspace tree under `<sandbox_ai_home()>/workspaces/<inst>/<ws>/`, and writes the `.initialized` sentinel

#### Scenario: Agent Startup Sequence
- **WHEN** the human operator executes `sandbox start <inst>`
- **THEN** the CLI verifies the `.initialized` sentinel exists, runs doctor Chain 1 pre-flight (including bridge-group existence and dev supplementary-group membership), validates secret completeness, then performs a pre-lock warm state check, acquires `state.lock` and the IPAM lock, allocates a `/24` subnet quintuple, and executes the ownership-sensitive phase order owned by `orchestrator-volumes`'s `Phase Order Contract for Ownership-Sensitive Phases`: `_phase_workspace_shared_group` (per-workspace chgrp/chmod 2770/setgid + persistent default ACL — runs BEFORE named-ACL grants so chmod 2770 lands on a non-extended-ACL inode), `_phase_acl_grant` (per-workspace named-ACL grants + instance-dir set + helper-recipe parent grants), `_phase_credentials` (SSH keypair + bcrypt htpasswd generation into `secrets/`), `_phase_hydrate` (Pydantic + Jinja2 hydration with multi-workspace fan-out in compose template volumes), `_phase_grant_post_hydrate_daemon_read` (unified setfacl-as-owner pass over `RO_FILE_RECIPES + EXEC_FILE_RECIPES + RW_FILE_RECIPES + DAEMON_READ_DIRECT_FILES`), the helper-recipe phases (`_phase_helper_mkdir_chown_cache_log` for cache/log subuid-chown; `_phase_helper_cp_chown_ro_files` for ro/exec/rw consumer-uid-0-chown), and finally `_phase_compose_up` (`docker compose up -d --build --wait` via `machinectl`); progress is displayed for each phase. On all containers healthy, the CLI defaults to handing the terminal over to **core** (as `agent`) via the `cli-attach` command shape (`tlog-rec → ssh → ProxyCommand → /fwd`) when stdin is a TTY (suppress with `--no-handover`; per `cli-start`'s `Handover Default Direction` and `Handover TTY Autodetect`).

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
The system SHALL isolate all Docker command execution across the `dev`/`sandbox` privilege boundary using `machinectl shell <docker_unprivileged_user>@.host` for PTY-needing paths and `pipe_cmd` (`systemd-run -q --pipe --uid=<docker_unprivileged_user>`) for byte-pipe paths. The machinectl invocation prefix SHALL be determined by the `machinectl_authentication` setting from host config (`sandbox-ai.toml`). When `machinectl_authentication` is `"sudo"`, all machinectl commands SHALL be prefixed with `sudo`. When `machinectl_authentication` is `"polkit"`, machinectl commands SHALL be invoked directly without `sudo`, relying on D-Bus native polkit authorization via `org.freedesktop.machine1.shell`. The centralized `machinectl_cmd()` builder from `core.host_config` SHALL be called only by the three documented allowlist categories defined in the `host-config` capability: `src/core/host_config.py` (self), `src/core/dispatch.py` (the typed dispatcher's orchestration module), and `src/core/setup/*.py` (the setup-phase package — sister change `sandbox-setup`; setup runs as root and crosses the boundary before the dispatcher is installed, so it cannot route through `core.dispatch`). Every other module crossing the boundary SHALL route through `core.dispatch.invoke(op, args, host_config)`. Byte-pipe call sites (notably the operator-handover `ProxyCommand`) SHALL use `pipe_cmd()` from `core.host_config`; `pipe_cmd` is polkit-authenticated via the `manage-units` action and is NEVER prefixed with `sudo` regardless of `machinectl_authentication` mode.

The orchestrator's non-interactive Docker invocations SHALL take the shape `[*machinectl_cmd(user, auth), "/bin/bash", "-c", f"/usr/local/libexec/sandbox-ai/dispatch {op} {shlex.join(args)}"]` rather than embedding inline bash. The outer `/bin/bash -c` wrapper is retained permanently: it is what bridges the orchestrator's argv (composed on the operator's host) into a bash environment inside the `docker_unprivileged_user` session, where the dispatcher binary at `/usr/local/libexec/sandbox-ai/dispatch` is reachable. This shape is also backward-compatible with operators' manually-installed legacy NOPASSWD sudoers rules whose Cmnd_Spec ends in `/bin/bash -c *` (the trailing `*` swallows the dispatcher command as one bash command), so this capability can ship before the sister capability `sandbox-setup`. The sister capability writes a narrower sudoers RULE shape — per-op `Cmnd_Alias` with the full invocation prefix and backslash-escaped whitespace, validated empirically by V9 (`openspec/explorations/ongoing/sandbox-setup/validation.md`) and grounded in finding F-004 (sudoers-cmnd-spec-quoting-not-shell-quoting); the orchestrator's invocation shape defined here is forward-compatible with that V9 rule shape (the V9 rule's per-op `Cmnd_Spec` matches argv-by-argv against this exact invocation), so operators upgrading via `sandbox setup` continue working without any orchestrator-side change. The dispatcher binary at `/usr/local/libexec/sandbox-ai/dispatch` is `root:root` mode `0755` on disk (with `chattr +i`), but EXECUTES at runtime as `[host].docker_unprivileged_user` — the `machinectl shell <user>@.host` crossing drops root before bash execs the dispatcher.

`machinectl_cmd()` deliberately emits the **relative** command name `machinectl` (`["sudo", "machinectl", "shell", …]`), not an absolute path — this is intentional and unchanged by this capability. Under SUDO auth mode, `sudo` resolves that relative `machinectl` against the sudoers `secure_path` and matches the *resolved absolute path* against the rule's `Cmnd_Spec`. Making the orchestrator's relative emission and the sister rule's absolute Cmnd_Spec coincide is owned entirely by `sandbox-setup` (it resolves `machinectl` on the same secure_path basis at L0, pins that path in the rule, verifies the bridge per-host with a relative-form L3a probe, and re-checks it via `setup_invariants`). This capability asserts only that the runtime invocation uses relative `machinectl`; it does NOT pin an absolute path (doing so would be scope creep into the load-bearing `machinectl_cmd()` primitive and every one of its callers). See `sandbox-setup` design B-3 and validation track V9e.

#### Scenario: Non-Interactive Daemon Interaction (sudo mode)
- **WHEN** the Python orchestrator needs to execute a non-interactive Docker command and `machinectl_authentication` is `"sudo"`
- **THEN** it invokes (via `core.dispatch`): `subprocess.run(["sudo", "machinectl", "shell", "<user>@.host", "/bin/bash", "-c", "/usr/local/libexec/sandbox-ai/dispatch <op> <quoted args>"])` with `capture_output=True`; `<op>` is one of the ten ops enumerated by `runtime-dispatcher`; `<quoted args>` is `shlex.join` of the op's validated args

#### Scenario: Non-Interactive Daemon Interaction (polkit mode)
- **WHEN** the Python orchestrator needs to execute a non-interactive Docker command and `machinectl_authentication` is `"polkit"`
- **THEN** it invokes (via `core.dispatch`): `subprocess.run(["machinectl", "shell", "<user>@.host", "/bin/bash", "-c", "/usr/local/libexec/sandbox-ai/dispatch <op> <quoted args>"])` with `capture_output=True`

#### Scenario: Interactive PTY Execution (sudo mode)
- **WHEN** the orchestrator hands the terminal to core and `machinectl_authentication` is `"sudo"`
- **THEN** it invokes: `subprocess.run(["tlog-rec", "--writer=file", "--file-path=<host-side log path>", "--", "ssh", "-i", "<inst_dir>/secrets/ipc_ssh_key", "-o", "UserKnownHostsFile=<inst_dir>/secrets/ipc_known_hosts", "-o", "StrictHostKeyChecking=yes", "-o", "ProxyCommand=systemd-run -q --pipe --uid=<docker_unprivileged_user> /usr/bin/docker exec -i <inst>-admin-1 /fwd <core_ipc_ip>:9999", "-p", "9999", "-t", "agent@<core_ipc_ip>", "cd /workspaces/<ws> && exec bash -l"])` with stdin/stdout/stderr inherited; the `ProxyCommand` is NOT prefixed with `sudo` even in sudo mode because it uses `pipe_cmd` (polkit), not `machinectl_cmd`; this invocation does NOT route through `core.dispatch` because it is the operator-handover path (interactive PTY), not a non-interactive Docker command

#### Scenario: Interactive PTY Execution (polkit mode)
- **WHEN** the orchestrator hands the terminal to core and `machinectl_authentication` is `"polkit"`
- **THEN** it invokes the same argv as the sudo-mode scenario above (byte-identical), because the `ProxyCommand` uses `pipe_cmd` (polkit) regardless of `machinectl_authentication`

#### Scenario: Dispatcher-only routing convention
- **WHEN** any module in `src/` outside the three `host-config` allowlist categories (i.e. not `src/core/host_config.py`, not `src/core/dispatch.py`, not a `src/core/setup/*.py` setup-phase module) builds a cross-boundary invocation
- **THEN** it MUST call `core.dispatch.invoke(op, args, host_config)` (or an equivalent dispatch helper) rather than constructing the argv list with `machinectl_cmd(...)` directly; the convention meta-test in `tests/unit/test_conventions.py` enforces this at gate time

### Requirement: Host Config Loading in CLI Commands
All post-init CLI commands (`start`, `stop`, `attach`, `destroy`, `status`, all `workspace ...` subcommands) SHALL load per-host config from `<sandbox_ai_home()>/config/sandbox-ai.toml` via `HostConfig.from_toml()` and read `docker_unprivileged_user`, `machinectl_authentication`, and `workspace_bridge_group` from it. The canonical path is resolved internally; CWD is no longer consulted.

#### Scenario: Post-init command reads host config
- **WHEN** any post-init command runs and the canonical `sandbox-ai.toml` exists
- **THEN** `docker_unprivileged_user`, `machinectl_authentication`, and `workspace_bridge_group` are sourced from the `[host]` section

#### Scenario: Post-init command fails without host config
- **WHEN** any post-init command runs and the canonical `sandbox-ai.toml` is absent
- **THEN** the CLI exits with an error directing the user to run `sandbox init`

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

