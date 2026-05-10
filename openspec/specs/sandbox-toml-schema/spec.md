## Purpose

This specification defines the `sandbox.toml` configuration file schema, governing schema generation on scaffold, Pydantic validation, component-conditional sub-table handling, and project name immutability warnings.

## Requirements

### Requirement: Workspaces Map-of-Tables

The `sandbox.toml` schema SHALL include a `[workspaces]` map-of-tables section. Each entry's value is a `WorkspaceConfig` Pydantic model with three fields: `bootstrap_mode: BootstrapMode`, `source: str | None`, `path: str`. The `BootstrapMode` enum has two members: `COPY = "copy"` and `EMPTY = "empty"`. The map-of-tables shape (`[workspaces.<name>]`) ensures TOML-parser-enforced uniqueness of workspace names within the instance.

#### Scenario: Workspaces map parses
- **WHEN** sandbox.toml contains `[workspaces.main]` with `bootstrap_mode = "copy"`, `source = "/p"`, `path = "/q"`
- **THEN** `SandboxInstanceSection.workspaces["main"]` is a `WorkspaceConfig` with those values

#### Scenario: BootstrapMode enum rejects unknown values
- **WHEN** sandbox.toml contains `bootstrap_mode = "clone"` (or any other unknown value)
- **THEN** Pydantic validation raises a structured error identifying the invalid enum value

#### Scenario: Empty workspaces map is rejected
- **WHEN** sandbox.toml contains an empty `[workspaces]` section (no nested entries)
- **THEN** Pydantic validation raises an error (an instance must have at least one workspace)

#### Scenario: Source field optional and conditional
- **WHEN** sandbox.toml contains `bootstrap_mode = "empty"` without a `source` field
- **THEN** `WorkspaceConfig.source` is `None`; validation passes

#### Scenario: Source required for copy mode
- **WHEN** sandbox.toml contains `bootstrap_mode = "copy"` without a `source` field
- **THEN** Pydantic validation raises an error

#### Scenario: Path field always required
- **WHEN** sandbox.toml contains a `[workspaces.<ws>]` entry without a `path` field
- **THEN** Pydantic validation raises an error

### Requirement: Schema Generation on Scaffold
The system SHALL write a valid `sandbox.toml` with all required fields and their defaults to the instance directory when a new instance is scaffolded. The `[instance]` section SHALL NOT include `host_unprivileged_user` or `user_project_root`. The `[workspaces]` section SHALL include one entry per workspace specified at `sandbox init` time (defaulting to a single `[workspaces.main]` with `bootstrap_mode = "empty"` when no `--copy`/`--empty` flags are supplied). The `[components.db_postgres]` sub-table SHALL include `pg_user`, `pg_db`, and `image` fields with defaults. The `[core]` section SHALL include `mem_limit`, `cpus`, and `base_image` fields with defaults. The `[proxy.whitelist]` section SHALL include `read_only_domains` with default package registry domains. All image defaults SHALL use SHA256 digest references. The scaffolded `sandbox.toml` SHALL NOT contain an `[admin]` section.

#### Scenario: Auto-derived instance name
- **WHEN** scaffold writes a new `sandbox.toml`
- **THEN** the `instance.name` field is set to the `<inst>` argument supplied to `sandbox init`

#### Scenario: Auto-detected host UID
- **WHEN** scaffold writes a new `sandbox.toml`
- **THEN** the `instance.host_uid` field is set to the UID of the invoking user (obtained via `os.getuid()`)

#### Scenario: host_unprivileged_user absent from scaffold output
- **WHEN** scaffold writes a new `sandbox.toml`
- **THEN** the `[instance]` section does NOT contain a `host_unprivileged_user` field

#### Scenario: user_project_root absent from scaffold output
- **WHEN** scaffold writes a new `sandbox.toml`
- **THEN** the `[instance]` section does NOT contain a `user_project_root` field

#### Scenario: Default workspace named main
- **WHEN** `sandbox init <inst>` is invoked with no `--copy`/`--empty` flags
- **THEN** the scaffolded `sandbox.toml` contains exactly one workspace `[workspaces.main]` with `bootstrap_mode = "empty"`, no `source` field, and `path` set to `~/.sandbox-ai/workspaces/<inst>/main`

#### Scenario: Multiple workspaces from CLI flags
- **WHEN** `sandbox init foo --copy a=/p1 --empty b --copy c=/p2` is invoked
- **THEN** the scaffolded `sandbox.toml` contains three workspaces (`a`, `b`, `c`) with the appropriate `bootstrap_mode`/`source`/`path` values

#### Scenario: Database config defaults include pg_user, pg_db, and image
- **WHEN** scaffold writes a new `sandbox.toml` with `components.db_postgres.enabled = true`
- **THEN** the `[components.db_postgres]` sub-table includes `pg_user = "sandbox"`, `pg_db = "sandbox_db"`, and `image` with a SHA256-pinned postgres digest as defaults

