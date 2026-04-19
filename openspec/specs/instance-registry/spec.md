## Purpose

This specification defines the instance registry that maps absolute project directories to stable sandbox instance identifiers, enabling multi-instance discovery and lifecycle management.

## Requirements

### Requirement: Instance Registration
The system SHALL persist a mapping from the absolute project directory path to a stable instance identifier in `SANDBOX_AI_HOME/.state/instances.json`.

#### Scenario: First invocation creates registry entry
- **WHEN** `sandbox start` is invoked for a project directory not present in `instances.json`
- **THEN** a new entry is written mapping `abs(project_dir)` to the generated `instance_id` (`<project_name>-<project_id>`) before any containers are started

#### Scenario: Subsequent invocations reuse existing entry
- **WHEN** `sandbox start`, `stop`, `attach`, or `destroy` is invoked for a registered project directory
- **THEN** the `instance_dir` is resolved from `instances.json` without recomputing or re-deriving the path

### Requirement: Unregistered Project Rejection
The system SHALL reject lifecycle commands other than `start` when the project directory is not present in the registry.

#### Scenario: Non-start command on unknown project
- **WHEN** `sandbox stop`, `attach`, or `destroy` is invoked for a project directory that has no entry in `instances.json`
- **THEN** the CLI exits with a clear error: "No sandbox instance found for this directory. Use 'sandbox start' to initialize."

### Requirement: Registry Lock Safety
The system SHALL guard all reads and writes to `instances.json` against concurrent modification.

#### Scenario: Concurrent start for different projects
- **WHEN** two `sandbox start` invocations for different project directories execute concurrently
- **THEN** both registry writes complete without corruption and both entries are present in `instances.json` afterward

### Requirement: Registry Entry Removal on Destroy
The system SHALL remove the registry entry for a destroyed instance to free the identifier for potential reuse.

#### Scenario: Destroy removes registry entry
- **WHEN** `sandbox destroy` completes successfully
- **THEN** the `instances.json` entry for the destroyed instance's `abs(project_dir)` is removed
