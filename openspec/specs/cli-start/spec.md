## Purpose

This specification defines the `sandbox start` command lifecycle, governing pre-lock warm state detection, concurrency lock acquisition, IPAM allocation, blocking healthcheck wait, and PTY handover.

## Requirements

### Requirement: Pre-Lock Warm State Detection
The system SHALL check whether the sandbox instance's containers are already running before acquiring any concurrency locks.

#### Scenario: Already-running instance exits without lock contention
- **WHEN** `sandbox start` is invoked and `docker compose ps -q` returns non-empty output
- **THEN** the CLI exits with "Sandbox '<name>' is already running. Use 'sandbox attach' to reconnect." before acquiring `state.lock` or the IPAM lock

### Requirement: Concurrency Lock Acquisition
The system SHALL acquire a per-instance `state.lock` (fcntl `LOCK_EX | LOCK_NB`) before modifying any instance state.

#### Scenario: Concurrent start rejected
- **WHEN** a second `sandbox start` is invoked for the same instance while the first is still in progress
- **THEN** the second invocation exits with: "Another sandbox start is already in progress for this instance."

### Requirement: IPAM Allocation Before Launch
The system SHALL allocate a `/24` subnet triple from the IPAM ledger and derive all static IPs before invoking `docker compose up`.

#### Scenario: New instance gets lowest available slot
- **WHEN** `sandbox start` scaffolds a new instance with no existing IPAM entry
- **THEN** the lowest available `base_index` (0–13311) is assigned and written to `ipam.json` before any compose command runs

#### Scenario: Existing instance reuses previous slot
- **WHEN** `sandbox start` is invoked for an instance already present in `ipam.json`
- **THEN** the same `base_index` from the ledger is used without modifying the ledger

### Requirement: Blocking Healthcheck Wait
The system SHALL block launch until all services with defined healthchecks report healthy, using `docker compose up -d --build --wait`.

#### Scenario: Healthy services allow handover
- **WHEN** all containers with healthchecks report healthy status
- **THEN** `docker compose up --wait` returns and the CLI proceeds to the handover phase

#### Scenario: Unhealthy launch releases lock and exits
- **WHEN** one or more containers fail their healthcheck within the timeout period
- **THEN** the CLI emits a service health summary, releases `state.lock`, and exits with a non-zero code

### Requirement: PTY Handover via machinectl
The system SHALL hand over the terminal to the admin container via `machinectl shell` + `docker exec -it`, releasing `state.lock` before the exec call.

#### Scenario: Terminal handed to admin container
- **WHEN** containers are healthy and `state.lock` is released
- **THEN** `sudo machinectl shell <host_unprivileged_user>@.host /usr/bin/docker exec -it <name>-admin-1 zsh` is executed and the user's terminal is now owned by that session

#### Scenario: Warmup prompt injected at exec time
- **WHEN** `sandbox.toml` declares a non-empty `project.warmup_prompt`
- **THEN** the `docker exec` call includes `-e SANDBOX_WARMUP_PROMPT="<value>"` and the admin container's `.zshrc` reads this variable on shell init to auto-invoke claude
