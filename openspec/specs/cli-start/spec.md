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
The system SHALL allocate seven consecutive `/24` subnets from the IPAM ledger and derive all static IPs before invoking `docker compose up`.

#### Scenario: New instance gets lowest available slot
- **WHEN** `sandbox start` scaffolds a new instance with no existing IPAM entry
- **THEN** the lowest available `base_index` (0–5704) is assigned and written to `ipam.json` before any compose command runs

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
The system SHALL validate instance readiness before beginning provisioning. Pre-flight includes sentinel verification, secret completeness, and doctor Chain 1 (Privilege Boundary) checks. SSH keypair generation SHALL occur during `_phase_credentials()`. Credential ownership matching SHALL occur during `_phase_credential_ownership()`, which executes after ACL grants (Phase 5). To bypass user namespace unmapped UID restrictions, the ownership matching phase SHALL temporarily escalate directory ACLs and mutate the files.

#### Scenario: Secret completeness gate
- **WHEN** `sandbox start` is invoked and `.sandbox.env` is missing a secret required by the current `sandbox.toml` config (e.g., `FIRECRAWL_API_KEY` when `mcp_firecrawl = true`)
- **THEN** the CLI lists the missing secrets, prints the path to `.sandbox.env`, and exits with code 1 before acquiring any locks

#### Scenario: Doctor Chain 1 pre-flight
- **WHEN** `sandbox start` is invoked
- **THEN** the system executes doctor Chain 1 checks (sudo, machinectl, user exists, systemd-machined, machinectl reachable, Docker available, Docker rootless, gVisor runsc) and aborts with diagnostic output if any check fails

#### Scenario: Pre-flight passes
- **WHEN** sentinel exists, all required secrets are populated, and all Chain 1 checks pass
- **THEN** provisioning proceeds normally

#### Scenario: SSH keypairs generated during credentials phase
- **WHEN** `_phase_credentials()` runs
- **THEN** SSH auth and host keypairs are generated (idempotent — skips if files exist)

#### Scenario: Credential ownership matching after ACL grants
- **WHEN** `_phase_credential_ownership()` runs after Phase 5 (ACL grants)
- **THEN** the orchestrator temporarily escalates the `secrets/` ACL to `rwX` for the `host_unprivileged_user`

#### Scenario: Copy-replace mutator for ownership alignment
- **WHEN** the `secrets/` ACL is escalated
- **THEN** a disposable helper container bind-mounts the `secrets/` directory and executes a `cp -> chown 1000:1000 -> mv` loop on all four IPC secret files via `machinectl shell` as the `host_unprivileged_user`, bypassing unmapped UID restrictions

#### Scenario: Privilege downgrade guard
- **WHEN** the helper container execution concludes (success or failure)
- **THEN** a `finally` block strictly downgrades the `secrets/` ACL back to `rX`

#### Scenario: Hard failure on downgrade fault
- **WHEN** the `finally` block fails to downgrade the `secrets/` ACL
- **THEN** the orchestrator crashes with a `[FATAL]` `SandboxExecutionError` containing a deterministic recovery command (`setfacl -m u:<user>:rX ...`)

#### Scenario: Credential ownership matching does not run during credential generation
- **WHEN** `_phase_credentials()` runs
- **THEN** no Docker commands are executed — ownership matching is deferred to `_phase_credential_ownership()`

### Requirement: ACL Cleanup on Start Failure
The system SHALL revoke ACL grants if Phase 6 (compose up) or Phase 5b (credential ownership matching) fails after Phase 5 (ACL grants) has begun. Cleanup scope SHALL be limited to ACLs — earlier phases are idempotent and do not require rollback.

#### Scenario: Phase 6 failure triggers ACL cleanup
- **WHEN** `_phase_compose_up` raises `SandboxExecutionError` after Phase 5 has begun
- **THEN** `_revoke_acls()` is called in the error handler before releasing the lock

#### Scenario: Phase 5b failure triggers ACL cleanup
- **WHEN** `_phase_credential_ownership` raises `SandboxExecutionError` after Phase 5 has completed
- **THEN** `_revoke_acls()` is called in the error handler before releasing the lock

#### Scenario: Phase 5 partial failure triggers ACL cleanup
- **WHEN** `_phase_acl_grant` raises `SandboxExecutionError` after some ACL grants have succeeded
- **THEN** `_revoke_acls()` is called in the error handler (the phase sentinel is set before Phase 5 begins)

#### Scenario: Pre-Phase-5 failure does not attempt ACL cleanup
- **WHEN** a phase before Phase 5 (IPAM, credentials, hydration) raises an error
- **THEN** ACL revocation is NOT attempted (no ACLs to revoke)

