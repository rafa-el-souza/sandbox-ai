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

### Requirement: Destroy Phase Ordering
The system SHALL execute destroy phases in dependency order: hard resources (containers, ACLs, directory) before soft state (IPAM, registry). This ordering SHALL ensure that partial failures leave the instance in a retryable state.

#### Scenario: Phase execution order
- **WHEN** `sandbox destroy` executes after confirmation and lock acquisition
- **THEN** phases execute in order: compose-down → ACL revoke → rmtree → IPAM release → registry remove

#### Scenario: rmtree failure preserves registry and IPAM
- **WHEN** `shutil.rmtree(instance_dir)` fails with `PermissionError`
- **THEN** IPAM release and registry removal are NOT attempted, and the operator can retry `sandbox destroy`

#### Scenario: IPAM/registry failure after rmtree
- **WHEN** `shutil.rmtree` succeeds but IPAM release or registry removal fails
- **THEN** the failure is reported as a warning (primary resource — the directory — is already removed)

### Requirement: Compose Environment File on Destroy
The system SHALL pass `--env-file <instance_dir>/.sandbox.env` to the `docker compose down -v` invocation during destroy.

#### Scenario: --env-file on compose down during destroy
- **WHEN** `_compose_down` constructs the compose command during destroy
- **THEN** the command includes `--env-file <instance_dir>/.sandbox.env`

### Requirement: Unconditional Volume Teardown
The system SHALL run `docker compose down -v` regardless of whether containers are currently running. Teardown failure SHALL NOT abort subsequent destroy phases.

#### Scenario: Warm instance torn down completely
- **WHEN** `sandbox destroy` is confirmed and containers are running
- **THEN** `docker compose down -v` removes all containers, networks, and named volumes

#### Scenario: Cold instance volumes removed idempotently
- **WHEN** `sandbox destroy` is confirmed and containers are already stopped
- **THEN** `docker compose down -v` still removes any remaining named volumes and networks without error

#### Scenario: Compose teardown failure continues destroy
- **WHEN** `docker compose down -v` raises `SandboxExecutionError` (e.g., no compose file, Docker daemon unreachable)
- **THEN** the error is reported as a warning and destroy proceeds to ACL revocation

### Requirement: IPAM and Registry Cleanup
The system SHALL free the IPAM `base_index` slot and remove the registry entry as part of destroy, enabling both to be reused by future instances. Each cleanup operation SHALL be independently fault-isolated.

#### Scenario: IPAM slot freed after destroy
- **WHEN** `sandbox destroy` reaches the IPAM release phase
- **THEN** the `project_id` entry is removed from `ipam.json` and that `base_index` is available for allocation to a new instance

#### Scenario: Registry entry removed after destroy
- **WHEN** `sandbox destroy` reaches the registry removal phase
- **THEN** the `abs(project_dir)` entry is removed from `instances.json`

#### Scenario: IPAM release failure reported as warning
- **WHEN** IPAM release raises an exception
- **THEN** the error is reported as a warning and registry removal is still attempted

#### Scenario: Registry removal failure reported as warning
- **WHEN** registry removal raises an exception
- **THEN** the error is reported as a warning

### Requirement: Safe Instance Directory Removal
The system SHALL verify the instance directory path before deletion and SHALL NOT touch the user's project directory. The system SHALL handle idempotent destroy gracefully.

#### Scenario: Path prefix guard prevents runaway rmtree
- **WHEN** `sandbox destroy` is about to call `shutil.rmtree(instance_dir)`
- **THEN** the CLI first asserts that `instance_dir` starts with `SANDBOX_AI_HOME/sandboxes/`; if not, it aborts with an error

#### Scenario: user_project_root is never removed
- **WHEN** `sandbox destroy` completes
- **THEN** the directory referenced by `project.user_project_root` in `sandbox.toml` is unmodified

#### Scenario: Already-deleted directory handled silently
- **WHEN** `shutil.rmtree(instance_dir)` raises `FileNotFoundError`
- **THEN** the error is suppressed (directory already removed = success condition)

#### Scenario: Permission error propagates
- **WHEN** `shutil.rmtree(instance_dir)` raises `PermissionError`
- **THEN** the error propagates to the caller (defense-in-depth — operator must investigate)
