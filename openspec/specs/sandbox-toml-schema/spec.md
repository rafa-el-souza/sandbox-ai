## Purpose

This specification defines the `sandbox.toml` configuration file schema, governing schema generation on scaffold, Pydantic validation, component-conditional sub-table handling, and project name immutability warnings.

## Requirements

### Requirement: Schema Generation on Scaffold
The system SHALL write a valid `sandbox.toml` with all required fields and their defaults to the instance directory when a new instance is scaffolded. The `[components.db_postgres]` sub-table SHALL include `pg_user` and `pg_db` fields with defaults. The `[core]` section SHALL include `mem_limit` and `cpus` fields with defaults. The `[admin]` section SHALL include `mem_limit` and `cpus` fields with defaults.

#### Scenario: Auto-derived project name
- **WHEN** no `project_name` override is present in an existing `sandbox.toml`
- **THEN** the `project.name` field is set to `basename(abs(project_dir))`

#### Scenario: Auto-detected host UID
- **WHEN** scaffold writes a new `sandbox.toml`
- **THEN** the `project.host_uid` field is set to the UID of the invoking user (obtained via `os.getuid()`)

#### Scenario: Database config defaults include pg_user and pg_db
- **WHEN** scaffold writes a new `sandbox.toml` with `components.db_postgres.enabled = true`
- **THEN** the `[components.db_postgres]` sub-table includes `pg_user = "sandbox"` and `pg_db = "sandbox_db"` as defaults

#### Scenario: Core resource limit defaults
- **WHEN** scaffold writes a new `sandbox.toml`
- **THEN** the `[core]` section includes `mem_limit = "8gb"` and `cpus = 4.0` as defaults

#### Scenario: Admin resource limit defaults
- **WHEN** scaffold writes a new `sandbox.toml`
- **THEN** the `[admin]` section includes `mem_limit = "8gb"` and `cpus = 4.0` as defaults

### Requirement: Pydantic Schema Validation
The system SHALL parse `sandbox.toml` through a Pydantic model before any lifecycle operation and fail with a structured validation error if the file is invalid. The `DbPostgresConfig` model SHALL validate `pg_user` and `pg_db` as non-empty strings. The `CoreConfig` model SHALL validate `mem_limit` as a non-empty string and `cpus` as a positive float. The `AdminConfig` model SHALL validate `mem_limit` as a non-empty string and `cpus` as a positive float.

#### Scenario: Missing required field
- **WHEN** `sandbox.toml` is missing a required field (e.g., `project.user_project_root`)
- **THEN** the CLI exits with a Pydantic `ValidationError` identifying the field and the reason before any state changes occur

#### Scenario: Unknown field rejection
- **WHEN** `sandbox.toml` contains a field not present in the Pydantic model
- **THEN** the CLI emits a warning (strict mode: error) identifying the unknown key

#### Scenario: DbPostgresConfig validates pg_user and pg_db
- **WHEN** `sandbox.toml` includes `[components.db_postgres]` with `pg_user` or `pg_db` fields
- **THEN** the Pydantic model validates them as strings and applies defaults if absent

#### Scenario: CoreConfig validates mem_limit and cpus
- **WHEN** `sandbox.toml` includes `[core]` with `mem_limit` or `cpus` fields
- **THEN** the Pydantic model validates `mem_limit` as a string and `cpus` as a float, applying defaults (`"8gb"`, `4.0`) if absent

#### Scenario: AdminConfig validates mem_limit and cpus
- **WHEN** `sandbox.toml` includes `[admin]` with `mem_limit` or `cpus` fields
- **THEN** the Pydantic model validates `mem_limit` as a string and `cpus` as a float, applying defaults (`"8gb"`, `4.0`) if absent

#### Scenario: Backward compatibility with existing sandbox.toml
- **WHEN** an existing `sandbox.toml` omits `mem_limit` and `cpus` from `[core]` and `[admin]`
- **THEN** Pydantic applies defaults without validation errors — the fields are optional with defaults

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
