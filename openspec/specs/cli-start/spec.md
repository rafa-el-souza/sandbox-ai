## Purpose

This specification defines the `sandbox start` command lifecycle, governing pre-lock warm state detection, concurrency lock acquisition, IPAM allocation, blocking healthcheck wait, and PTY handover.
## Requirements
### Requirement: Explicit Instance Argument

`sandbox start <inst>` SHALL require the instance name as a positional argument. CWD-based instance discovery is removed; the command does NOT inspect the current working directory.

#### Scenario: Instance argument required
- **WHEN** `sandbox start` is invoked without an `<inst>` argument
- **THEN** the CLI exits with a typer "missing argument" error

#### Scenario: CWD has no role in resolution
- **WHEN** `sandbox start <inst>` is invoked from any working directory
- **THEN** the instance is looked up by name in `~/.sandbox-ai/state/instances.json` regardless of CWD

### Requirement: Per-Instance Backup Lock Check

`sandbox start <inst>` SHALL check `~/.sandbox-ai/state/<inst>.backup.lock` after acquiring `state.lock` and refuse fast if held. Operator guidance: wait for the backup to complete or run `sandbox doctor` to inspect.

#### Scenario: Concurrent backup blocks start
- **WHEN** `sandbox start <inst>` is invoked while `<inst>.backup.lock` is held by a backup operation
- **THEN** start exits with: "Backup in progress for <inst>; wait or `sandbox doctor` to inspect." and exit code 1

### Requirement: Workspace Shared-Group Phase Iterates Workspaces

