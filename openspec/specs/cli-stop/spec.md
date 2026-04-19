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
The system SHALL revoke the `sandbox` user's ACL grants on `docker/` and `config/` after containers are confirmed down.

#### Scenario: sandbox ACL removed after stop
- **WHEN** `docker compose down` confirms all containers have exited
- **THEN** `setfacl -R -x u:<host_unprivileged_user>` is applied to `sandboxes/<id>/docker/` and `sandboxes/<id>/config/`