#### Scenario: Core resource limit defaults
- **WHEN** scaffold writes a new `sandbox.toml`
- **THEN** the `[core]` section includes `mem_limit = "8gb"`, `cpus = 4.0`, and `base_image` with a SHA256-pinned wolfi-base digest as defaults

#### Scenario: Admin section absent from scaffold output
- **WHEN** scaffold writes a new `sandbox.toml`
- **THEN** the file does NOT contain an `[admin]` section (admin's image is `FROM scratch` + a static `/fwd` binary; there are no configurable runtime knobs)

#### Scenario: Read-only domains defaults in scaffold
- **WHEN** scaffold writes a new `sandbox.toml`
- **THEN** the `[proxy.whitelist]` section includes `read_only_domains` with package registry domains

#### Scenario: Backward compatibility with existing sandbox.toml missing read_only_domains and image
- **WHEN** an existing `sandbox.toml` omits `read_only_domains` from `[proxy.whitelist]` and `image` from `[components.db_postgres]`
- **THEN** Pydantic applies defaults without validation errors — the fields are optional with defaults

### Requirement: Pydantic Schema Validation
The system SHALL parse `sandbox.toml` through a Pydantic model before any lifecycle operation and fail with a structured validation error if the file is invalid. The `[instance]` section's Pydantic model class SHALL be named `SandboxInstanceSection` to disambiguate from the per-host `HostConfig` model. The `SandboxInstanceSection` model SHALL NOT contain `host_unprivileged_user` or `user_project_root` (the latter removed in change 5; replaced by `[workspaces]`). The `SandboxInstanceSection` model SHALL contain `workspaces: dict[str, WorkspaceConfig]` (non-empty dict). The `DbPostgresConfig` model SHALL validate `pg_user` and `pg_db` as non-empty strings. The `CoreConfig` model SHALL validate `mem_limit` as a non-empty string and `cpus` as a positive float. The model SHALL NOT define an `AdminConfig` class and SHALL NOT recognize an `[admin]` top-level section.

When `_load_config` (or any other CLI-side TOML loader for `sandbox.toml`) catches a `pydantic.ValidationError`, the CLI SHALL emit one user-facing message per error, formatted as a single line: `Invalid <toml-path>: <field-path>: <error-message>`, where `<toml-path>` is the absolute path to the offending `sandbox.toml`, `<field-path>` is the dotted location of the failing field (joining the entries of `error["loc"]` with `.`), and `<error-message>` is the Pydantic-supplied message. The CLI SHALL NOT display Pydantic's default `__str__` representation (which includes library internals, line numbers, and JSON URLs); the raw exception SHALL be suppressed (e.g., `raise typer.Exit(1) from None`). The wrap point SHALL be `_load_config` itself, so all callers benefit without per-command boilerplate.

The `try`/`except` clause SHALL match `pydantic.ValidationError` specifically and SHALL NOT match bare `Exception`. Other exceptions raised during TOML loading or validation (e.g., `FileNotFoundError`, `OSError`, `tomllib.TOMLDecodeError`) SHALL propagate to the caller with their full traceback intact, unmodified by `_load_config`. This narrow-catch contract guarantees that the friendly-formatting path applies only to schema validation failures and does not over-suppress unrelated error classes.

#### Scenario: Missing required field
- **WHEN** `sandbox.toml` is missing a required field (e.g., a workspace's `path` or `bootstrap_mode`)
- **THEN** the CLI emits a single line of the form `Invalid <abs-path-to-sandbox.toml>: workspaces.<ws>.path: Field required` and exits with code 1; the Pydantic traceback is not displayed

#### Scenario: Multiple validation errors emit multiple lines
- **WHEN** `sandbox.toml` has more than one validation failure (e.g., missing `path` AND invalid `cpus` type)
- **THEN** the CLI emits one `Invalid <abs-path>: <field>: <reason>` line per error in the order Pydantic reports them, then exits with code 1; no Pydantic traceback is displayed

#### Scenario: Empty workspaces map produces formatted error
- **WHEN** an externally hand-edited `sandbox.toml` has an empty `[workspaces]` section, and any command that calls `_load_config` is invoked
- **THEN** the CLI emits `Invalid <abs-path>: workspaces: Dictionary should have at least 1 item after validation, not 0` (or the equivalent Pydantic-supplied message) and exits with code 1; the Pydantic traceback is not displayed

#### Scenario: Non-validation exceptions propagate with full traceback
- **WHEN** `_load_config` is invoked against an instance whose `sandbox.toml` does not exist (raising `FileNotFoundError`/`OSError`) or contains a TOML syntax error (raising `tomllib.TOMLDecodeError`)
- **THEN** the exception propagates to the caller unmodified, with its original traceback intact; the `_load_config` `try`/`except` does NOT catch it, NOT format it, and NOT suppress its chain

#### Scenario: Unknown field rejection
- **WHEN** `sandbox.toml` contains a field not present in the Pydantic model
- **THEN** the CLI emits a warning (strict mode: error) identifying the unknown key

#### Scenario: DbPostgresConfig validates pg_user and pg_db
- **WHEN** `sandbox.toml` includes `[components.db_postgres]` with `pg_user` or `pg_db` fields
- **THEN** the Pydantic model validates them as strings and applies defaults if absent

#### Scenario: CoreConfig validates mem_limit and cpus
- **WHEN** `sandbox.toml` includes `[core]` with `mem_limit` or `cpus` fields
- **THEN** the Pydantic model validates `mem_limit` as a string and `cpus` as a float, applying defaults (`"8gb"`, `4.0`) if absent

#### Scenario: Workspaces map required
- **WHEN** `sandbox.toml` lacks a `[workspaces]` section entirely OR contains an empty `[workspaces]` section
- **THEN** Pydantic raises a validation error (instances must have at least one workspace)

#### Scenario: Backward compatibility with existing sandbox.toml
- **WHEN** an existing `sandbox.toml` omits `mem_limit` and `cpus` from `[core]`
- **THEN** Pydantic applies defaults without validation errors — the fields are optional with defaults

### Requirement: Legacy `[admin]` Section Rejection
Existing `sandbox.toml` files MAY contain a legacy `[admin]` table from before the admin-reframe change (when admin was a Debian-based interactive-shell container with configurable `mem_limit`, `cpus`, and `base_image`). Post-reframe, `[admin]` is not a recognized section: admin is `FROM scratch` + a single static `/fwd` binary with no configurable runtime knobs. When `_load_config` parses a `sandbox.toml` containing an `[admin]` table, the Pydantic `SandboxInstanceSection`/`InstanceConfig` validation SHALL reject the input with a clear error message instructing the operator to remove the section.

#### Scenario: Legacy `[admin]` table is rejected on load
- **WHEN** `_load_config` parses a `sandbox.toml` whose top-level contains an `[admin]` table (e.g., from a pre-upgrade instance)
- **THEN** Pydantic raises a validation error identifying `admin` as an unknown section, and the CLI emits a single line of the form `Invalid <abs-path-to-sandbox.toml>: admin: Extra inputs are not permitted` (or the equivalent Pydantic-supplied message) and exits with code 1; the Pydantic traceback is not displayed

### Requirement: Read-Only Domains Configuration
The `ProxyWhitelistConfig` Pydantic model SHALL include a `read_only_domains: list[str]` field with a default list of package registry domains. The `_SANDBOX_TOML_TEMPLATE` SHALL include `read_only_domains` in the `[proxy.whitelist]` section with sensible defaults.

#### Scenario: ProxyWhitelistConfig validates read_only_domains
- **WHEN** `sandbox.toml` includes `[proxy.whitelist]` with a `read_only_domains` list
- **THEN** the Pydantic model validates it as `list[str]` and accepts the values

#### Scenario: ProxyWhitelistConfig applies empty default
- **WHEN** `sandbox.toml` includes `[proxy.whitelist]` without a `read_only_domains` field
- **THEN** the Pydantic model applies the default of `[]` (empty list), matching the `domains: list[str] = []` pattern

#### Scenario: Scaffold template includes read_only_domains with populated defaults
- **WHEN** `sandbox init` scaffolds a new instance
- **THEN** the generated `sandbox.toml` contains a `read_only_domains` field under `[proxy.whitelist]` with package registry domains (`.pypi.org`, `.pythonhosted.org`, `.npmjs.com`, `.npmjs.org`, `.crates.io`, `.rust-lang.org`, `.golang.org`, `.go.dev`, `.debian.org`, `.ubuntu.com`)

### Requirement: Postgres Image Configuration
The `DbPostgresConfig` Pydantic model SHALL include an `image: str` field with a SHA256-pinned default. The postgres image SHALL be user-configurable via `sandbox.toml`.

#### Scenario: DbPostgresConfig validates image field
- **WHEN** `sandbox.toml` includes `[components.db_postgres]` with an `image` field
- **THEN** the Pydantic model validates it as a string

#### Scenario: DbPostgresConfig applies digest-pinned default
- **WHEN** `sandbox.toml` includes `[components.db_postgres]` without an `image` field
- **THEN** the Pydantic model applies the default from `IMAGE_DIGESTS["postgres"]`

### Requirement: Component-Conditional Validation
The system SHALL apply sub-table validation only for components that are enabled in `[components]`.

#### Scenario: Disabled component skips sub-table validation
- **WHEN** `components.db_postgres = false`
- **THEN** the absence of `[components.db_postgres]` does NOT cause a validation error

#### Scenario: Enabled component requires sub-table
- **WHEN** `components.db_postgres = true` and `[components.db_postgres]` sub-table is absent
- **THEN** the Pydantic model applies defaults (the sub-table is not required to be explicitly present; defaults are sufficient)

### Requirement: Instance Name Immutability Warning
The system SHALL emit a warning if `instance.name` in `sandbox.toml` differs from the `instance_name` component of the instance directory name.

#### Scenario: Renamed instance.name detected
- **WHEN** `sandbox.toml` is read and `instance.name` does not match the name portion of the instance directory
- **THEN** CLI emits: "WARNING: instance.name has changed since init. COMPOSE_PROJECT_NAME mismatch may orphan running containers."
