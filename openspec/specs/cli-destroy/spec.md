## Purpose

This specification defines the `sandbox destroy` command lifecycle, governing confirmation, unconditional volume teardown, IPAM/registry cleanup, and safe instance directory removal.

## Requirements

### Requirement: Confirmation Required Before Destruction
The system SHALL require explicit confirmation before executing any irreversible destroy operation.

#### Scenario: Name-typing confirmation prevents accidental destroy
- **WHEN** `sandbox destroy` is invoked without `--force` and the user types a name that does not match the sandbox name
- **THEN** the CLI aborts with no state changes and exits 0

#### Scenario: Force flag bypasses confirmation
- **WHEN** `sandbox destroy --force` is invoked
- **THEN** the CLI proceeds without prompting for name confirmation

### Requirement: Unconditional Volume Teardown
The system SHALL run `docker compose down -v` regardless of whether containers are currently running.

#### Scenario: Warm instance torn down completely
- **WHEN** `sandbox destroy` is confirmed and containers are running
- **THEN** `docker compose down -v` removes all containers, networks, and named volumes

#### Scenario: Cold instance volumes removed idempotently
- **WHEN** `sandbox destroy` is confirmed and containers are already stopped
- **THEN** `docker compose down -v` still removes any remaining named volumes and networks without error

### Requirement: IPAM and Registry Cleanup
The system SHALL free the IPAM `base_index` slot and remove the registry entry as part of destroy, enabling both to be reused by future instances.

#### Scenario: IPAM slot freed after destroy
- **WHEN** `sandbox destroy` completes
- **THEN** the `project_id` entry is removed from `ipam.json` and that `base_index` is available for allocation to a new instance

#### Scenario: Registry entry removed after destroy
- **WHEN** `sandbox destroy` completes
- **THEN** the `abs(project_dir)` entry is removed from `instances.json`

### Requirement: Safe Instance Directory Removal
The system SHALL verify the instance directory path before deletion and SHALL NOT touch the user's project directory.

#### Scenario: Path prefix guard prevents runaway rmtree
- **WHEN** `sandbox destroy` is about to call `shutil.rmtree(instance_dir)`
- **THEN** the CLI first asserts that `instance_dir` starts with `SANDBOX_AI_HOME/sandboxes/`; if not, it aborts with an error

#### Scenario: user_project_root is never removed
- **WHEN** `sandbox destroy` completes
- **THEN** the directory referenced by `project.user_project_root` in `sandbox.toml` is unmodified
