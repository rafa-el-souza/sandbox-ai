## Purpose

This specification governs the absolute filesystem boundary constraints separating the Human Host repository from the deeply mapped gVisor-native execution matrices. It enforces structural mitigations resolving the rootless SubUID paradox (`setfacl`), establishes topological bifurcation arrays separating mutable workspace metrics from immutable template planes (`~/.sandbox/`), and dictates unrecoverable VFS volume annihilation procedures natively.

## Requirements

### Requirement: UID Paradox ACL Default Overrides
The system SHALL definitively execute mathematical POSIX permission checks seamlessly managing Human `dev` to Daemon `sandbox` directory crossings.

#### Scenario: Default Directory Control Bounding
- **WHEN** the Python Orchestrator maps a local codebase into the static Docker VFS context natively
- **THEN** it executes `setfacl -d -m u:dev:rwx` upon the human Host's repository path prior to container initialization, explicitly securing structural modifications without trapping files under the unprivileged rootless Daemon `sandbox` subUID ownership constraints.

#### Scenario: Ephemeral Teardown Dis-Connection
- **WHEN** the Orchestrator explicitly receives a targeted terminate or stop command
- **THEN** the script initiates an un-recoverable traversal purge natively calling `setfacl -R -x u:sandbox`, logically severing the Daemon's mapping capability from Host workspace variables rapidly.

### Requirement: Topographical File Isolation Boundaries
The system SHALL ruthlessly enforce geometric separations distinguishing mutable localized artifacts from global immutable templates.

#### Scenario: The Immutable Tooling Plane (`~/.sandbox/`)
- **WHEN** the Orchestrator configures standard infrastructure bindings directly against `~/.sandbox/docker/` components
- **THEN** it strictly maps the array natively as `read-only (ro)`, structurally ensuring a rogue AI payload inside an isolated workspace context cannot dynamically corrupt the foundational `docker/compose.yml` baseline metrics.

#### Scenario: The Mutable Project Plane (`./.sandbox/`)
- **WHEN** establishing core diagnostic or internal proxy sidecar bindings natively
- **THEN** the CLI structurally remaps telemetry footprints strictly to localized directories (`.sandbox/logs/proxy/`, `.sandbox/cache/core/`, AND explicitly routes `.sandbox/logs/orchestrator/orchestrator.log` for secure POSIX execution traces), thoroughly eliminating overlapping diagnostic streams tracking back to global file matrices negatively impacting host storage.

#### Scenario: Human Shell History Isolation
- **WHEN** mapping the human operator's `admin` and `core` environments
- **THEN** the Orchestrator mechanically bounds `.zsh_history` and `.bash_history` volume arrays directly into localized `./.sandbox/logs/admin/` and `./.sandbox/logs/core/` paths, preventing terminal history drift or `.bashrc` host cross-contamination natively.

### Requirement: Deep VFS Annihilation
The system SHALL natively intercept infrastructure unrolls preventing lingering Docker volume data traps.

#### Scenario: The `--clean` Flag Termination Sequence
- **WHEN** a Human explicitly types `$ sandbox stop --clean`
- **THEN** the Orchestrator dictates a targeted sequence bypassing basic stops natively executing an unrecoverable `docker compose down -v` destruction layer, definitively wiping named cache sets (Postgres Volumes) back to structural day zero states.
