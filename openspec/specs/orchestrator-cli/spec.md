## Purpose

This specification defines the deterministic execution constraints bounding the Python CLI architecture. It dictates the invariant orchestration logic required to safely bootstrap the environment, isolate Host terminal contexts from unprivileged Daemon payloads utilizing `machinectl`, and manage asynchronous AI agent handovers strictly within POSIX containment parameters.

## Requirements

### Requirement: Python CLI Orchestrator Execution
The system SHALL execute utilizing a strict Python `typer` interface to deterministically govern the `sandbox` operational lifecycle.

#### Scenario: Tooling Plane Bootstrapping
- **WHEN** the Orchestrator initiates execution on a fresh host machine
- **THEN** it mechanically intercepts the pipeline to duplicate its internal `docker/` and `config/` array templates into the global `~/.sandbox/` Tooling Plane before proceeding, preserving Git version-control natively.

#### Scenario: Agent Startup Sequence
- **WHEN** the human operator executes `$ sandbox start`
- **THEN** the CLI acquires exclusive concurrency locks on both `./.sandbox/state.lock` AND the global IPAM ledger, determines network bindings, parses the `ruamel.yaml` stack, and physically prioritizes Docker Compose ingestion inside the daemon.

#### Scenario: Graceful Teardown Cleanses
- **WHEN** the human operator executes `$ sandbox stop`
- **THEN** the CLI natively terminates running container meshes, aggressively stripping active default ACL paths (`setfacl -R -x u:sandbox`) and purging ephemeral `.env` credentials without triggering a full volume overwrite.

#### Scenario: Concurrency Collision Mitigation
- **WHEN** a background process or secondary terminal concurrently executes `$ sandbox start` on an initializing repository
- **THEN** the native `fcntl` filesystem lock rejects the OS request, forcing the Orchestrator to gracefully abort the execution loop before triggering a Docker socket race condition.

#### Scenario: Standardized Module Execution
- **WHEN** the Orchestrator triggers via a pure `python -m cli` boundary execution
- **THEN** the system rigidly interprets a `cli/__main__.py` block seamlessly mapping execution arrays backwards explicitly into the `cli/main.py` entrypoint schema.

#### Scenario: Opaque Error Bounds Trace
- **WHEN** the orchestrator fails POSIX boundaries triggering native `CalledProcessError` exceptions
- **THEN** the system violently strips Python stack trace limits natively raising a mathematical `SandboxExecutionError` strictly masking host variables and safely rendering clinical Typer warnings pointing explicitly at `./.sandbox/logs/orchestrator/orchestrator.log` securely.


### Requirement: Sub-Process Privilege Bounding
The system SHALL strictly isolate command payloads across `dev` and `sandbox` bounds utilizing `machinectl`.

#### Scenario: Daemon Interaction Crossing
- **WHEN** the Python Orchestrator needs to execute `docker compose up`
- **THEN** it strictly wraps the array inside an exclusive `subprocess.run(["sudo", "machinectl", "shell", "sandbox@...", "docker compose"])` IPC array.

### Requirement: Automated AI Handover
The system SHALL dictate Agent invocation asynchronously without trapping the human shell pipeline.

#### Scenario: PTY Execution Bounding
- **WHEN** the orchestrator initiates `docker compose wait` and the internal container healthchecks officially unblock the pipeline
- **THEN** the CLI natively dispatches a `tmux send-keys` buffer payload targeting the `admin` container to automatically drop the operator inside an active bash session.

#### Scenario: Warmup Prompt Injection
- **WHEN** the `.sandbox.toml` declares a valid `warmup_prompt` parameter string
- **THEN** the CLI inherently injects structured parameters `claude -p "..." --dangerously-skip-permissions` into the `tmux` mapping buffer, enforcing automation initialization bounds cleanly.
