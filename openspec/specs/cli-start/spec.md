## Purpose

This specification defines the `sandbox start` command lifecycle, governing pre-lock warm state detection, concurrency lock acquisition, IPAM allocation, blocking healthcheck wait, and PTY handover.

## Requirements

### Requirement: Pre-Lock Warm State Detection
The system SHALL check whether the sandbox instance's containers are already running before acquiring any concurrency locks. The container status query SHALL use the configured machinectl authentication mode. When `--dry-run` is passed, the system SHALL skip the warm state check and proceed directly to pipeline validation. The system SHALL require a prior `sandbox init` and reject start if no instance is registered.

#### Scenario: No instance found — error with guidance
- **WHEN** `sandbox start` is invoked and no instance is registered for the current project directory
- **THEN** the CLI exits with "No sandbox instance found. Run `sandbox init` first." and exit code 1

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
The system SHALL hand over the terminal to the admin container via `machinectl shell` + `docker exec -it`, releasing `state.lock` before the exec call. The machinectl invocation SHALL use the configured authentication mode from host config.

#### Scenario: Terminal handed to admin container (sudo mode)
- **WHEN** containers are healthy, `state.lock` is released, and `machinectl_authentication` is `"sudo"`
- **THEN** `sudo machinectl shell <docker_unprivileged_user>@.host /usr/bin/docker exec -it <name>-admin-1 zsh` is executed

#### Scenario: Terminal handed to admin container (polkit mode)
- **WHEN** containers are healthy, `state.lock` is released, and `machinectl_authentication` is `"polkit"`
- **THEN** `machinectl shell <docker_unprivileged_user>@.host /usr/bin/docker exec -it <name>-admin-1 zsh` is executed

### Requirement: Instance Pre-Flight Checks
The system SHALL validate instance readiness before beginning provisioning. Pre-flight includes sentinel verification, secret completeness, and doctor Chain 1 (Privilege Boundary) checks. The doctor Chain 1 pre-flight SHALL receive the `machinectl_authentication` mode from host config and pass it to `build_check_registry()`. SSH keypair generation SHALL occur during `_phase_credentials()`. Per-instance file ownership matching for ro single-files (including the four IPC SSH secrets, all proxy ro files, dotfiles, and rendered service configs) SHALL occur during `_phase_helper_cp_chown_ro_files`, after ACL grants and the cache/log helper-recipe phase, via the disposable-helper-container primitive `helper_chown_files` (per the `helper-container` capability).

#### Scenario: Doctor Chain 1 pre-flight with auth mode
- **WHEN** `sandbox start` is invoked
- **THEN** the system loads `machinectl_authentication` from host config and passes it to `build_check_registry()`. In polkit mode, the sudo binary check is omitted. All other Chain 1 checks execute normally.

#### Scenario: Secret completeness gate
- **WHEN** `sandbox start` is invoked and `.sandbox.env` is missing a secret required by the current `sandbox.toml` config (e.g., `FIRECRAWL_API_KEY` when `mcp_firecrawl = true`)
- **THEN** the CLI lists the missing secrets, prints the path to `.sandbox.env`, and exits with code 1 before acquiring any locks

#### Scenario: Pre-flight passes
- **WHEN** sentinel exists, all required secrets are populated, and all Chain 1 checks pass
- **THEN** provisioning proceeds normally

#### Scenario: SSH keypairs generated during credentials phase
- **WHEN** `_phase_credentials()` runs
- **THEN** SSH auth and host keypairs are generated (idempotent — skips if files exist)

#### Scenario: Ro-file ownership matching follows ACL grants
- **WHEN** `_phase_helper_cp_chown_ro_files` runs after `_phase_acl_grant` and `_phase_helper_mkdir_chown_cache_log`
- **THEN** the helper-cp+chown phase chowns each ro single-file (including the four IPC SSH secrets at mode 0600) to its consumer-uid-0 mapping per the `orchestrator-volumes` consumer-uid-0-chown recipe table

#### Scenario: Ownership matching does not run during credential generation
- **WHEN** `_phase_credentials()` runs
- **THEN** no Docker commands are executed — ownership matching is deferred to the helper-recipe phases

