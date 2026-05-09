## Purpose

This specification defines the `sandbox stop` command lifecycle, governing container shutdown, named volume preservation/removal, and ACL revocation.
## Requirements
### Requirement: Explicit Instance Argument

`sandbox stop <inst>` SHALL require the instance name as a positional argument. CWD-based instance discovery is removed.

#### Scenario: Instance argument required
- **WHEN** `sandbox stop` is invoked without an `<inst>` argument
- **THEN** the CLI exits with a typer "missing argument" error

### Requirement: Per-Instance Backup Lock Check

`sandbox stop <inst>` SHALL check `<inst>.backup.lock` after acquiring `state.lock` and refuse fast if held.

#### Scenario: Concurrent backup blocks stop
- **WHEN** `sandbox stop <inst>` is invoked while `<inst>.backup.lock` is held
- **THEN** stop exits with a "Backup in progress" error and exit code 1

### Requirement: Container Shutdown via machinectl
The system SHALL shut down the sandbox's containers by running `docker compose down` as the `docker_unprivileged_user` via `machinectl shell`, using the configured machinectl authentication mode from host config.

#### Scenario: Warm sandbox stopped cleanly (sudo mode)
- **WHEN** `sandbox stop` is invoked, containers are running, and `machinectl_authentication` is `"sudo"`
- **THEN** `sudo machinectl shell <user>@.host /bin/bash -c "docker compose down"` is executed

#### Scenario: Warm sandbox stopped cleanly (polkit mode)
- **WHEN** `sandbox stop` is invoked, containers are running, and `machinectl_authentication` is `"polkit"`
- **THEN** `machinectl shell <user>@.host /bin/bash -c "docker compose down"` is executed without `sudo` prefix

#### Scenario: Cold sandbox reports warning and exits
- **WHEN** `sandbox stop` is invoked and `docker compose ps -q` returns no output
- **THEN** the CLI emits a warning that no containers are running and exits with code 0

### Requirement: Concurrency Lock Acquisition on Stop
The system SHALL acquire the per-instance `state.lock` with `LOCK_NB` before executing stop operations. If the lock is held by a concurrent `start`, `stop` SHALL fail fast with a clear message.

#### Scenario: Stop acquires lock successfully
- **WHEN** `sandbox stop` is invoked and no other command holds the instance lock
- **THEN** the lock is acquired and stop proceeds normally

#### Scenario: Stop rejected during concurrent start
- **WHEN** `sandbox stop` is invoked while `sandbox start` holds the instance lock
- **THEN** the CLI exits immediately with: "Another sandbox operation is already in progress for this instance."

### Requirement: Compose Environment File on Stop
The system SHALL pass `--env-file <instance_dir>/.sandbox.env` to the `docker compose down` invocation during stop.

#### Scenario: --env-file on compose down
- **WHEN** `_compose_down` constructs the compose command during stop
- **THEN** the command includes `--env-file <instance_dir>/.sandbox.env`

### Requirement: Named Volume Preservation on Plain Stop
The system SHALL preserve all named Docker volumes when `sandbox stop` is invoked without `--clean`.

#### Scenario: Postgres data survives stop
- **WHEN** `sandbox stop` completes (without `--clean`)
- **THEN** the named volume for Postgres data (`<name>_db-postgres-data` or equivalent) still exists in the Docker volume list

### Requirement: Named Volume Removal on Clean Stop
The system SHALL remove all named Docker volumes when `sandbox stop --clean` is invoked.

#### Scenario: All volumes removed on clean stop
- **WHEN** `sandbox stop --clean` completes
- **THEN** `docker compose down -v` has been executed and all named volumes for the instance are absent from the Docker volume list

### Requirement: ACL Revocation After Shutdown

The system SHALL revoke the `<host_unprivileged_user>` named-ACL grants applied during `_phase_acl_grant` of `sandbox start`, after containers are confirmed down. The revoke set is the output of `_acl_revoke_plan()`, which covers:
- Instance root, `docker/` (recursive), `config/` (dir-level traverse), `secrets/` (dir-level traverse), `.sandbox.env`.
- For EACH workspace in `sandbox.toml [workspaces]`: BOTH the effective entry on `workspace.path` AND the named-entry portion of the workspace's default ACL (i.e., `setfacl -x u:<host_user> <ws.path>` and `setfacl -d -x u:<host_user> <ws.path>`).

