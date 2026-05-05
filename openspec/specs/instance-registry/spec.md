## Purpose

This specification defines the instance registry that maps absolute project directories to stable sandbox instance identifiers, enabling multi-instance discovery and lifecycle management.

## Requirements

### Requirement: Instance Registration
The system SHALL persist a mapping from the absolute working directory path to a stable instance identifier in `<sandbox_ai_user_home()>/state/instances.json`. The registry is per-user (shared across all working directories of the same user), not per-CWD.

#### Scenario: First invocation creates registry entry
- **WHEN** `sandbox start` is invoked from a working directory not present in `instances.json`
- **THEN** a new entry is written mapping `abs(cwd)` to the generated `instance_id` (`<instance_name>-<hash_hex>`) before any containers are started

#### Scenario: Subsequent invocations reuse existing entry
- **WHEN** `sandbox start`, `stop`, `attach`, or `destroy` is invoked from a registered working directory
- **THEN** the `instance_dir` is resolved from `instances.json` without recomputing or re-deriving the path

#### Scenario: Two CWDs share the registry but have separate entries
- **WHEN** `sandbox start` is invoked from `/home/alice/projects/foo` and (separately) from `/home/alice/projects/bar`
- **THEN** both entries appear in the same `<home>/state/instances.json` file, keyed by their respective absolute paths; the registry is shared because it is per-user

### Requirement: Unregistered Project Rejection
The system SHALL reject lifecycle commands other than `start` when the project directory is not present in the registry.

#### Scenario: Non-start command on unknown project
- **WHEN** `sandbox stop`, `attach`, or `destroy` is invoked for a project directory that has no entry in `instances.json`
- **THEN** the CLI exits with a clear error: "No sandbox instance found for this directory. Use 'sandbox start' to initialize."

### Requirement: Registry Lock Safety
The system SHALL guard all reads and writes to `instances.json` against concurrent modification using `<sandbox_ai_user_home()>/state/state.lock`. The lock is per-user; concurrent `sandbox` invocations across any working directories under the same user serialize through the same lock file.

#### Scenario: Concurrent start from different CWDs serializes
- **WHEN** two `sandbox start` invocations run concurrently for different working directories under the same user
- **THEN** they serialize on `<home>/state/state.lock`; both registry writes complete without corruption and both entries are present in `instances.json` afterward

#### Scenario: Lock is transient, not held during runtime
- **WHEN** a `sandbox start` provisioning phase completes and containers are running
- **THEN** `state.lock` is released; running sandboxes do not hold the lock during their runtime, allowing parallelism across instances at runtime

### Requirement: Registry Entry Removal on Destroy
The system SHALL remove the registry entry for a destroyed instance to free the identifier for potential reuse.

#### Scenario: Destroy removes registry entry
- **WHEN** `sandbox destroy` completes successfully
- **THEN** the `instances.json` entry for the destroyed instance's `abs(project_dir)` is removed
