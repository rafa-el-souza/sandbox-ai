## Purpose

This specification defines the deterministic execution constraints bounding the Python CLI architecture. It dictates the invariant orchestration logic required to safely bootstrap the environment, isolate Host terminal contexts from unprivileged Daemon payloads utilizing `machinectl`, and manage asynchronous AI agent handovers strictly within POSIX containment parameters.

## Requirements

### Requirement: Python CLI Orchestrator Execution
The system SHALL execute utilizing a strict Python `typer` interface to deterministically govern the `sandbox` operational lifecycle across seven commands: `init`, `start`, `stop`, `attach`, `destroy`, `doctor`, and `status`.

#### Scenario: Tooling Plane Bootstrapping
- **WHEN** the orchestrator initiates execution on a fresh host machine
- **THEN** the operator runs `sandbox init --user <user>` which resolves `SANDBOX_AI_HOME` from the location of the orchestrator source, scaffolds the per-instance directory tree under `SANDBOX_AI_HOME/sandboxes/<project_name>-<project_id>/`, and writes the `.initialized` sentinel

#### Scenario: Agent Startup Sequence
- **WHEN** the human operator executes `sandbox start`
- **THEN** the CLI verifies the `.initialized` sentinel exists, runs doctor Chain 1 pre-flight, validates secret completeness, then performs a pre-lock warm state check, acquires `state.lock` and the IPAM lock, allocates a `/24` subnet septuple, runs the Pydantic + Jinja2 hydration pipeline, applies ACL grants, executes `docker compose up -d --build --wait` via `machinectl`, and hands the terminal over to the admin container via `docker exec -it`, displaying progress for each phase

#### Scenario: Instance State Query
- **WHEN** the human operator executes `sandbox status`
- **THEN** the CLI displays a Rich-formatted dashboard with instance identity, container health, IPAM allocation, and config completeness warnings

#### Scenario: Graceful Teardown
- **WHEN** the human operator executes `sandbox stop`
- **THEN** the CLI terminates running containers via `docker compose down` (or `down -v` with `--clean`), then revokes the `sandbox` user's ACL entries on `sandboxes/<id>/docker/` and `sandboxes/<id>/config/`

#### Scenario: Concurrency Collision Mitigation
- **WHEN** a background process or secondary terminal concurrently executes `sandbox start` on an initializing repository
- **THEN** the native `fcntl` filesystem lock rejects the OS request, forcing the Orchestrator to gracefully abort the execution loop before triggering a Docker socket race condition.

#### Scenario: Standardized Module Execution
- **WHEN** the Orchestrator triggers via a pure `python -m cli` boundary execution
- **THEN** the system rigidly interprets a `cli/__main__.py` block seamlessly mapping execution arrays backwards explicitly into the `cli/main.py` entrypoint schema.

#### Scenario: Opaque Error Bounds Trace
- **WHEN** the orchestrator fails POSIX boundaries triggering native `CalledProcessError` exceptions
- **THEN** the system raises a `SandboxExecutionError` masking host topology variables and rendering clinical error messages identifying the failed command without leaking environment state.


### Requirement: Sub-Process Privilege Bounding
The system SHALL isolate all Docker command execution across the `dev`/`sandbox` privilege boundary using `machinectl shell <docker_unprivileged_user>@.host`. The machinectl invocation prefix SHALL be determined by the `machinectl_authentication` setting from project config (`sandbox-ai.toml`). When `machinectl_authentication` is `"sudo"`, all machinectl commands SHALL be prefixed with `sudo`. When `machinectl_authentication` is `"polkit"`, machinectl commands SHALL be invoked directly without `sudo`, relying on D-Bus native polkit authorization via `org.freedesktop.machine1.shell`. All call sites SHALL use the centralized `machinectl_cmd()` builder from `core.project_config`.

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

### Requirement: Project Config Loading in CLI Commands
All post-init CLI commands (`start`, `stop`, `attach`, `destroy`, `status`) SHALL load project-wide config from `sandbox-ai.toml` via `ProjectConfig.from_toml(project_dir)` and read `docker_unprivileged_user` and `machinectl_authentication` from it. The `project_dir` SHALL be resolved from CWD via `_resolve_project_dir()`.

#### Scenario: Post-init command reads project config
- **WHEN** any post-init command runs and `sandbox-ai.toml` exists in the project directory
- **THEN** `docker_unprivileged_user` and `machinectl_authentication` are sourced from the `[host]` section

#### Scenario: Post-init command fails without project config
- **WHEN** any post-init command runs and `sandbox-ai.toml` does not exist in the project directory
- **THEN** the CLI exits with an error directing the user to create `sandbox-ai.toml`

### Requirement: Automated AI Handover
The system SHALL deliver an interactive shell session in the admin container to the operator after containers are confirmed healthy, optionally auto-starting the agent if a warmup prompt is configured.

#### Scenario: PTY Execution Bounding
- **WHEN** `docker compose up --wait` returns successfully (all healthchecks pass)
- **THEN** `state.lock` is released and the CLI executes `docker exec -it <name>-admin-1 zsh` via machinectl, transferring terminal ownership to the admin container shell session

#### Scenario: Warmup Prompt Injection
- **WHEN** `sandbox.toml` declares a non-empty `project.warmup_prompt`
- **THEN** the `docker exec` call includes `-e SANDBOX_WARMUP_PROMPT="<value>"` and the admin container's `.zshrc` reads this env var on init to invoke `claude -p "<value>" --dangerously-skip-permissions` before dropping to an interactive prompt
