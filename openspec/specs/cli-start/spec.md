## Purpose

This specification defines the `sandbox start` command lifecycle, governing pre-lock warm state detection, concurrency lock acquisition, IPAM allocation, blocking healthcheck wait, and PTY handover.

## Requirements

### Requirement: Pre-Lock Warm State Detection
The system SHALL check whether the sandbox instance's containers are already running before acquiring any concurrency locks. When `--dry-run` is passed, the system SHALL skip the warm state check and proceed directly to pipeline validation. The system SHALL require a prior `sandbox init` and reject start if no instance is registered.

#### Scenario: No instance found — error with guidance
- **WHEN** `sandbox start` is invoked and no instance is registered for the current project directory
- **THEN** the CLI exits with "No sandbox instance found. Run `sandbox init --user <user>` first." and exit code 1

#### Scenario: Partial init detected
- **WHEN** `sandbox start` is invoked and the registry contains an entry but `.initialized` sentinel is missing
- **THEN** the CLI exits with "Instance partially initialized. Run `sandbox destroy` then `sandbox init`." and exit code 1

#### Scenario: Already-running instance exits without lock contention
- **WHEN** `sandbox start` is invoked and `docker compose ps -q` returns non-empty output
- **THEN** the CLI exits with "Sandbox '<name>' is already running. Use 'sandbox attach' to reconnect." before acquiring `state.lock` or the IPAM lock

#### Scenario: Dry-run bypasses warm state check
- **WHEN** `sandbox start --dry-run` is invoked
- **THEN** the warm state check is skipped and the system proceeds to dry-run validation regardless of container state

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

### Requirement: Instance Pre-Flight Checks
The system SHALL validate instance readiness before beginning provisioning. Pre-flight includes sentinel verification, secret completeness, and doctor Chain 1 (Privilege Boundary) checks.

#### Scenario: Secret completeness gate
- **WHEN** `sandbox start` is invoked and `.sandbox.env` is missing a secret required by the current `sandbox.toml` config (e.g., `FIRECRAWL_API_KEY` when `mcp_firecrawl = true`)
- **THEN** the CLI lists the missing secrets, prints the path to `.sandbox.env`, and exits with code 1 before acquiring any locks

#### Scenario: Doctor Chain 1 pre-flight
- **WHEN** `sandbox start` is invoked
- **THEN** the system executes doctor Chain 1 checks (sudo, machinectl, user exists, systemd-machined, machinectl reachable, Docker available, Docker rootless, gVisor runsc) and aborts with diagnostic output if any check fails

#### Scenario: Pre-flight passes
- **WHEN** sentinel exists, all required secrets are populated, and all Chain 1 checks pass
- **THEN** provisioning proceeds normally

### Requirement: Phase Progress Output
The system SHALL display progress for each provisioning phase using Rich formatted output.

#### Scenario: Fast phase completion
- **WHEN** a provisioning phase (IPAM, credentials, hydration, ACL grant) completes
- **THEN** a static line `✓ <phase name> (<summary>)` is printed

#### Scenario: Compose up spinner
- **WHEN** the compose up phase begins
- **THEN** a Rich `console.status()` spinner is displayed until compose up completes, then replaced by a `✓ Containers ready (<duration>)` line

#### Scenario: Handover indication
- **WHEN** all phases complete and handover begins
- **THEN** a `→ Handing over to admin shell` line is printed before the PTY exec

#### Scenario: Warmup prompt injected at exec time
- **WHEN** `sandbox.toml` declares a non-empty `project.warmup_prompt`
- **THEN** the `docker exec` call includes `-e SANDBOX_WARMUP_PROMPT="<value>"` and the admin container's `.zshrc` reads this variable on shell init to auto-invoke claude
