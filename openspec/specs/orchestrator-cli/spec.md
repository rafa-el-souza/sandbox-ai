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
- **THEN** the CLI verifies the `.initialized` sentinel exists, runs doctor Chain 1 pre-flight, validates secret completeness, then performs a pre-lock warm state check, acquires `state.lock` and the IPAM lock, allocates a `/24` subnet triple, runs the Pydantic + Jinja2 hydration pipeline, applies ACL grants, executes `docker compose up -d --build --wait` via `machinectl`, and hands the terminal over to the admin container via `docker exec -it`, displaying progress for each phase

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
The system SHALL isolate all Docker command execution across the `dev`/`sandbox` privilege boundary using `sudo machinectl shell <host_unprivileged_user>@.host`.

#### Scenario: Non-Interactive Daemon Interaction
- **WHEN** the Python orchestrator needs to execute a non-interactive Docker command (e.g., `docker compose up`)
- **THEN** it invokes: `subprocess.run(["sudo", "machinectl", "shell", "<user>@.host", "/bin/bash", "-c", "<command>"])` with `capture_output=True`

#### Scenario: Interactive PTY Execution
- **WHEN** the orchestrator hands the terminal to the admin container
- **THEN** it invokes: `subprocess.run(["sudo", "machinectl", "shell", "<user>@.host", "/usr/bin/docker", "exec", "-it", "<name>-admin-1", "zsh"])` with stdin/stdout/stderr inherited (no `-c` wrapper, PTY allocated through machinectl directly)

### Requirement: Automated AI Handover
The system SHALL deliver an interactive shell session in the admin container to the operator after containers are confirmed healthy, optionally auto-starting the agent if a warmup prompt is configured.

#### Scenario: PTY Execution Bounding
- **WHEN** `docker compose up --wait` returns successfully (all healthchecks pass)
- **THEN** `state.lock` is released and the CLI executes `docker exec -it <name>-admin-1 zsh` via machinectl, transferring terminal ownership to the admin container shell session

#### Scenario: Warmup Prompt Injection
- **WHEN** `sandbox.toml` declares a non-empty `project.warmup_prompt`
- **THEN** the `docker exec` call includes `-e SANDBOX_WARMUP_PROMPT="<value>"` and the admin container's `.zshrc` reads this env var on init to invoke `claude -p "<value>" --dangerously-skip-permissions` before dropping to an interactive prompt