### Requirement: ACL Grant Error Wrapping
The system SHALL wrap `CalledProcessError` from `setfacl` subprocess calls in `SandboxExecutionError` so that ACL grant failures enter the start command's handled exception path.

#### Scenario: setfacl failure wrapped with context
- **WHEN** `subprocess.run(["setfacl", ...], check=True)` raises `CalledProcessError` during Phase 5
- **THEN** the error is caught and re-raised as `SandboxExecutionError` containing the target path that failed and the runtime traverse diagnostic output

### Requirement: Runtime Traverse Failure Diagnostic
The system SHALL provide a diagnostic helper that identifies which specific ancestor directory lacks execute permission for the sandbox user. This diagnostic SHALL only run on the failure path.

#### Scenario: Traverse diagnostic on ACL grant failure
- **WHEN** `_phase_acl_grant` or `_phase_compose_up` fails
- **THEN** `_diagnose_traverse_failure()` walks the ancestor chain, checks `--x` for the sandbox user on each directory, and reports the first failure point

#### Scenario: Diagnostic output format
- **WHEN** a traverse permission gap is found
- **THEN** the output includes the specific directory, the sandbox username, a fix command (`setfacl -m u:<user>:--x <dir>`), and a reference to `sandbox doctor`

### Requirement: Compose Environment File Flag
The system SHALL pass `--env-file <instance_dir>/.sandbox.env` to all `docker compose` invocations to enable compose-level `${VAR}` interpolation.

#### Scenario: --env-file on compose up
- **WHEN** `_phase_compose_up` constructs the compose command
- **THEN** the command includes `--env-file <instance_dir>/.sandbox.env`

#### Scenario: --env-file on warm check
- **WHEN** `_container_status` constructs the compose ps command
- **THEN** the command includes `--env-file <instance_dir>/.sandbox.env`

#### Scenario: Compose file flags not double-wrapped
- **WHEN** `_container_status` constructs the compose ps command with multiple compose files from `_build_compose_files()`
- **THEN** each compose file appears exactly once in the `-f` flag list (flags are used directly from `_build_compose_files()` without re-wrapping)

### Requirement: Phase Progress Output
The system SHALL display progress for each provisioning phase using Rich formatted output. The IPC setup phase (Phase 5b) SHALL NOT appear in the output.

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
- **THEN** the `docker exec` call includes `-e SANDBOX_WARMUP_PROMPT="<value>"` and the admin container's `.zshrc` reads this variable on shell init to auto-invoke claude via SSH

#### Scenario: IPC setup phase absent
- **WHEN** `sandbox start` provisioning phases are inspected
- **THEN** there is NO Phase 5b (IPC setup) — the disposable Alpine container for sticky bit is removed

#### Scenario: Failure with ACL cleanup emits diagnostic
- **WHEN** Phase 5 or Phase 6 fails and ACL cleanup is triggered
- **THEN** the error output includes the traverse failure diagnostic (if applicable) followed by the ACL cleanup status

### Requirement: ACL Grants for rw Bind-Mount Sources
The system SHALL grant `rwX` ACLs (effective and default) to the `host_unprivileged_user` on each rw bind-mount source subdirectory during Phase 5 (ACL grant). The grant plan SHALL include both effective ACL entries (`-m`) and default ACL entries (`-d -m`) to ensure files created by containers inside the bind-mount inherit the sandbox user's write permission.

#### Scenario: rw bind-mount source directories receive effective ACLs
- **WHEN** `_acl_grant_plan()` is called for an instance
- **THEN** the returned plan includes `setfacl -R -m u:<host_user>:rwX <target>` entries for `cache/core/.claude`, `cache/admin/tmux_resurrect`, `log/core`, and `log/admin`

#### Scenario: rw bind-mount source directories receive default ACLs
- **WHEN** `_acl_grant_plan()` is called for an instance
- **THEN** the returned plan includes `setfacl -R -d -m u:<host_user>:rwX <target>` entries for `cache/core/.claude`, `cache/admin/tmux_resurrect`, `log/core`, and `log/admin`

#### Scenario: Non-bind-mounted log directories excluded from grants
- **WHEN** `_acl_grant_plan()` is called for an instance
- **THEN** the returned plan does NOT include entries for `log/proxy` or `log/orchestrator` (these directories are not bind-mounted into containers)

#### Scenario: rw bind-mount ACLs appear in dry-run preview
- **WHEN** `sandbox start --dry-run` is invoked
- **THEN** the command preview includes the rw bind-mount source ACL entries from `_acl_grant_plan()`