### Requirement: ACL Cleanup on Start Failure
The system SHALL revoke ACL grants if any phase after Phase 5 (ACL grants) has begun raises a fatal error. The cleanup scope is the named-ACL grants emitted by `_acl_grant_plan()`; helper-recipe operations themselves are not reverted (per `orchestrator-volumes`'s lifecycle taxonomy — Decision 4 of the acl-ownership-recipes design).

#### Scenario: Phase 6 failure triggers ACL cleanup
- **WHEN** `_phase_compose_up` raises `SandboxExecutionError` after Phase 5 has begun
- **THEN** `_revoke_acls()` is called in the error handler before releasing the lock

#### Scenario: Helper-recipe phase failure triggers ACL cleanup
- **WHEN** any of `_phase_helper_mkdir_chown_cache_log`, `_phase_helper_cp_chown_ro_files`, or `_phase_workspace_shared_group` raises `SandboxExecutionError` after Phase 5 has begun
- **THEN** `_revoke_acls()` is called in the error handler before releasing the lock

#### Scenario: Phase 5 partial failure triggers ACL cleanup
- **WHEN** `_phase_acl_grant` raises `SandboxExecutionError` after some ACL grants have succeeded
- **THEN** `_revoke_acls()` is called in the error handler (the phase sentinel is set before Phase 5 begins)

#### Scenario: Pre-Phase-5 failure does not attempt ACL cleanup
- **WHEN** a phase before Phase 5 (IPAM, credentials, hydration) raises an error
- **THEN** ACL revocation is NOT attempted (no ACLs to revoke)

#### Scenario: Helper-recipe mutations not reverted on failure
- **WHEN** a helper-recipe phase fails partway through after some chowns succeeded
- **THEN** the chowned files remain in their post-helper state; only named-ACL grants are revoked. Per Decision 4, helper-recipe state is in the `applied-on-every-start, idempotent` lifecycle and is reapplied on the next start.

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
- **WHEN** `sandbox.toml` declares a non-empty `instance.warmup_prompt`
- **THEN** the `docker exec` call includes `-e SANDBOX_WARMUP_PROMPT="<value>"` and the admin container's `.zshrc` reads this variable on shell init to auto-invoke claude via SSH

#### Scenario: IPC setup phase absent
- **WHEN** `sandbox start` provisioning phases are inspected
- **THEN** there is NO Phase 5b (IPC setup) — the disposable Alpine container for sticky bit is removed

#### Scenario: Failure with ACL cleanup emits diagnostic
- **WHEN** Phase 5 or Phase 6 fails and ACL cleanup is triggered
- **THEN** the error output includes the traverse failure diagnostic (if applicable) followed by the ACL cleanup status

### Requirement: ACL Grants for rw Bind-Mount Sources
The system SHALL NOT grant `rwX` ACLs (effective or default) to the `host_unprivileged_user` on rw bind-mount source subdirectories during Phase 5. These grants — formerly Pattern B / Option B — are empirically dead under runsc (named ACLs are stripped at the gofer/directfs boundary per `temp/bug-tracker/2026-05-04.md` finding 1) and are replaced by the cache/log helper-recipe phase (subuid-chown + parent default ACL `u:dev:rwx`) for `cache/core/.claude`, `cache/admin/tmux_resurrect`, `log/core`, and `log/admin`. The workspace mount (`user_project_root`) is handled by the workspace shared-group phase plus a granted/revoked named-ACL.

#### Scenario: rw bind-mount source directories do NOT receive the prior effective ACLs
- **WHEN** `_acl_grant_plan()` is called for an instance
- **THEN** the returned plan does NOT include `setfacl -R -m u:<host_user>:rwX <target>` entries for `cache/core/.claude`, `cache/admin/tmux_resurrect`, `log/core`, or `log/admin`

#### Scenario: rw bind-mount source directories do NOT receive the prior default ACLs
- **WHEN** `_acl_grant_plan()` is called for an instance
- **THEN** the returned plan does NOT include `setfacl -R -d -m u:<host_user>:rwX <target>` entries for the same paths

#### Scenario: Cache/log access is provided by the helper-recipe phase instead
- **WHEN** `sandbox start` reaches `_phase_helper_mkdir_chown_cache_log`
- **THEN** the cache/log leaves are chowned to the consumer subuid; the in-container agent reads/writes via owner check (runsc-compatible); dev reads via the parent's inherited `u:dev:rwx`

#### Scenario: Workspace access is provided by the workspace shared-group + named-ACL phases
- **WHEN** `sandbox start` reaches the workspace phases
- **THEN** the workspace mount has `chgrp <bridge-group>`, `chmod 2770`, the persistent default ACL portion, and the granted-at-start named ACL `u:<host_user>:rwx`; the in-container agent reads/writes via group bits (sb-ws supplementary gid); dev reads/writes via group bits (dev's sb-ws membership)

#### Scenario: Non-bind-mounted log directories excluded from grants
- **WHEN** `_acl_grant_plan()` is called for an instance
- **THEN** the returned plan does NOT include entries for `log/proxy` or `log/orchestrator` (these directories are not bind-mounted into containers); this exclusion remains correct post-change-4 even though the rw bind-mount entries are also absent for a different reason

#### Scenario: rw bind-mount ACLs absent from dry-run preview
- **WHEN** `sandbox start --dry-run` is invoked
- **THEN** the command preview does NOT include the prior rw bind-mount source ACL entries; instead, the preview shows the helper-recipe phase plans

### Requirement: Phase Order Includes Helper-Recipe Phases

`sandbox start` SHALL execute provisioning phases in the order: `_phase_ipam → _phase_credentials → _phase_hydrate → _phase_acl_grant → _phase_helper_mkdir_chown_cache_log → _phase_helper_cp_chown_ro_files → _phase_workspace_shared_group → _phase_compose_up`. The three helper-recipe phases between `_phase_acl_grant` and `_phase_compose_up` are new in this change; together they replace the dropped Option-B grants (per `orchestrator-volumes`).

#### Scenario: Helper-recipe phases run after ACL grants and before compose up
- **WHEN** `sandbox start` proceeds past Phase 5 (ACL grants)
- **THEN** `_phase_helper_mkdir_chown_cache_log` runs, then `_phase_helper_cp_chown_ro_files`, then `_phase_workspace_shared_group`, then `_phase_compose_up`

#### Scenario: Each helper phase is independently traceable in dry-run output
- **WHEN** `sandbox start --dry-run` is invoked
- **THEN** the preview output enumerates each helper-recipe phase's planned operations separately (per `orchestrator-volumes`'s "ACL Grant Plan as Single Source of Truth")

### Requirement: Phase 5 Plan Drops Option-B Grants on rw Mount Sources

The Phase-5 ACL grant plan (`_acl_grant_plan`) SHALL NOT include the recursive `u:<host_unprivileged_user>:rwX` grants (effective or default) on rw mount sources (`cache/core/.claude`, `cache/admin/tmux_resurrect`, `log/core`, `log/admin`, `user_project_root`). These grants were "Option B" in the prior model; they are empirically dead under runsc (per `temp/bug-tracker/2026-05-04.md`) and are replaced by helper-recipe phases.

#### Scenario: rw bind-mount source dirs absent from grant plan
- **WHEN** `_acl_grant_plan()` is called
- **THEN** the returned plan does NOT include `setfacl -R -m u:host_user:rwX` entries for any of `cache/core/.claude`, `cache/admin/tmux_resurrect`, `log/core`, `log/admin`, or `user_project_root`

#### Scenario: rw bind-mount source dirs absent from default-ACL grant plan
- **WHEN** `_acl_grant_plan()` is called
- **THEN** the returned plan does NOT include `setfacl -R -d -m u:host_user:rwX` entries for the same paths

### Requirement: Phase 5 Plan Includes Workspace Named-ACL Grants

The Phase-5 ACL grant plan SHALL include the workspace named-ACL grants (effective and default-ACL named-entry portion). These are part of the named-acl mechanism per `orchestrator-volumes`'s lifecycle table.

#### Scenario: Workspace effective named-ACL granted
- **WHEN** `_acl_grant_plan()` is called
- **THEN** the returned plan includes `setfacl -m u:<host_unprivileged_user>:rwx <user_project_root>`

#### Scenario: Workspace default-ACL with named entry granted
- **WHEN** `_acl_grant_plan()` is called
- **THEN** the returned plan includes `setfacl -d -m u::rwx,g::rwx,o::---,m::rwx,u:<host_unprivileged_user>:rwx,u:dev:rwx <user_project_root>`

### Requirement: Cache/Log Helper-Recipe Phase

The system SHALL provide `_phase_helper_mkdir_chown_cache_log` that, after Phase 5 ACL grants, applies the cache/log subuid-chown recipe to every cache/log leaf identified by the bind-mount inventory in `compose.yml`. The phase SHALL invoke `helper_mkdir_chown_dirs` for each (parent, leaves, owner_uid, owner_gid) group, batching leaves that share the same parent and target ownership.

#### Scenario: Cache/log phase batches by parent
- **WHEN** the phase runs against the four standard cache/log leaves (`cache/core/.claude`, `cache/admin/tmux_resurrect`, `log/core`, `log/admin`)
- **THEN** `helper_mkdir_chown_dirs` is invoked at most once per distinct parent dir (each invocation handles all leaves under that parent)

#### Scenario: Cache/log phase sets parent default ACL before helper runs
- **WHEN** the phase processes a cache/log parent dir
- **THEN** `setfacl -d -m u::rwx,g::---,o::---,m::rwx,u:dev:rwx <parent>` is applied BEFORE the helper invocation; the helper-created leaf inherits the default ACL on its child files

#### Scenario: Cache/log phase is idempotent
- **WHEN** the phase runs and the leaves already exist with correct ownership
- **THEN** `helper_mkdir_chown_dirs` returns successfully without changing any state (mkdir -p is no-op; chown is no-op when owner matches)

### Requirement: Ro-Files Helper-Recipe Phase

The system SHALL provide `_phase_helper_cp_chown_ro_files` that, after `_phase_helper_mkdir_chown_cache_log`, applies the consumer-uid-0-chown recipe to every ro single-file in the per-class table (per `orchestrator-volumes`). The phase SHALL invoke `helper_chown_files` once per (consumer-uid, mode) group, batching files that share the target ownership/mode.

#### Scenario: Ro-files phase batches by consumer-uid + mode
- **WHEN** the phase runs across all ro single-files
- **THEN** `helper_chown_files` is invoked at most once per distinct (consumer-uid, mode) tuple

#### Scenario: Ro-files phase replaces _phase_credential_ownership
- **WHEN** the orchestrator code is inspected for the prior `_phase_credential_ownership` function
- **THEN** the function has been removed; its IPC SSH secrets are now handled by `_phase_helper_cp_chown_ro_files` per the standard mapping table

#### Scenario: Ro-files phase is idempotent
- **WHEN** the phase runs and the files already have correct ownership/mode
- **THEN** `helper_chown_files` returns successfully without changing any state

### Requirement: Workspace Shared-Group Phase

The system SHALL provide `_phase_workspace_shared_group` that, after `_phase_helper_cp_chown_ro_files`, applies the shared-group recipe to `<user_project_root>` per the drift-detection contract in `orchestrator-volumes`'s "Workspace Recursive Setup via Drift Detection".

#### Scenario: Drift detection on workspace root
- **WHEN** the phase runs
- **THEN** it calls `os.stat(<user_project_root>)` and compares the result against `(setgid bit set, group == workspace_bridge_gid(host))`

#### Scenario: First-run / drift triggers recursive setup
- **WHEN** drift is detected
- **THEN** the phase runs `chgrp -R <bridge-group> <user_project_root>` (best-effort; logs per-file failures), `find ... -type d -exec chmod 2770`, `find ... -type f -exec chmod 0660`, and the workspace setfacl operations

#### Scenario: Steady-state skips recursion
- **WHEN** drift is NOT detected (workspace root already has setgid + correct group)
- **THEN** the phase only ensures root state via idempotent operations (`chmod 2770`, `chgrp`, `setfacl` on root only); recursive operations are skipped

#### Scenario: Phase fails fast if bridge-group preconditions are unmet
- **WHEN** the phase calls `workspace_bridge_gid(host)` and the function raises `WorkspaceBridgeGroupMissingError` (group does not exist) or `SubgidOutOfRangeError` (group's gid is outside the daemon user's subgid range)
- **THEN** the phase aborts with a clear error directing the operator to `sandbox doctor`

### Requirement: ACL Cleanup on Start Failure Includes Helper-Recipe Phases

The existing "ACL Cleanup on Start Failure" requirement SHALL be extended: if any of the helper-recipe phases (`_phase_helper_mkdir_chown_cache_log`, `_phase_helper_cp_chown_ro_files`, `_phase_workspace_shared_group`) raises after Phase 5 has begun, the existing `_revoke_acls()` cleanup SHALL still run before releasing the lock. Helper-recipe operations themselves are not revoked (per `orchestrator-volumes`'s lifecycle policy); only the named-ACL grants are.

#### Scenario: Helper-recipe failure triggers ACL cleanup
- **WHEN** `_phase_helper_mkdir_chown_cache_log` (or any helper-recipe phase) raises `SandboxExecutionError` after Phase 5 has begun
- **THEN** `_revoke_acls()` is called in the error handler before releasing the lock

#### Scenario: Helper-recipe failure does NOT attempt to revert chown/chmod
- **WHEN** a helper-recipe phase fails partway through
- **THEN** files/dirs already chowned by previous helper invocations remain in their post-helper state; the orchestrator does not attempt to chown them back


### Requirement: Per-User State Initialization Required
The `sandbox start` command SHALL refuse to operate when the per-user state tree is not initialized. Initialization is signaled by the presence of `<sandbox_ai_user_home()>/state/instances.json`. On absence, the command SHALL exit with a clear error directing the operator to run `sandbox init`.

#### Scenario: Start on uninitialized host
- **WHEN** `sandbox start` is invoked and `<home>/state/instances.json` does not exist
- **THEN** the CLI exits with: "Error: per-user state not initialized at `<resolved-home>`. Run `sandbox init` first." and exit code 1, before any other phase runs (registry lookup, lock acquisition, doctor pre-flight all skipped)

#### Scenario: Resolved home in error message
- **WHEN** the start command above runs with `SANDBOX_AI_USER_HOME=/tmp/test-home` set
- **THEN** the error message contains `/tmp/test-home` so the operator can verify which path was checked
