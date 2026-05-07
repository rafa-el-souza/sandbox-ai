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
- **THEN** the CLI verifies the `.initialized` sentinel exists, runs doctor Chain 1 pre-flight (including bridge-group existence and dev supplementary-group membership), validates secret completeness, then performs a pre-lock warm state check, acquires `state.lock` and the IPAM lock, allocates a `/24` subnet septuple, runs the Pydantic + Jinja2 hydration pipeline (with multi-workspace fan-out in compose template volumes), applies ACL grants per-workspace, executes the helper-recipe phases (cache/log, ro-files, per-workspace shared-group), executes `docker compose up -d --build --wait` via `machinectl`, and hands the terminal over to the admin container via `docker exec -it`, displaying progress for each phase

#### Scenario: Instance State Query
- **WHEN** the human operator executes `sandbox status [<inst>]`
- **THEN** the CLI displays a Rich-formatted dashboard. With `<inst>` argument: instance identity, container health, IPAM allocation, config completeness warnings, and a Workspaces section listing each workspace with its mode, path, and state (per `cli-status`). Without argument: a summary table of all registered instances.

#### Scenario: Graceful Teardown
- **WHEN** the human operator executes `sandbox stop <inst>`
- **THEN** the CLI terminates running containers via `docker compose down` (or `down -v` with `--clean`), then revokes the `sandbox` user's named-ACL entries on the instance dir set (`<sandbox_ai_home()>/instances/<inst>/`, plus `docker/`, `config/`, `secrets/`, `.sandbox.env`) AND on each workspace's path (effective + default-ACL named-entry portion, per `cli-stop`)

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
The system SHALL isolate all Docker command execution across the `dev`/`sandbox` privilege boundary using `machinectl shell <docker_unprivileged_user>@.host`. The machinectl invocation prefix SHALL be determined by the `machinectl_authentication` setting from host config (`sandbox-ai.toml`). When `machinectl_authentication` is `"sudo"`, all machinectl commands SHALL be prefixed with `sudo`. When `machinectl_authentication` is `"polkit"`, machinectl commands SHALL be invoked directly without `sudo`, relying on D-Bus native polkit authorization via `org.freedesktop.machine1.shell`. All call sites SHALL use the centralized `machinectl_cmd()` builder from `core.host_config`.

#### Scenario: Non-Interactive Daemon Interaction (sudo mode)
- **WHEN** the Python orchestrator needs to execute a non-interactive Docker command and `machinectl_authentication` is `"sudo"`
- **THEN** it invokes: `subprocess.run(["sudo", "machinectl", "shell", "<user>@.host", "/bin/bash", "-c", "<command>"])` with `capture_output=True`

#### Scenario: Non-Interactive Daemon Interaction (polkit mode)
- **WHEN** the Python orchestrator needs to execute a non-interactive Docker command and `machinectl_authentication` is `"polkit"`
- **THEN** it invokes: `subprocess.run(["machinectl", "shell", "<user>@.host", "/bin/bash", "-c", "<command>"])` with `capture_output=True`

#### Scenario: Interactive PTY Execution (sudo mode)
- **WHEN** the orchestrator hands the terminal to the admin container and `machinectl_authentication` is `"sudo"`
- **THEN** it invokes: `subprocess.run(["sudo", "machinectl", "shell", "<user>@.host", "/usr/bin/docker", "exec", "-it", "<name>-admin-1", "zsh"])` with stdin/stdout/stderr inherited

#### Scenario: Interactive PTY Execution (polkit mode)
- **WHEN** the orchestrator hands the terminal to the admin container and `machinectl_authentication` is `"polkit"`
- **THEN** it invokes: `subprocess.run(["machinectl", "shell", "<user>@.host", "/usr/bin/docker", "exec", "-it", "<name>-admin-1", "zsh"])` with stdin/stdout/stderr inherited

### Requirement: Host Config Loading in CLI Commands
All post-init CLI commands (`start`, `stop`, `attach`, `destroy`, `status`, all `workspace ...` subcommands) SHALL load per-host config from `<sandbox_ai_home()>/config/sandbox-ai.toml` via `HostConfig.from_toml()` and read `docker_unprivileged_user`, `machinectl_authentication`, and `workspace_bridge_group` from it. The canonical path is resolved internally; CWD is no longer consulted.

#### Scenario: Post-init command reads host config
- **WHEN** any post-init command runs and the canonical `sandbox-ai.toml` exists
- **THEN** `docker_unprivileged_user`, `machinectl_authentication`, and `workspace_bridge_group` are sourced from the `[host]` section

#### Scenario: Post-init command fails without host config
- **WHEN** any post-init command runs and the canonical `sandbox-ai.toml` is absent
- **THEN** the CLI exits with an error directing the user to run `sandbox init`

### Requirement: Automated AI Handover
The system SHALL deliver an interactive shell session in the admin container to the operator after containers are confirmed healthy, optionally auto-starting the agent if a warmup prompt is configured.

#### Scenario: PTY Execution Bounding
- **WHEN** `docker compose up --wait` returns successfully (all healthchecks pass)
- **THEN** `state.lock` is released and the CLI executes `docker exec -it <name>-admin-1 zsh` via machinectl, transferring terminal ownership to the admin container shell session

#### Scenario: Warmup Prompt Injection
- **WHEN** `sandbox.toml` declares a non-empty `instance.warmup_prompt`
- **THEN** the `docker exec` call includes `-e SANDBOX_WARMUP_PROMPT="<value>"` and the admin container's `.zshrc` reads this env var on init to invoke `claude -p "<value>" --dangerously-skip-permissions` before dropping to an interactive prompt
