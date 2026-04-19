## Purpose

This specification defines the `sandbox.toml` configuration file schema, governing schema generation on scaffold, Pydantic validation, component-conditional sub-table handling, and project name immutability warnings.

## Requirements

### Requirement: Schema Generation on Scaffold
The system SHALL write a valid `sandbox.toml` with all required fields and their defaults to the instance directory when a new instance is scaffolded.

#### Scenario: Auto-derived project name
- **WHEN** no `project_name` override is present in an existing `sandbox.toml`
- **THEN** the `project.name` field is set to `basename(abs(project_dir))`

#### Scenario: Auto-detected host UID
- **WHEN** scaffold writes a new `sandbox.toml`
- **THEN** the `project.host_uid` field is set to the UID of the invoking user (obtained via `os.getuid()`)

### Requirement: Pydantic Schema Validation
The system SHALL parse `sandbox.toml` through a Pydantic model before any lifecycle operation and fail with a structured validation error if the file is invalid.

#### Scenario: Missing required field
- **WHEN** `sandbox.toml` is missing a required field (e.g., `project.user_project_root`)
- **THEN** the CLI exits with a Pydantic `ValidationError` identifying the field and the reason before any state changes occur

#### Scenario: Unknown field rejection
- **WHEN** `sandbox.toml` contains a field not present in the Pydantic model
- **THEN** the CLI emits a warning (strict mode: error) identifying the unknown key

### Requirement: Component-Conditional Validation
The system SHALL apply sub-table validation only for components that are enabled in `[components]`.

#### Scenario: Disabled component skips sub-table validation
- **WHEN** `components.db_postgres = false`
- **THEN** the absence of `[components.db_postgres]` does NOT cause a validation error

#### Scenario: Enabled component requires sub-table
- **WHEN** `components.db_postgres = true` and `[components.db_postgres]` sub-table is absent
- **THEN** the Pydantic model applies defaults (the sub-table is not required to be explicitly present; defaults are sufficient)

### Requirement: Project Name Immutability Warning
The system SHALL emit a warning if `project.name` in `sandbox.toml` differs from the `project_name` component of the instance directory name.

#### Scenario: Renamed project.name detected
- **WHEN** `sandbox.toml` is read and `project.name` does not match the name portion of the instance directory
- **THEN** CLI emits: "WARNING: project.name has changed since init. COMPOSE_PROJECT_NAME mismatch may orphan running containers."
