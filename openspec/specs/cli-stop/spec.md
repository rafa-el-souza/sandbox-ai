## Purpose

This specification defines the `sandbox stop` command lifecycle, governing container shutdown, named volume preservation/removal, and ACL revocation.

## Requirements

### Requirement: Container Shutdown via machinectl
The system SHALL shut down the sandbox's containers by running `docker compose down` as the `host_unprivileged_user` via `machinectl shell`.

#### Scenario: Warm sandbox stopped cleanly
- **WHEN** `sandbox stop` is invoked and containers are running
- **THEN** `docker compose down` is executed via machinectl, blocking until all containers confirm exit, and named volumes are preserved

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
The system SHALL revoke the `sandbox` user's ACL grants on instance root, `docker/`, `config/`, `.sandbox.env`, and rw bind-mount source subdirectories (`cache/core/.claude`, `cache/admin/tmux_resurrect`, `log/core`, `log/admin`) after containers are confirmed down. Revocation of rw bind-mount sources SHALL remove both effective and default ACL entries. Revocation SHALL use fault-isolated execution — each target attempted independently with failures reported as warnings.

#### Scenario: sandbox ACL removed after stop
- **WHEN** `docker compose down` confirms all containers have exited
- **THEN** `setfacl -x u:<host_unprivileged_user>` is applied independently to `sandboxes/<id>/`, `sandboxes/<id>/docker/` (recursive), `sandboxes/<id>/config/` (recursive), and `sandboxes/<id>/.sandbox.env`

#### Scenario: rw bind-mount source ACLs revoked after stop
- **WHEN** `docker compose down` confirms all containers have exited
- **THEN** `setfacl -R -x u:<host_unprivileged_user>` (effective) and `setfacl -R -d -x u:<host_unprivileged_user>` (default) are applied independently to `cache/core/.claude`, `cache/admin/tmux_resurrect`, `log/core`, and `log/admin`

#### Scenario: Partial revocation failure reported as warning
- **WHEN** one or more ACL revocation targets fail during stop
- **THEN** the failure is reported as a warning and remaining targets are still attempted
