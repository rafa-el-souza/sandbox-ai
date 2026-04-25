## Purpose

This specification defines the centralized container image SHA256 digest pinning registry that ensures reproducible, immutable image references across all sandbox infrastructure and user-configurable images.

## Requirements

### Requirement: Centralized Image Digest Registry
The system SHALL declare a module-level `IMAGE_DIGESTS: dict[str, str]` in `hydration.py` containing SHA256 digest references for all container images. The dict SHALL include entries for `wolfi_base`, `debian_trixie`, `squid`, `coredns`, `dnsdist`, and `postgres`. A comment above the dict SHALL document the digest rotation procedure using `docker manifest inspect`.

#### Scenario: IMAGE_DIGESTS contains all 6 images
- **WHEN** `IMAGE_DIGESTS` in `hydration.py` is inspected
- **THEN** it contains keys `wolfi_base`, `debian_trixie`, `squid`, `coredns`, `dnsdist`, and `postgres`, each with a value in the format `<registry>/<image>@sha256:<64-hex-chars>`

#### Scenario: Rotation procedure documented
- **WHEN** the `IMAGE_DIGESTS` declaration is inspected
- **THEN** a comment above it documents the command `docker manifest inspect <image>:<tag> | jq ...` for resolving updated digests

### Requirement: Infrastructure Image Digest Usage
The system SHALL use `IMAGE_DIGESTS` values directly in `build_jinja_context()` for sandbox infrastructure images (proxy, dns, dnsdist) that are NOT user-configurable.

#### Scenario: Proxy image uses digest from registry
- **WHEN** `build_jinja_context()` is called
- **THEN** the `proxy_image` context value equals `IMAGE_DIGESTS["squid"]`

#### Scenario: DNS image uses digest from registry
- **WHEN** `build_jinja_context()` is called
- **THEN** the `dns_image` context value equals `IMAGE_DIGESTS["coredns"]`

#### Scenario: dnsdist image uses digest from registry
- **WHEN** `build_jinja_context()` is called
- **THEN** the `dnsdist_image` context value equals `IMAGE_DIGESTS["dnsdist"]`

### Requirement: User-Configurable Image Digest Defaults
The system SHALL use `IMAGE_DIGESTS` values as Pydantic field defaults for user-configurable images (`CoreConfig.base_image`, `AdminConfig.base_image`, `DbPostgresConfig.image`). Users MAY override these in `sandbox.toml`.

#### Scenario: Core base image default is digest-pinned
- **WHEN** `CoreConfig` is instantiated without an explicit `base_image`
- **THEN** `base_image` defaults to `IMAGE_DIGESTS["wolfi_base"]`

#### Scenario: Admin base image default is digest-pinned
- **WHEN** `AdminConfig` is instantiated without an explicit `base_image`
- **THEN** `base_image` defaults to `IMAGE_DIGESTS["debian_trixie"]`

#### Scenario: Postgres image default is digest-pinned
- **WHEN** `DbPostgresConfig` is instantiated without an explicit `image`
- **THEN** `image` defaults to `IMAGE_DIGESTS["postgres"]`

#### Scenario: User can override image with mutable tag
- **WHEN** `sandbox.toml` contains `base_image = "cgr.dev/chainguard/wolfi-base:latest"` in `[core]`
- **THEN** the Pydantic model accepts the mutable tag without validation error (backward compatible)

### Requirement: Postgres Template Image Templatization
The `db-postgres.yml` extras template SHALL use `{{ db_postgres_image }}` from the Jinja2 context instead of a hardcoded image reference.

#### Scenario: Postgres template uses context variable
- **WHEN** the `db-postgres.yml` template source is inspected
- **THEN** it contains `image: {{ db_postgres_image }}` (not `image: postgres:16-alpine`)

#### Scenario: Postgres image context key present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `db_postgres_image` (from `config.components_db_postgres.image`)

### Requirement: Scaffold Template Digest Integration
The `_SANDBOX_TOML_TEMPLATE` in `scaffold.py` SHALL import `IMAGE_DIGESTS` from `hydration` and use digest values as default image references in the generated `sandbox.toml`.

#### Scenario: Scaffold imports IMAGE_DIGESTS
- **WHEN** `scaffold.py` source is inspected
- **THEN** it imports `IMAGE_DIGESTS` from `core.hydration` (or equivalent module path)

#### Scenario: Generated sandbox.toml contains digest defaults
- **WHEN** `sandbox init` scaffolds a new instance
- **THEN** the generated `sandbox.toml` contains `base_image` values with `@sha256:` digest references (not mutable tags)