The persistent portion of each workspace's default ACL (`u::rwx, g::rwx, o::---, m::rwx, u:dev:rwx`) is NOT revoked; per `orchestrator-volumes`'s `UID Paradox ACL Default Overrides` lifecycle taxonomy, the workspace shared-group state (chgrp + chmod 2770 + setgid + persistent default-ACL portion) is in the `granted-once, persistent` lifecycle.

The cache/log rw bind-mount sources (per `orchestrator-volumes`'s "Cache/Log Leaf Inventory" requirement) are NOT in the revoke set: they are subuid-chowned by the cache/log helper-recipe phase (in the `applied-on-every-start, idempotent, never-revoked` lifecycle) and carry no `u:<host_unprivileged_user>` named ACL to revoke.

Revocation SHALL use fault-isolated execution — each target attempted independently with failures reported as warnings.

#### Scenario: Workspace named-ACLs revoked per-workspace, symmetrically
- **WHEN** `docker compose down` confirms all containers have exited and the instance has workspaces `main` and `scratch`
- **THEN** `setfacl -x u:<host_user>` is applied to BOTH `<main.path>` and `<scratch.path>` (effective), AND `setfacl -d -x u:<host_user>` is applied to BOTH (default-ACL named entry); the persistent portion of each workspace's default ACL is preserved

#### Scenario: Instance dir set ACLs revoked
- **WHEN** `docker compose down` confirms exit
- **THEN** `setfacl -x u:<host_user>` is applied to `instances/<inst>/`, `instances/<inst>/docker/` (recursive), `instances/<inst>/config/` (dir-level), `instances/<inst>/secrets/` (dir-level), and `instances/<inst>/.sandbox.env`

#### Scenario: Cache/log rw bind-mount sources NOT in revoke set
- **WHEN** `_acl_revoke_plan()` is called
- **THEN** the returned target set does NOT include any leaf enumerated in `orchestrator-volumes`'s "Cache/Log Leaf Inventory" requirement

#### Scenario: Partial revocation failure reported as warning
- **WHEN** one or more ACL revocation targets fail during stop
- **THEN** the failure is reported as a warning and remaining targets are still attempted

### Requirement: Stop Does Not Revoke Helper-Recipe Operations
`sandbox stop <inst>` SHALL NOT revoke any operation performed by the cache/log helper-recipe phase, the ro-files helper-recipe phase, or the per-workspace shared-group phase's persistent operations (chgrp, chmod 2770 + setgid, persistent default ACL portion).

#### Scenario: Cache/log subuid ownership preserved across stop
- **WHEN** `sandbox stop <inst>` completes
- **THEN** cache/log leaves remain owned by the consumer subuid; agent state is preserved for the next start

#### Scenario: Ro-file consumer-uid:consumer-gid ownership preserved across stop
- **WHEN** `sandbox stop <inst>` completes
- **THEN** ro single-files (Corefile, dnsdist conf, proxy files, dotfiles, secrets) remain owned by `<consumer-uid>:<consumer-gid>` (consumer's host subuid + host subgid pair, per `orchestrator-volumes`'s `Ro single-files on-disk gid matches consumer's host subgid` scenario) with their respective modes

#### Scenario: Per-workspace shared-group state preserved across stop
- **WHEN** `sandbox stop <inst>` completes for an instance with multiple workspaces
- **THEN** EACH workspace retains its bridge-group ownership, mode 2770 + setgid, and persistent default ACL portion; only the named ACLs (effective + default named entry) on each workspace are revoked

### Requirement: Per-User State Initialization Required
The `sandbox stop` command SHALL refuse to operate when the per-user state tree is not initialized. Initialization is signaled by the presence of `<sandbox_ai_user_home()>/state/instances.json`. On absence, the command SHALL exit with a clear error directing the operator to run `sandbox init`.

#### Scenario: Stop on uninitialized host
- **WHEN** `sandbox stop` is invoked and `<home>/state/instances.json` does not exist
- **THEN** the CLI exits with: "Error: per-user state not initialized at `<resolved-home>`. Run `sandbox init` first." and exit code 1

#### Scenario: Resolved home in error message
- **WHEN** the stop command above runs with `SANDBOX_AI_USER_HOME=/tmp/test-home` set
- **THEN** the error message contains `/tmp/test-home`