The system SHALL provide `_phase_workspace_shared_group` that runs as part of the canonical phase order (between `_phase_ipam` and `_phase_acl_grant`, per `orchestrator-volumes`'s `Phase Order Contract for Ownership-Sensitive Phases`), iterates `[workspaces]` (sorted by name), and applies the shared-group recipe to each `workspace.path`.

For each workspace:
- Drift detection: `os.stat(ws.path)` checks setgid bit + group ownership against `workspace_bridge_gid(host)`.
- First-run / drift: in-process recursive setup over `os.walk(ws.path)` — for every entry, `os.chown(path, -1, bridge_gid, follow_symlinks=False)` (best-effort; per-file EPERM on non-dev-owned files is collected and reported in aggregate, not escalated), then `os.chmod(path, 0o2770)` for directories and `os.chmod(path, 0o0660)` for non-symlink regular files. Then steady-state idempotent root setup: `os.chown` + `os.chmod` on `<ws.path>` itself, and `setfacl` (effective + default) for the persistent default-ACL portion `u::rwx,g::rwx,o::---,m::rwx,u:<host_user>:rwx[,u:dev:rwx]`.
- Steady-state: only the root-state idempotent setup runs (drift detection signals no recursion needed).

The recursive recipe is fully in-process — there are no subprocess `chgrp -R`, `find -exec chmod`, or `setfacl -R` invocations during the walk. Subprocess `setfacl` runs only on the workspace root (in steady-state and first-run alike), to install the default-ACL portion that the kernel inheritance does not derive from mode bits.

The phase SHALL fail-fast if `workspace_bridge_gid(host)` raises (group missing or out of subgid range), surfacing the doctor remediation.

#### Scenario: Phase iterates all workspaces
- **WHEN** the phase runs for an instance with N workspaces
- **THEN** drift detection runs N times (once per workspace); first-run setup runs only on workspaces whose drift detection signals it

#### Scenario: First-run on a fresh workspace triggers recursive setup
- **WHEN** a workspace's root has no setgid bit set (typical: just-scaffolded workspace)
- **THEN** the phase walks `<ws.path>` in-process via `os.walk` and applies `os.chown(path, -1, bridge_gid, follow_symlinks=False)` + `os.chmod(path, 0o2770 for dirs / 0o0660 for non-symlink files)` to every entry, then runs the steady-state root setup on `<ws.path>` (chown + chmod 2770 + setfacl for the persistent default-ACL portion)

#### Scenario: Steady-state skips recursion
- **WHEN** a workspace's root already has setgid + correct group
- **THEN** the phase only ensures root state is correct (idempotent) and skips recursive operations for that workspace

#### Scenario: Bridge-group missing fails fast
- **WHEN** the phase calls `workspace_bridge_gid(host)` and it raises `WorkspaceBridgeGroupMissingError`
- **THEN** the phase aborts with a clear error directing the operator to `sandbox doctor`

### Requirement: Pre-Lock Warm State Detection
The system SHALL check whether the sandbox instance's containers are already running before acquiring any concurrency locks. The container status query SHALL use the configured machinectl authentication mode. When `--dry-run` is passed, the system SHALL skip the warm state check and proceed directly to pipeline validation. The system SHALL require a prior `sandbox init <inst>` and reject start if `<inst>` is not registered.

#### Scenario: No instance found — error with guidance
- **WHEN** `sandbox start <inst>` is invoked and `<inst>` is not present in the registry
- **THEN** the CLI exits with "No sandbox instance named '<inst>'. Run `sandbox init <inst>` first." and exit code 1

#### Scenario: Partial init detected
- **WHEN** `sandbox start <inst>` is invoked and the registry contains an entry but `.initialized` sentinel is missing
- **THEN** the CLI exits with "Instance partially initialized. Run `sandbox destroy <inst>` then `sandbox init <inst>`." and exit code 1

#### Scenario: Already-running instance exits without lock contention
- **WHEN** `sandbox start <inst>` is invoked and `docker compose ps -q` returns non-empty output
- **THEN** the CLI exits with "Sandbox '<inst>' is already running. Use 'sandbox attach <inst> [<ws>]' to reconnect." before acquiring `state.lock` or the IPAM lock

#### Scenario: Dry-run bypasses warm state check
- **WHEN** `sandbox start <inst> --dry-run` is invoked
- **THEN** the warm state check is skipped and the system proceeds to dry-run validation regardless of container state

### Requirement: Concurrency Lock Acquisition
The system SHALL acquire a per-user `state.lock` (fcntl `LOCK_EX | LOCK_NB`) at `<sandbox_ai_home()>/state/state.lock` before modifying any instance state. The per-user `state.lock` guards the provisioning sequence as a whole; finer-grained mutation locks (notably the IPAM ledger lock at `<sandbox_ai_home()>/state/ipam.json.lock`, per `orchestrator-networking`'s "IPAM Ledger Lock File" requirement) are acquired internally by the components they protect and are distinct from `state.lock`.

The `start` command SHALL NOT pass its `state.lock` file descriptor into IPAM or any other component, and SHALL NOT re-open `state.lock` after the initial acquisition. IPAM mutation paths invoked while `state.lock` is held SHALL acquire only the IPAM lock and complete without raising `IPAMLockException` due to the outer `state.lock` holder.

#### Scenario: Concurrent start rejected
- **WHEN** a second `sandbox start` is invoked for the same instance while the first is still in progress
- **THEN** the second invocation exits with: "Another sandbox start is already in progress for this instance."

#### Scenario: IPAM phase succeeds while start holds state.lock

- **WHEN** `sandbox start <inst>` has acquired the per-user `state.lock` and proceeds to `_phase_ipam`
- **THEN** `IPAMLedger.allocate` acquires `<sandbox_ai_home()>/state/ipam.json.lock`, mutates the ledger, releases the IPAM lock, and returns; the start command continues to subsequent phases without raising `IPAMLockException` or `BlockingIOError`

### Requirement: IPAM Allocation Before Launch
The system SHALL allocate five consecutive `/24` subnets from the IPAM ledger and derive all static IPs before invoking `docker compose up`.

#### Scenario: New instance gets lowest available slot
- **WHEN** `sandbox start` scaffolds a new instance with no existing IPAM entry
- **THEN** the lowest available `base_index` (0–7986) is assigned and written to `ipam.json` before any compose command runs

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
The system SHALL hand over the terminal to **core** (as `agent`) using the same canonical mechanism `cli-attach`'s `PTY Handover Without Re-Hydration` requirement defines: a host-side ssh client wrapped in `tlog-rec`, with a `ProxyCommand` that **in separate-user mode** crosses the privilege boundary via the streaming dispatcher op `fwd` (the bare `dispatch fwd <inst> --project <P> --ip <IP>` payload over `sudo_pipe_cmd`), which execs `docker exec -i <project>-admin-1 /fwd <core_ipc_ip>:9999` to forward stdio↔TCP to core's sshd. The exact command shape is owned by `cli-attach`; this requirement delegates to it. `state.lock` SHALL be released before the handover invocation.

**In separate-user mode** the `ProxyCommand` crossing prefix is `sudo_pipe_cmd` (the privileged byte-pipe, sudoers-authorized, headless-capable). The crossed `dispatch fwd <wire>` payload crosses via `sudo_pipe_cmd`; `machinectl_cmd` is never used for the ProxyCommand (the PTY's `onlcr` would corrupt the SSH binary stream).

#### Scenario: Terminal handed to core (sudo mode)
- **WHEN** containers are healthy, `state.lock` is released, and `machinectl_authentication` is `"sudo"`
- **THEN** the system invokes the same `tlog-rec → ssh → ProxyCommand → /fwd` command as `sandbox attach` (see `cli-attach`'s "Terminal handed to core via ssh-through-admin (separate-user, SUDO mode)" scenario); the `ProxyCommand` is `sudo systemd-run -q --pipe --uid=<docker_unprivileged_user> /bin/bash -c '/usr/local/libexec/sandbox-ai/dispatch fwd <inst> --project <project_name> --ip <core_ipc_ip>'`

### Requirement: Instance Pre-Flight Checks
The system SHALL validate instance readiness before beginning provisioning. Pre-flight includes sentinel verification, secret completeness, and doctor Chain 1 (Privilege Boundary) checks. The doctor Chain 1 pre-flight SHALL receive the `machinectl_authentication` mode from host config and pass it to `build_check_registry()`. SSH keypair generation SHALL occur during `_phase_credentials()`. Per-instance file ownership matching for ro single-files (including the four IPC SSH secrets, all proxy ro files, dotfiles, and rendered service configs) SHALL occur during `_phase_helper_cp_chown_ro_files`, after ACL grants and the cache/log helper-recipe phase, via the disposable-helper-container primitive `helper_chown_files` (per the `helper-container` capability).

#### Scenario: Doctor Chain 1 pre-flight with auth mode
- **WHEN** `sandbox start` is invoked
- **THEN** the system loads `machinectl_authentication` from host config and passes it to `build_check_registry()`. All Chain 1 checks execute normally.

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

The system SHALL revoke ACL grants if any phase after `_phase_acl_grant` (the named-ACL grant phase) has begun raises a fatal error. The cleanup scope is the named-ACL grants emitted by `_acl_grant_plan()`; helper-recipe operations themselves are not reverted (per `orchestrator-volumes`'s `Lifecycle × Mechanism Matrix` — helper-recipe state is in the `applied-on-every-start, idempotent` lifecycle).

#### Scenario: Compose-up failure triggers ACL cleanup
- **WHEN** `_phase_compose_up` raises `SandboxExecutionError` after `_phase_acl_grant` has begun
- **THEN** `_revoke_acls()` is called in the error handler before releasing the lock

#### Scenario: Helper-recipe phase failure triggers ACL cleanup
- **WHEN** either `_phase_helper_mkdir_chown_cache_log` or `_phase_helper_cp_chown_ro_files` raises `SandboxExecutionError` (both run AFTER `_phase_acl_grant` per the canonical phase order)
- **THEN** `_revoke_acls()` is called in the error handler before releasing the lock

#### Scenario: Post-hydrate daemon-read pass failure triggers ACL cleanup
- **WHEN** `_phase_grant_post_hydrate_daemon_read` raises `SandboxExecutionError` (runs AFTER `_phase_acl_grant`)
- **THEN** `_revoke_acls()` is called in the error handler before releasing the lock

#### Scenario: ACL-grant partial failure triggers ACL cleanup
- **WHEN** `_phase_acl_grant` raises `SandboxExecutionError` after some ACL grants have succeeded
- **THEN** `_revoke_acls()` is called in the error handler (the `acl_granted` sentinel is set when `_phase_acl_grant` begins)

#### Scenario: Pre-ACL-grant failure does not attempt ACL cleanup
- **WHEN** a phase before `_phase_acl_grant` raises an error — under the canonical phase order, this is `_phase_ipam` or `_phase_workspace_shared_group`
- **THEN** ACL revocation is NOT attempted (no ACLs have been granted; `acl_granted=False`)

#### Scenario: Helper-recipe mutations not reverted on failure
- **WHEN** a post-`_phase_acl_grant` helper-recipe phase fails partway through after some chowns succeeded
- **THEN** the chowned files remain in their post-helper state; only named-ACL grants are revoked. Helper-recipe state is in the `applied-on-every-start, idempotent` lifecycle and is reapplied on the next start.

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
The system SHALL pass `--env-file <instance_dir>/.sandbox.env` to all `docker compose` invocations to enable compose-level `${VAR}` interpolation. The `compose up` shell command SHALL be constructed by a single helper (`_compose_up_cmd_plan` or equivalent in `src/cli/main.py`) that is the sole producer of the `TERM=dumb NO_COLOR=1 BUILDKIT_PROGRESS=plain COMPOSE_PROJECT_NAME=<project> docker compose <files> --ansi never --env-file <env> up -d --build --wait` string. Both the live execution path (`_phase_compose_up`) and the dry-run preview SHALL render their displayed/executed command from this helper; no parallel reconstruction of the compose `up` command is permitted.

#### Scenario: --env-file on compose up
- **WHEN** `_phase_compose_up` constructs the compose command
- **THEN** the command includes `--env-file <instance_dir>/.sandbox.env`

#### Scenario: --env-file on warm check
- **WHEN** `_container_status` constructs the compose ps command
- **THEN** the command includes `--env-file <instance_dir>/.sandbox.env`

#### Scenario: Compose file flags not double-wrapped
- **WHEN** `_container_status` constructs the compose ps command with multiple compose files from `_build_compose_files()`
- **THEN** each compose file appears exactly once in the `-f` flag list (flags are used directly from `_build_compose_files()` without re-wrapping)

#### Scenario: Live and dry-run derive compose up from a shared plan helper

- **WHEN** the dry-run preview and `_phase_compose_up` are invoked for the same `(instance_dir, project_name, config)` inputs
- **THEN** both paths obtain the inner `bash -c` command string from the same `_compose_up_cmd_plan` helper; the dry-run displayed command and the string passed to `Executor.run` by `_phase_compose_up` are byte-identical for those inputs

#### Scenario: Compose up command contains compose -f flags, not helper-cp filenames

- **WHEN** the dry-run preview renders the compose up command for an instance whose hydration emits one or more helper-cp groups (e.g., `ipc_known_hosts`, `ipc_ssh_key`)
- **THEN** the rendered command contains `docker compose -f <instance_dir>/docker/compose.yml [...]` and does NOT contain any helper-cp filename joined by `, ` in place of compose file flags

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
- **THEN** a `→ Handing over to core` line is printed before the handover exec

#### Scenario: IPC setup phase absent
- **WHEN** `sandbox start` provisioning phases are inspected
- **THEN** there is NO Phase 5b (IPC setup) — the disposable Alpine container for sticky bit is removed

#### Scenario: Failure with ACL cleanup emits diagnostic
- **WHEN** Phase 5 or Phase 6 fails and ACL cleanup is triggered
- **THEN** the error output includes the traverse failure diagnostic (if applicable) followed by the ACL cleanup status

### Requirement: ACL Grants for rw Bind-Mount Sources

The system SHALL NOT grant `rwX` ACLs (effective or default) to the `host_unprivileged_user` on rw bind-mount source subdirectories during `_phase_acl_grant`. These grants — formerly known as Pattern B / Option B — are empirically dead under runsc (named POSIX ACLs are stripped at the gofer/directfs boundary, so the named entry never reaches the in-container process and cannot grant access; the `--add-cap` and supplementary-group mechanisms are what actually carry permission). They are replaced by the cache/log helper-recipe phase (subuid-chown + parent default ACL `u:dev:rwx`) for the cache/log leaves per `orchestrator-volumes`'s "Cache/Log Leaf Inventory" requirement. Each workspace's bind-mount source (every `workspace.path` in `[workspaces]`) is handled by the per-workspace shared-group phase plus a granted/revoked named-ACL applied per-workspace.

#### Scenario: rw bind-mount source directories do NOT receive the prior effective ACLs
- **WHEN** `_acl_grant_plan()` is called for an instance
- **THEN** the returned plan does NOT include `setfacl -R -m u:<host_user>:rwX <target>` entries for any leaf in `orchestrator-volumes`'s "Cache/Log Leaf Inventory"

### Requirement: Phase Order Includes Helper-Recipe Phases

`sandbox start <inst>` SHALL execute provisioning phases in the order specified by `orchestrator-volumes`'s `Phase Order Contract for Ownership-Sensitive Phases` requirement (the canonical owner): `_phase_ipam → _phase_workspace_shared_group → _phase_acl_grant → _phase_credentials → _phase_hydrate → _phase_grant_post_hydrate_daemon_read → _phase_helper_mkdir_chown_cache_log → _phase_helper_cp_chown_ro_files → _phase_compose_up`.

This requirement mirrors the canonical order at the CLI surface so dry-run output, per-phase logging, and error-handler rollback semantics align with the lifecycle×mechanism contract owned by `orchestrator-volumes`. The CLI MUST NOT define an alternative ordering; any change to the phase sequence SHALL update `orchestrator-volumes`'s `Phase Order Contract for Ownership-Sensitive Phases` first and then ripple into this requirement.

Three ordering invariants from the canonical contract surface at the CLI:

1. `_phase_workspace_shared_group` runs BEFORE `_phase_acl_grant` so `chmod 2770` on each workspace root lands on a non-extended-ACL inode (the workspace-failure-pre-acl-grant case yields a teardown that does NOT invoke `_revoke_acls` because `acl_granted=False`).
2. `_phase_acl_grant` runs BEFORE `_phase_credentials` so the default ACL on `secrets/` is in place before `generate_ssh_keypair` writes new files.
3. `_phase_grant_post_hydrate_daemon_read` runs AFTER both `_phase_credentials` AND `_phase_hydrate`, AND BEFORE `_phase_helper_cp_chown_ro_files` AND `_phase_compose_up`, so every helper-cp source file (`RO_FILE_RECIPES + EXEC_FILE_RECIPES`) AND every daemon-read direct file (`DAEMON_READ_DIRECT_FILES`) carries the named ACL by the time the daemon reads them.

#### Scenario: Phase invocation order matches the canonical contract
- **WHEN** `sandbox start <inst>` runs
- **THEN** the phase invocation order is: `_phase_ipam`, `_phase_workspace_shared_group`, `_phase_acl_grant`, `_phase_credentials`, `_phase_hydrate`, `_phase_grant_post_hydrate_daemon_read`, `_phase_helper_mkdir_chown_cache_log`, `_phase_helper_cp_chown_ro_files`, `_phase_compose_up`

#### Scenario: Workspace shared-group precedes named-ACL grant
- **WHEN** `sandbox start <inst>` proceeds through the ownership-sensitive phases
- **THEN** `_phase_workspace_shared_group` is invoked BEFORE `_phase_acl_grant`; this ordering is what allows the workspace shared-group recipe to omit the explicit `setfacl -m g::rwx` step

#### Scenario: Post-hydrate daemon-read pass runs between hydrate and helper-cp
- **WHEN** `sandbox start <inst>` proceeds past `_phase_hydrate`
- **THEN** `_phase_grant_post_hydrate_daemon_read` is invoked next (before `_phase_helper_mkdir_chown_cache_log` and `_phase_helper_cp_chown_ro_files`), iterating `RO_FILE_RECIPES + EXEC_FILE_RECIPES + DAEMON_READ_DIRECT_FILES` and emitting `setfacl -m u:<host_user>:r <path>` per existing target

#### Scenario: Each helper phase is independently traceable in dry-run output
- **WHEN** `sandbox start <inst> --dry-run` is invoked
- **THEN** the preview output enumerates each helper-recipe phase's planned operations separately, with per-workspace fan-out for `_phase_workspace_shared_group`

### Requirement: Phase 5 Plan Drops Option-B Grants on rw Mount Sources

The Phase-5 ACL grant plan (`_acl_grant_plan`) SHALL NOT include the recursive `u:<host_unprivileged_user>:rwX` grants (effective or default) on rw mount sources — neither the cache/log leaves per `orchestrator-volumes`'s "Cache/Log Leaf Inventory" requirement, nor any workspace path from `[workspaces]`. These grants were "Option B" in the prior model; they are empirically dead under runsc and are replaced by helper-recipe phases (cache/log) and the per-workspace shared-group phase (workspace paths).

#### Scenario: rw bind-mount source dirs absent from grant plan
- **WHEN** `_acl_grant_plan()` is called
- **THEN** the returned plan does NOT include `setfacl -R -m u:host_user:rwX` entries for any leaf in `orchestrator-volumes`'s "Cache/Log Leaf Inventory" or any `workspace.path` from `[workspaces]`

#### Scenario: rw bind-mount source dirs absent from default-ACL grant plan
- **WHEN** `_acl_grant_plan()` is called
- **THEN** the returned plan does NOT include `setfacl -R -d -m u:host_user:rwX` entries for the same paths (cache/log inventory leaves and workspace paths)

### Requirement: Phase 5 Plan Includes Per-Workspace Named-ACL Grants

The Phase-5 ACL grant plan SHALL include the per-workspace named-ACL grants (effective and default-ACL named-entry portion) for EACH workspace in `[workspaces]`. Plans iterate the workspaces map sorted by name for render determinism.

#### Scenario: Per-workspace effective named-ACL granted
- **WHEN** `_acl_grant_plan()` is called for an instance with workspaces `main` and `scratch`
- **THEN** the returned plan includes `setfacl -m u:<host_unprivileged_user>:rwx <main.path>` AND `setfacl -m u:<host_unprivileged_user>:rwx <scratch.path>`

#### Scenario: Per-workspace default-ACL with named entry granted
- **WHEN** `_acl_grant_plan()` is called for the same instance
- **THEN** the returned plan includes `setfacl -d -m u::rwx,g::rwx,o::---,m::rwx,u:<host_unprivileged_user>:rwx,u:dev:rwx <main.path>` AND the equivalent for `<scratch.path>`

### Requirement: Cache/Log Helper-Recipe Phase

The system SHALL provide `_phase_helper_mkdir_chown_cache_log` that, after Phase 5 ACL grants, applies the cache/log subuid-chown recipe to every leaf in the cache/log bind-mount inventory (per `orchestrator-volumes`'s "Cache/Log Leaf Inventory" requirement; the runtime source of truth is `compose.yml`). The phase SHALL invoke `helper_mkdir_chown_dirs` for each (parent, leaves, owner_uid, owner_gid) group, batching leaves that share the same parent and target ownership. Per `orchestrator-volumes`'s "Scaffold-vs-Helper Boundary" requirement, the helper recipe is the sole creator of the leaves on disk — `sandbox init` does NOT pre-create them.

#### Scenario: Cache/log phase batches by parent
- **WHEN** the phase runs against the cache/log leaves enumerated in `orchestrator-volumes`'s "Cache/Log Leaf Inventory"
- **THEN** `helper_mkdir_chown_dirs` is invoked at most once per distinct parent dir (each invocation handles all leaves under that parent)

#### Scenario: Cache/log phase sets parent default ACL before helper runs
- **WHEN** the phase processes a cache/log parent dir
- **THEN** `setfacl -d -m u::rwx,g::---,o::---,m::rwx,u:dev:rwx <parent>` is applied BEFORE the helper invocation; the helper-created leaf inherits the default ACL on its child files

#### Scenario: Cache/log phase is idempotent
- **WHEN** the phase runs a second time against an instance whose leaves already exist with correct ownership
- **THEN** `helper_mkdir_chown_dirs`'s `mkdir -p` is a no-op and the `chown` is idempotent; on-disk state is unchanged

#### Scenario: Cache/log phase creates leaves on first start (post-Change-D contract)
- **WHEN** the phase runs against a freshly-init'd instance whose cache/log leaves do NOT exist on disk (the post-Change-D scaffold contract per `orchestrator-volumes`'s "Scaffold-vs-Helper Boundary")
- **THEN** the phase succeeds: `helper_mkdir_chown_dirs` creates the leaves as in-container root (= host claude-sandbox, mapped in the helper userns) and chowns them to the consumer subuid (also mapped); both operations are kernel-legal; the resulting on-disk leaves are consumer-subuid-owned

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

### Requirement: Handover TTY Autodetect

The `sandbox start` command SHALL gate `_phase_handover` on the
process's stdin TTY status. When `_stdin_is_tty()` returns False, the
command MUST skip `_phase_handover` entirely, print the attach hint
`Sandbox '<inst>' started. Attach with: sandbox attach <inst>`, and
return successfully (exit code 0).

The TTY check MUST use the `_stdin_is_tty()` wrapper (which calls
`sys.stdin.isatty()`); inlining `sys.stdin.isatty()` or `os.isatty(0)`
is forbidden because typer's `CliRunner` replaces `sys.stdin` and
breaks those forms.

#### Scenario: Non-TTY stdin skips handover and prints hint

- **WHEN** `sandbox start <inst>` is invoked with `_stdin_is_tty()`
  returning False (CI runner, redirected stdin, scripted probe)
- **THEN** `_phase_handover` is NOT called, the printed output
  contains `attach with: sandbox attach <inst>` (case-insensitive),
  and the command returns exit code 0

#### Scenario: TTY stdin proceeds to handover

- **WHEN** `sandbox start <inst>` is invoked with `_stdin_is_tty()`
  returning True and `--no-handover` not passed
- **THEN** `_phase_handover` is called and the printed output
  contains `handing over` (case-insensitive)

### Requirement: --no-handover Flag

The `sandbox start` command SHALL accept a `--no-handover` boolean
flag. When passed, the command MUST skip `_phase_handover` regardless
of TTY status, print the attach hint, and return successfully (exit
code 0).

The flag's effect MUST be a logical OR with the non-TTY autodetect:
either predicate short-circuits the handover. Operators on a TTY
session who want to start-and-go pass `--no-handover`; the autodetect
covers callers who never had a TTY.

#### Scenario: --no-handover skips handover on a TTY session

- **WHEN** `sandbox start <inst> --no-handover` is invoked with
  `_stdin_is_tty()` returning True
- **THEN** `_phase_handover` is NOT called, the printed output
  contains `attach with: sandbox attach <inst>` (case-insensitive),
  and the command returns exit code 0

#### Scenario: Flag is documented in --help

- **WHEN** `sandbox start --help` is invoked
- **THEN** the help output lists `--no-handover` with a description
  noting that it skips the interactive core handover and
  prints the attach hint

### Requirement: Handover Default Direction

The `sandbox start` command SHALL default to invoking
`_phase_handover` when stdin is a TTY and `--no-handover` is not
passed. This preserves the operator-friendly workflow where `sandbox
start <inst>` drops the operator into an interactive shell on core
ready to work.

The default direction is recorded normatively so any future flip
(e.g. opt-in `--handover` flag, or moving the shell-drop to a
separate `sandbox shell` command) requires a spec change rather than
silently shipping under a refactor.

#### Scenario: Default-on handover for interactive operator

- **WHEN** an operator runs `sandbox start <inst>` from an
  interactive terminal with no flags
- **THEN** after all phases succeed, `_phase_handover` is invoked
  and the operator is dropped into core's interactive shell (as
  `agent`) via the `cli-attach` canonical `tlog-rec → ssh →
  ProxyCommand → /fwd` invocation

#### Scenario: Spec change required to flip default

- **WHEN** a maintainer wishes to change the default direction
  (handover OFF by default; require `--handover` to opt in, or
  introduce `sandbox shell`)
- **THEN** the maintainer MUST submit a new OpenSpec change that
  modifies this requirement; ad-hoc flips in a refactor are
  prohibited

### Requirement: `start` preflight health crossings collapse to one (plus the compose-ps warm-check)

`sandbox start`'s preflight SHALL perform its instance-agnostic read-only health checks (the 7-check
privilege-boundary doctor chain — `machinectl_reachable`, `docker_available`, `docker_rootless`, `runsc`,
`runsc_runtimeargs`, `host_uds`, `compose_project_name_collision`) in a **single** `preflight`-op crossing
whose result is parsed orchestrator-side, instead of one crossing per check. (Reachability, formerly the
`machinectl_reachable`/`auth-probe` check, is established by the `preflight` crossing itself succeeding — its
`echo ok` query.) The instance-stateful `compose-ps` warm-check (Q6 —
operator-resolved project/compose-file/env-file) SHALL remain its own crossing. So the read-only preflight
collapses from 8 crossings to **two** (one `preflight` + one `compose-ps`). The bundle SHALL be read-only and
SHALL preserve each check's individual pass/fail signal + its specific diagnostic in the parsed result. The
explicit (init-path) `auth-probe`'s redundancy with the chain's `machinectl_reachable` check SHALL be removed
(the crossing succeeding is the reachability signal).

#### Scenario: one health crossing, not a burst
- **WHEN** `sandbox start <inst>` runs its preflight on a separate-user host
- **THEN** the instance-agnostic health checks perform exactly one boundary crossing (the `preflight` op), not
  one per check
- **AND** the `compose-ps` warm-check is the only other read-only crossing
- **AND** each bundled check's pass/fail is still surfaced individually to the operator

#### Scenario: a failing preflight check still aborts with its own message
- **WHEN** a bundled preflight check fails (e.g. the privilege boundary is misconfigured)
- **THEN** `start` aborts with that check's specific diagnostic, not a generic bundle failure

#### Scenario: behavior parity off the burst
- **WHEN** the preflight passes
- **THEN** `start` proceeds to `compose-up` exactly as before (the bundling changes crossing count, not the
  set of checks performed)

