## Purpose

This specification defines the `sandbox stop` command lifecycle, governing container shutdown, named volume preservation/removal, and ACL revocation.

## Requirements

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
The system SHALL revoke the `<host_unprivileged_user>` named-ACL grants applied during Phase 5 of `sandbox start`, after containers are confirmed down. The revoke set is the output of `_acl_revoke_plan()`, which per `orchestrator-volumes` covers: instance root, `docker/` (recursive), `config/` (dir-level traverse), `secrets/` (dir-level traverse), `.sandbox.env`, AND the workspace mount's named ACL — both the effective entry on `user_project_root` AND the named-entry portion of the workspace's default ACL. Revocation SHALL use fault-isolated execution — each target attempted independently with failures reported as warnings.

The cache/log rw bind-mount sources (`cache/core/.claude`, `cache/admin/tmux_resurrect`, `log/core`, `log/admin`) are NOT in the revoke set: post-change-4, they are subuid-chowned (no `u:<host_unprivileged_user>` named ACL exists on them), so there is nothing to revoke. Their persistent state is preserved across stop per `orchestrator-volumes`'s lifecycle taxonomy.

#### Scenario: sandbox ACL removed from instance dir set after stop
- **WHEN** `docker compose down` confirms all containers have exited
- **THEN** `setfacl -x u:<host_unprivileged_user>` is applied independently to `sandboxes/<id>/`, `sandboxes/<id>/docker/` (recursive), `sandboxes/<id>/config/` (dir-level), `sandboxes/<id>/secrets/` (dir-level), and `sandboxes/<id>/.sandbox.env`

#### Scenario: workspace named-ACL revoked symmetrically (effective + default)
- **WHEN** `docker compose down` confirms all containers have exited
- **THEN** `setfacl -x u:<host_unprivileged_user> <user_project_root>` AND `setfacl -d -x u:<host_unprivileged_user> <user_project_root>` are both applied; the persistent portion of the workspace's default ACL (`u::rwx, g::rwx, o::---, m::rwx, u:dev:rwx`) is preserved

#### Scenario: cache/log rw bind-mount sources NOT in revoke set
- **WHEN** `_acl_revoke_plan()` is called
- **THEN** the returned target set does NOT include `cache/core/.claude`, `cache/admin/tmux_resurrect`, `log/core`, or `log/admin`; these are subuid-chowned (no named ACL to revoke) and their state is preserved across stop

#### Scenario: Partial revocation failure reported as warning
- **WHEN** one or more ACL revocation targets fail during stop
- **THEN** the failure is reported as a warning and remaining targets are still attempted

### Requirement: Stop Does Not Revoke Helper-Recipe Operations

`sandbox stop` SHALL NOT revoke any operation performed by the cache/log helper-recipe phase, the ro-files helper-recipe phase, or the workspace shared-group phase's persistent operations (chgrp, chmod 2770, persistent default ACL portion). These are in the `granted-once, persistent` or `applied-on-every-start, idempotent, never-revoked` lifecycles per `orchestrator-volumes`.

#### Scenario: Cache/log subuid ownership preserved across stop
- **WHEN** `sandbox stop` completes
- **THEN** cache/log leaves remain owned by the consumer subuid; agent state is preserved for the next start

#### Scenario: Ro-file consumer-uid:0 ownership preserved across stop
- **WHEN** `sandbox stop` completes
- **THEN** ro single-files (Corefile, dnsdist conf, proxy files, dotfiles, secrets) remain owned by `<consumer-uid>:0` with their respective modes; the next start re-renders them dev-owned and re-chowns them via the helper phase

#### Scenario: Workspace shared-group state preserved across stop
- **WHEN** `sandbox stop` completes
- **THEN** `<user_project_root>` retains its bridge-group ownership, mode 2770 + setgid, and the persistent default ACL portion; only the named ACL is revoked


### Requirement: Per-User State Initialization Required
The `sandbox stop` command SHALL refuse to operate when the per-user state tree is not initialized. Initialization is signaled by the presence of `<sandbox_ai_user_home()>/state/instances.json`. On absence, the command SHALL exit with a clear error directing the operator to run `sandbox init`.

#### Scenario: Stop on uninitialized host
- **WHEN** `sandbox stop` is invoked and `<home>/state/instances.json` does not exist
- **THEN** the CLI exits with: "Error: per-user state not initialized at `<resolved-home>`. Run `sandbox init` first." and exit code 1

#### Scenario: Resolved home in error message
- **WHEN** the stop command above runs with `SANDBOX_AI_USER_HOME=/tmp/test-home` set
- **THEN** the error message contains `/tmp/test-home`
