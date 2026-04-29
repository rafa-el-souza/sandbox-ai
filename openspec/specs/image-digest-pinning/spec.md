## Purpose

This specification defines the centralized container image SHA256 digest pinning registry that ensures reproducible, immutable image references across all sandbox infrastructure and user-configurable images.

## Requirements

### Requirement: Centralized Image Digest Registry
The system SHALL declare a module-level `IMAGE_REGISTRY: dict[str, ImagePin]` in `hydration.py` containing structured image pin entries for all container images. `ImagePin` SHALL be a frozen dataclass with `ref: str`, `tag: str`, and `digest: str` fields, plus derived properties `pinned` (returns `f"{self.ref}@{self.digest}"`) and `tagged` (returns `f"{self.ref}:{self.tag}"`). The dict SHALL include entries for `wolfi_base`, `debian_trixie`, `squid`, `coredns`, `dnsdist`, `postgres`, and `busybox_musl`. All digest values SHALL be cryptographically verified live values resolvable against their respective registries.

#### Scenario: IMAGE_REGISTRY contains all 7 images
- **WHEN** `IMAGE_REGISTRY` in `hydration.py` is inspected
- **THEN** it contains keys `wolfi_base`, `debian_trixie`, `squid`, `coredns`, `dnsdist`, `postgres`, and `busybox_musl`, each mapping to an `ImagePin` instance

#### Scenario: ImagePin fields are correctly typed
- **WHEN** any `ImagePin` instance is inspected
- **THEN** it has `ref` (str, e.g. `coredns/coredns`), `tag` (str, e.g. `1.11.1`), and `digest` (str, e.g. `sha256:...64-hex-chars...`) as stored fields

#### Scenario: ImagePin.pinned returns digest-qualified reference
- **WHEN** `IMAGE_REGISTRY["coredns"].pinned` is accessed
- **THEN** it returns a string in the format `<ref>@sha256:<64-hex-chars>`

#### Scenario: ImagePin.tagged returns tag-qualified reference
- **WHEN** `IMAGE_REGISTRY["coredns"].tagged` is accessed
- **THEN** it returns a string in the format `<ref>:<tag>`

#### Scenario: ImagePin is immutable
- **WHEN** code attempts to assign to an `ImagePin` field (e.g., `pin.digest = "..."`)
- **THEN** a `FrozenInstanceError` is raised

#### Scenario: Rotation procedure documented
- **WHEN** the `IMAGE_REGISTRY` declaration is inspected
- **THEN** a comment above it documents the rotation procedure referencing `scripts/rotate_digests.py`

#### Scenario: busybox_musl entry pinned at immutable release
- **WHEN** `IMAGE_REGISTRY["busybox_musl"]` is inspected
- **THEN** its `tag` is `1.36.1-musl` and its `ref` is `busybox`

#### Scenario: Legacy IMAGE_DIGESTS name is removed
- **WHEN** `hydration.py` source is inspected
- **THEN** there is no `IMAGE_DIGESTS` variable declaration

### Requirement: Infrastructure Image Digest Usage
The system SHALL use `IMAGE_REGISTRY[key].pinned` values in `build_jinja_context()` for sandbox infrastructure images (proxy, dns, dnsdist) that are NOT user-configurable.

#### Scenario: Proxy image uses pinned digest from registry
- **WHEN** `build_jinja_context()` is called
- **THEN** the `proxy_image` context value equals `IMAGE_REGISTRY["squid"].pinned`

#### Scenario: DNS image uses pinned digest from registry
- **WHEN** `build_jinja_context()` is called
- **THEN** the `dns_image` context value equals `IMAGE_REGISTRY["coredns"].pinned`

#### Scenario: dnsdist image uses pinned digest from registry
- **WHEN** `build_jinja_context()` is called
- **THEN** the `dnsdist_image` context value equals `IMAGE_REGISTRY["dnsdist"].pinned`

#### Scenario: Legacy IMAGE_DIGESTS accessor absent from infrastructure context
- **WHEN** `build_jinja_context()` source is inspected
- **THEN** it does NOT contain `IMAGE_DIGESTS["coredns"]`, `IMAGE_DIGESTS["squid"]`, or `IMAGE_DIGESTS["dnsdist"]`

### Requirement: User-Configurable Image Digest Defaults
The system SHALL use `IMAGE_REGISTRY[key].pinned` values as Pydantic field defaults for user-configurable images (`CoreConfig.base_image`, `AdminConfig.base_image`, `DbPostgresConfig.image`). Users MAY override these in `sandbox.toml`.

#### Scenario: Core base image default is digest-pinned
- **WHEN** `CoreConfig` is instantiated without an explicit `base_image`
- **THEN** `base_image` defaults to `IMAGE_REGISTRY["wolfi_base"].pinned`

#### Scenario: Admin base image default is digest-pinned
- **WHEN** `AdminConfig` is instantiated without an explicit `base_image`
- **THEN** `base_image` defaults to `IMAGE_REGISTRY["debian_trixie"].pinned`

#### Scenario: Postgres image default is digest-pinned
- **WHEN** `DbPostgresConfig` is instantiated without an explicit `image`
- **THEN** `image` defaults to `IMAGE_REGISTRY["postgres"].pinned`

#### Scenario: User can override image with mutable tag
- **WHEN** `sandbox.toml` contains `base_image = "cgr.dev/chainguard/wolfi-base:latest"` in `[core]`
- **THEN** the Pydantic model accepts the mutable tag without validation error (backward compatible)

#### Scenario: Legacy IMAGE_DIGESTS accessor absent from Pydantic defaults
- **WHEN** the `CoreConfig`, `AdminConfig`, and `DbPostgresConfig` source is inspected
- **THEN** default values use `IMAGE_REGISTRY["..."].pinned`, not `IMAGE_DIGESTS["..."]`

### Requirement: Postgres Template Image Templatization
The `db-postgres.yml` extras template SHALL use `{{ db_postgres_image }}` from the Jinja2 context instead of a hardcoded image reference.

#### Scenario: Postgres template uses context variable
- **WHEN** the `db-postgres.yml` template source is inspected
- **THEN** it contains `image: {{ db_postgres_image }}` (not `image: postgres:16-alpine`)

#### Scenario: Postgres image context key present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `db_postgres_image` (from `config.components_db_postgres.image`)

### Requirement: Scaffold Template Digest Integration
The `_SANDBOX_TOML_TEMPLATE` in `scaffold.py` SHALL import `IMAGE_REGISTRY` from `core.hydration` and use `.pinned` values as default image references in the generated `sandbox.toml`.

#### Scenario: Scaffold imports IMAGE_REGISTRY
- **WHEN** `scaffold.py` source is inspected
- **THEN** it imports `IMAGE_REGISTRY` from `core.hydration` (not the legacy `IMAGE_DIGESTS`)

#### Scenario: Generated sandbox.toml contains digest defaults
- **WHEN** `sandbox init` scaffolds a new instance
- **THEN** the generated `sandbox.toml` contains `base_image` values with `@sha256:` digest references (not mutable tags)
