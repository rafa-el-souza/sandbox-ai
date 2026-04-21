## Purpose

This specification defines the Pydantic + Jinja2 hydration pipeline that renders infrastructure templates from the tooling plane into per-instance directories on every `sandbox start`.

## Requirements

### Requirement: Pydantic Validation Before Rendering
The system SHALL parse `sandbox.toml` into a typed Pydantic model and abort hydration with a structured error if validation fails, before any template is written.

#### Scenario: Invalid config aborts before any file write
- **WHEN** `sandbox.toml` fails Pydantic validation (e.g., an invalid field type)
- **THEN** no files in `sandboxes/<id>/docker/` or `sandboxes/<id>/config/` are created or overwritten

### Requirement: Template Rendering on Every Start
The system SHALL render all Jinja2 templates from the tooling plane into the instance directory on every `sandbox start` invocation. Extension templates SHALL use Jinja2 `{{ var }}` syntax for infrastructure values and Compose-time `${VAR}` syntax only for secrets loaded via `env_file:`.

#### Scenario: Compose and Dockerfile rendered
- **WHEN** `sandbox start` proceeds to the hydration phase
- **THEN** `.docker/compose.yml` is rendered into `sandboxes/<id>/docker/compose.yml` with all Jinja2 variables resolved from the Pydantic model context

#### Scenario: Config templates rendered
- **WHEN** `sandbox start` proceeds to the hydration phase
- **THEN** `.config/dns-sidecar/Corefile` is rendered into `sandboxes/<id>/config/dns-sidecar/Corefile` with the `proxy.whitelist.domains` list resolved

#### Scenario: Extras templates resolve Jinja2 variables
- **WHEN** an enabled extras template (e.g., `db-postgres.yml`) is rendered
- **THEN** all `{{ var }}` tokens are resolved from the Jinja2 context and zero `${VAR:-default}` patterns remain for infrastructure values (IPs, subnets, paths, credentials)

#### Scenario: Extras templates use env_file for secrets
- **WHEN** an enabled extras template contains secret references
- **THEN** the rendered file includes `env_file: "<instance_dir>/.sandbox.env"` and secrets appear as Compose-time `${VAR}` (e.g., `${PG_PASSWORD}`, `${FIRECRAWL_API_KEY}`)

### Requirement: Component-Conditional Template Inclusion
The system SHALL render extension override files only for components that are enabled in `sandbox.toml`.

#### Scenario: Disabled component skips template rendering
- **WHEN** `components.db_postgres = false`
- **THEN** `sandboxes/<id>/docker/extras/db-postgres.yml` is NOT created or overwritten

#### Scenario: Enabled component renders extension template
- **WHEN** `components.db_postgres = true`
- **THEN** `.docker/extras/db-postgres.yml` is rendered into `sandboxes/<id>/docker/extras/db-postgres.yml`

### Requirement: Extras Jinja2 Context Completeness
The system SHALL include all values required by extras templates in the Jinja2 context returned by `build_jinja_context()`.

#### Scenario: Database context keys present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `pg_user`, `pg_db`, `db_postgres_ip`, `core_pids_limit`, `runtime`, and `instance_dir`

#### Scenario: Firecrawl context keys present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `mcp_firecrawl_isolated_ip`, `mcp_firecrawl_proxy_ip`, `proxy_password`, `dns_sidecar_ip`, `proxy_ip`, `db_postgres_ip`, `isolated_subnet`, `proxy_subnet`, `core_pids_limit`, `runtime`, and `instance_dir`

#### Scenario: Dry-run validation catches missing context keys
- **WHEN** `validate_templates()` renders an extras template and a required Jinja2 variable is missing from the context
- **THEN** `jinja2.StrictUndefined` raises `UndefinedError` and the validation reports the missing variable name and template file

### Requirement: Precious State Preservation
The system SHALL never overwrite the user's persistent state files during hydration.

#### Scenario: sandbox.toml is not overwritten
- **WHEN** the hydration pipeline runs
- **THEN** `sandboxes/<id>/sandbox.toml` is read as input but never written or truncated

#### Scenario: User custom configs are preserved
- **WHEN** the hydration pipeline runs
- **THEN** files under `sandboxes/<id>/custom/config/` are NOT modified

#### Scenario: Cache and logs are preserved
- **WHEN** the hydration pipeline runs
- **THEN** `sandboxes/<id>/cache/` and `sandboxes/<id>/log/` contents are NOT modified
