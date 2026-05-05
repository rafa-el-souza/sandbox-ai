## Purpose

This specification defines the Pydantic + Jinja2 hydration pipeline that renders infrastructure templates from the packaged `templates` Python module into per-instance directories on every `sandbox start`. Template paths in this spec are package-relative (e.g., `templates/docker/compose.yml` resolves to `importlib.resources.files("templates").joinpath("docker/compose.yml")`); the discovery mechanism is owned by the `templates-packaging` capability.

## Requirements

### Requirement: Pydantic Validation Before Rendering
The system SHALL parse `sandbox.toml` into a typed Pydantic model and abort hydration with a structured error if validation fails, before any template is written.

#### Scenario: Invalid config aborts before any file write
- **WHEN** `sandbox.toml` fails Pydantic validation (e.g., an invalid field type)
- **THEN** no files in `sandboxes/<id>/docker/` or `sandboxes/<id>/config/` are created or overwritten

### Requirement: Template Rendering on Every Start
The system SHALL render all Jinja2 templates from the `templates` package into the instance directory on every `sandbox start` invocation. Extension templates SHALL use Jinja2 `{{ var }}` syntax for infrastructure values and Compose-time `${VAR}` syntax only for secrets loaded via `env_file:`.

#### Scenario: Compose and Dockerfile rendered
- **WHEN** `sandbox start` proceeds to the hydration phase
- **THEN** `templates/docker/compose.yml` is rendered into `sandboxes/<id>/docker/compose.yml` with all Jinja2 variables resolved from the Pydantic model context

#### Scenario: Config templates rendered
- **WHEN** `sandbox start` proceeds to the hydration phase
- **THEN** all entries in `_JINJA_RENDERED_CONFIG` are rendered into `sandboxes/<id>/config/` with all Jinja2 variables resolved from the Pydantic model context. This includes `coredns/Corefile`, `dnsdist/dnsdist.conf`, `proxy/squid.conf`, `core/.gitconfig`, `core/.npmrc`, `core/.bashrc`, `core/CLAUDE.md`, `core/sshd_config`, `admin/.zshrc`, `admin/.tmux.conf`, and `admin/.gitconfig`.

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
- **THEN** `templates/docker/extras/db-postgres.yml` is rendered into `sandboxes/<id>/docker/extras/db-postgres.yml`

### Requirement: Extras Jinja2 Context Completeness
The system SHALL include all values required by extras templates and config templates in the Jinja2 context returned by `build_jinja_context()`. The context SHALL include both Squid-format domain lists (leading-dot) and CoreDNS-format domain lists (no leading dot) as distinct keys. The context SHALL include `proxy_whitelist_read_only_domains`, `db_postgres_image`, `proxy_image`, `dns_image`, `dnsdist_image`, `agent_proxy_ip`, and `admin_proxy_ip`.

#### Scenario: Database context keys present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `pg_user`, `pg_db`, `db_postgres_ip`, `db_postgres_admin_ip`, `db_postgres_image`, `core_pids_limit`, `runtime`, and `instance_dir`

#### Scenario: Firecrawl context keys present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `mcp_firecrawl_proxy_ip`, `firecrawl_dns_ip`, `firecrawl_isolated_ip`, `proxy_password`, `coredns_dns_ip`, `proxy_core_ip`, `db_postgres_ip`, `isolated_subnet`, `core_proxy_subnet`, `dns_subnet`, `core_pids_limit`, `runtime`, and `instance_dir`

#### Scenario: Dry-run validation catches missing context keys
- **WHEN** `validate_templates()` renders a template (including extras and config templates) and a required Jinja2 variable is missing from the context
- **THEN** `jinja2.StrictUndefined` raises `UndefinedError` and the validation reports the missing variable name and template file

#### Scenario: Proxy URL context key present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `proxy_url_core` with value `http://proxyuser:<proxy_password>@proxy:3128`

#### Scenario: Git identity context keys present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `git_user` (from `config.core.git_user`, falling back to `"Agent"` when empty) and `git_email` (from `config.core.git_email`, falling back to `"agent@sandbox.local"` when empty)

#### Scenario: Custom config path context keys present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `custom_config_core` (value: `/home/agent/.sandbox/custom`), `custom_config_admin` (value: `/home/human/.sandbox/custom`), and `tmux_resurrect_dir` (value: `/home/human/.sandbox/tmux_resurrect`)

#### Scenario: Component enablement context keys present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `db_postgres_enabled` (from `config.components_db_postgres.enabled`) and `mcp_firecrawl_enabled` (from `config.components.mcp_firecrawl`)

#### Scenario: Custom CLAUDE.md rules context key present
- **WHEN** `build_jinja_context()` is called and `custom/config/core/CLAUDE.md` exists in the instance directory
- **THEN** the returned context includes `custom_claude_rules` containing the file's contents

#### Scenario: Custom CLAUDE.md rules absent
- **WHEN** `build_jinja_context()` is called and `custom/config/core/CLAUDE.md` does not exist in the instance directory
- **THEN** the returned context includes `custom_claude_rules` with value `""`

#### Scenario: CoreDNS domain context key present
- **WHEN** `build_jinja_context()` is called and `config.proxy_whitelist.domains` contains `[".github.com", ".npmjs.com"]`
- **THEN** the returned context includes `proxy_whitelist_domains_coredns` with value `["github.com", "npmjs.com"]` (leading dots stripped)

#### Scenario: CoreDNS domain key coexists with Squid domain key
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes both `proxy_whitelist_domains` (with leading dots, for Squid `dstdomain` and `allowed_domains.txt`) and `proxy_whitelist_domains_coredns` (without leading dots, for CoreDNS zone declarations)

#### Scenario: Per-container proxy IP context keys present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `agent_proxy_ip` and `admin_proxy_ip` (from `derive_static_ips()`)

#### Scenario: Read-only domains context key present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `proxy_whitelist_read_only_domains` (from `config.proxy_whitelist.read_only_domains`)

#### Scenario: Infrastructure image context keys present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `proxy_image` (from `IMAGE_REGISTRY["squid"].pinned`), `dns_image` (from `IMAGE_REGISTRY["coredns"].pinned`), and `dnsdist_image` (from `IMAGE_REGISTRY["dnsdist"].pinned`)

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

### Requirement: Core Resource Limit Context Keys
The system SHALL include `core_mem_limit`, `core_memswap_limit`, and `core_cpus` in the Jinja2 context returned by `build_jinja_context()`. `core_memswap_limit` SHALL always equal `core_mem_limit` (zero swap — not independently configurable).

#### Scenario: Core resource context keys present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `core_mem_limit` (from `config.core.mem_limit`), `core_memswap_limit` (equal to `core_mem_limit`), and `core_cpus` (from `str(config.core.cpus)`)

#### Scenario: Core memswap_limit derives from mem_limit
- **WHEN** `build_jinja_context()` is called with `config.core.mem_limit = "16gb"`
- **THEN** the returned context has `core_mem_limit = "16gb"` and `core_memswap_limit = "16gb"`

### Requirement: Admin Resource Limit Context Keys
The system SHALL include `admin_mem_limit`, `admin_memswap_limit`, and `admin_cpus` in the Jinja2 context returned by `build_jinja_context()`. `admin_memswap_limit` SHALL always equal `admin_mem_limit`.

#### Scenario: Admin resource context keys present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `admin_mem_limit` (from `config.admin.mem_limit`), `admin_memswap_limit` (equal to `admin_mem_limit`), and `admin_cpus` (from `str(config.admin.cpus)`)

### Requirement: Compose Template Resource Limits
The rendered `compose.yml` SHALL include `mem_limit`, `memswap_limit`, and `cpus` on the core and admin services, resolved from the Jinja2 context.

#### Scenario: Core service rendered with resource limits
- **WHEN** `compose.yml` is rendered with default config
- **THEN** the core service block contains `mem_limit: "8gb"`, `memswap_limit: "8gb"`, and `cpus: "4.0"`

#### Scenario: Admin service rendered with resource limits
- **WHEN** `compose.yml` is rendered with default config
- **THEN** the admin service block contains `mem_limit: "8gb"`, `memswap_limit: "8gb"`, and `cpus: "4.0"`

### Requirement: Compose Template Static Hardening Properties
The rendered compose templates (main `compose.yml` and feature-gated extras) SHALL include static hardening properties that are not configurable via `sandbox.toml`. All services with `cap_drop: ALL` SHALL re-grant only the minimum Linux capabilities required for their entrypoint to function. All tmpfs mounts SHALL include `noexec,nosuid,nodev` flags.

#### Scenario: Core IPC isolation
- **WHEN** `compose.yml` is rendered
- **THEN** the core service block contains `ipc: private`

#### Scenario: Core and admin ulimits
- **WHEN** `compose.yml` is rendered
- **THEN** both core and admin service blocks contain `ulimits` with `core: { soft: 0, hard: 0 }` (disabling core dumps) and `nofile: { soft: 65536, hard: 65536 }` (bounding file descriptors)

#### Scenario: coredns IP forwarding disabled
- **WHEN** `compose.yml` is rendered
- **THEN** the coredns service's `sysctls` block contains `net.ipv4.ip_forward=0`

#### Scenario: coredns capability grant
- **WHEN** `compose.yml` is rendered
- **THEN** the coredns service block contains `cap_drop: [ALL]` and `cap_add: [NET_BIND_SERVICE]`

#### Scenario: Proxy capability grants
- **WHEN** `compose.yml` is rendered
- **THEN** the proxy service block contains `cap_drop: [ALL]` and `cap_add: [SETUID, SETGID]`

#### Scenario: Proxy tmpfs mounts hardened
- **WHEN** `compose.yml` is rendered
- **THEN** the proxy service's `tmpfs` block contains `/var/spool/squid:noexec,nosuid,nodev`, `/var/run/squid:noexec,nosuid,nodev`, `/var/log/squid:noexec,nosuid,nodev`, and `/run:size=1m,noexec,nosuid,nodev`

#### Scenario: Postgres extras capability grants
- **WHEN** `db-postgres.yml` extras template is rendered
- **THEN** the db-postgres service block contains `cap_drop: [ALL]` and `cap_add: [CHOWN, FOWNER, SETGID, SETUID]`

### Requirement: Config Template Path Templatization
The system SHALL use Jinja2 context variables — not hardcoded paths — for all custom config override locations and tmux resurrect state directories within rendered config files. No rendered config file SHALL contain the literal path `/workspace/.sandbox/custom/` or `/workspace/.tmux_resurrect`.

#### Scenario: Core config files use templatized custom path
- **WHEN** `templates/config/core/.gitconfig` and `templates/config/core/.bashrc` are rendered
- **THEN** custom config references resolve to the value of `{{ custom_config_core }}` (not `/workspace/.sandbox/custom/`)

#### Scenario: Admin config files use templatized custom path
- **WHEN** `templates/config/admin/.zshrc` and `templates/config/admin/.tmux.conf` are rendered
- **THEN** custom config references resolve to the value of `{{ custom_config_admin }}` (not `/workspace/.sandbox/custom/`)

#### Scenario: Tmux resurrect dir uses templatized path
- **WHEN** `templates/config/admin/.tmux.conf` is rendered
- **THEN** the resurrect-dir setting resolves to the value of `{{ tmux_resurrect_dir }}` (not `/workspace/.tmux_resurrect`)

#### Scenario: No hardcoded workspace sandbox paths in rendered output
- **WHEN** all config templates are rendered
- **THEN** zero files in the rendered instance contain `/workspace/.sandbox/` or `/workspace/.tmux_resurrect`

### Requirement: Gitconfig Default Filter Removal
The `templates/config/core/.gitconfig` template SHALL use bare `{{ git_user }}` and `{{ git_email }}` without Jinja2 `| default()` filters. Default resolution is the responsibility of `build_jinja_context()`.

#### Scenario: Gitconfig uses bare context variables
- **WHEN** `templates/config/core/.gitconfig` template source is inspected
- **THEN** it contains `{{ git_user }}` and `{{ git_email }}` without `| default(...)` filters

### Requirement: Zshrc Load Order Contract
The `templates/config/admin/.zshrc` template SHALL enforce a tail load order of: (1) `starship init`, (2) user override hook, (3) warmup prompt. A comment block SHALL document this contract.

#### Scenario: Starship init before user override
- **WHEN** the rendered `.zshrc` is inspected
- **THEN** the `eval "$(starship init zsh)"` line appears before the user override `source` block

#### Scenario: User override before warmup prompt
- **WHEN** the rendered `.zshrc` is inspected
- **THEN** the user override `source` block appears before the `SANDBOX_WARMUP_PROMPT` check

#### Scenario: Load order contract comment present
- **WHEN** the `templates/config/admin/.zshrc` template source is inspected
- **THEN** it contains a comment block documenting the load order: shell setup → starship init → user override → warmup prompt

### Requirement: Read-Only Domains Context Key
The system SHALL include `proxy_whitelist_read_only_domains` in the Jinja2 context returned by `build_jinja_context()`, sourced from `config.proxy_whitelist.read_only_domains`.

#### Scenario: Read-only domains context key present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `proxy_whitelist_read_only_domains` (from `config.proxy_whitelist.read_only_domains`)

### Requirement: Read-Only Domains File Generation
The system SHALL generate `config/proxy/read_only_domains.txt` during `render_templates()`, containing one domain per line from the `proxy_whitelist_read_only_domains` context key. This follows the same generation pattern as `allowed_domains.txt`.

#### Scenario: read_only_domains.txt generated
- **WHEN** `render_templates()` completes
- **THEN** `sandboxes/<id>/config/proxy/read_only_domains.txt` exists and contains one domain per line from the configured `read_only_domains` list

#### Scenario: Empty read_only_domains produces empty file
- **WHEN** `render_templates()` runs with `proxy_whitelist.read_only_domains = []`
- **THEN** `config/proxy/read_only_domains.txt` is created but empty

### Requirement: Read-Only Domains Validation Warning
The system SHALL emit a validation warning (not error) if any domain in `read_only_domains` is not also present in `domains`. This indicates a configuration mistake (the domain is unreachable regardless of method), but the failure mode is over-restriction, not under-restriction.

#### Scenario: Orphaned read-only domain emits warning
- **WHEN** `read_only_domains` contains `.example.com` but `domains` does not contain `.example.com`
- **THEN** the system emits a warning indicating the domain is unreachable (not in the allowlist)

#### Scenario: Valid read-only domain subset produces no warning
- **WHEN** every domain in `read_only_domains` is also present in `domains`
- **THEN** no validation warning is emitted for the read-only domains configuration

### Requirement: Image Digest Context Keys
The system SHALL include `db_postgres_image` in the Jinja2 context returned by `build_jinja_context()`, sourced from `config.components_db_postgres.image`.

#### Scenario: Postgres image context key present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `db_postgres_image` (from `config.components_db_postgres.image`)

### Requirement: Infrastructure Image Context Keys
The system SHALL include `proxy_image`, `dns_image`, `dnsdist_image`, and `busybox_image` in the Jinja2 context returned by `build_jinja_context()`, sourced from `IMAGE_REGISTRY`.

#### Scenario: Infrastructure image context keys present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `proxy_image` (from `IMAGE_REGISTRY["squid"].pinned`), `dns_image` (from `IMAGE_REGISTRY["coredns"].pinned`), `dnsdist_image` (from `IMAGE_REGISTRY["dnsdist"].pinned`), and `busybox_image` (from `IMAGE_REGISTRY["busybox_musl"].pinned`)

### Requirement: Seven-Subnet Context Keys
The system SHALL include all seven subnet CIDR strings and all multi-network container IP addresses in the Jinja2 context returned by `build_jinja_context()`.

#### Scenario: All seven subnet context keys present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `isolated_subnet`, `core_proxy_subnet`, `dns_subnet`, `admin_subnet`, `admin_proxy_subnet`, `egress_subnet`, and `ipc_subnet`

#### Scenario: IPC IP context keys present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `core_ipc_ip` and `admin_ipc_ip`

#### Scenario: Firecrawl isolated IP context key present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `firecrawl_isolated_ip`

#### Scenario: Proxy dual-network IP context keys present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `proxy_core_ip` and `proxy_admin_ip`

#### Scenario: dnsdist multi-network IP context keys present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `dnsdist_isolated_ip`, `dnsdist_dns_ip`, and `dnsdist_admin_ip`

#### Scenario: coredns multi-network IP context keys present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `coredns_dns_ip`, `coredns_admin_ip`, and `coredns_egress_ip`

#### Scenario: db-postgres dual-network IP context keys present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `db_postgres_ip` and `db_postgres_admin_ip`

### Requirement: Programmatic .claude.json Generation
The system SHALL generate `.claude.json` programmatically in `render_templates()` using `json.dump`, with conditional `mcpServers` registration. This follows the existing pattern for `allowed_domains.txt` and `read_only_domains.txt`.

#### Scenario: .claude.json generated during render_templates
- **WHEN** `render_templates()` completes
- **THEN** `sandboxes/<id>/config/core/.claude.json` exists and contains valid JSON

#### Scenario: Firecrawl MCP registered when enabled
- **WHEN** `render_templates()` runs with `mcp_firecrawl_enabled = True`
- **THEN** `.claude.json` contains `mcpServers.firecrawl` with `type: "http"` and `url: "http://<firecrawl_isolated_ip>:3000/mcp"`

#### Scenario: Empty config when no MCP enabled
- **WHEN** `render_templates()` runs with `mcp_firecrawl_enabled = False`
- **THEN** `.claude.json` contains `{}`

### Requirement: Busybox Image Context Key
The system SHALL include `busybox_image` in the Jinja2 context returned by `build_jinja_context()`, sourced from `IMAGE_REGISTRY["busybox_musl"].pinned`. This key is consumed by the coredns service's `build.args` in `compose.yml`.

#### Scenario: busybox_image context key present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `busybox_image` with a value in the format `busybox@sha256:<64-hex-chars>`

#### Scenario: busybox_image absent from context causes StrictUndefined error
- **WHEN** `busybox_image` is removed from `build_jinja_context()` and `compose.yml` references `{{ busybox_image }}`
- **THEN** `jinja2.UndefinedError` is raised during template rendering

### Requirement: CoreDNS Dockerfile Static Copy
The system SHALL copy `templates/docker/coredns/Dockerfile.coredns` as a static file (not Jinja2-rendered) during `render_templates()`, using the existing `_copy_file()` mechanism (or its `importlib.resources`-based equivalent post-`src-layout-and-templates-packaging`). The `docker/coredns` subdirectory SHALL be included in `INSTANCE_SUBDIRS`.

#### Scenario: CoreDNS Dockerfile copied to instance
- **WHEN** `render_templates()` completes
- **THEN** `sandboxes/<id>/docker/coredns/Dockerfile.coredns` exists and is identical to `templates/docker/coredns/Dockerfile.coredns` (the source from the templates package)

#### Scenario: docker/coredns directory created by scaffold
- **WHEN** `create_instance_dirs()` is called
- **THEN** a `docker/coredns` subdirectory exists in the instance directory

#### Scenario: CoreDNS Dockerfile validated as static file
- **WHEN** `validate_templates()` runs
- **THEN** `templates/docker/coredns/Dockerfile.coredns` is included in the static file existence check and counts toward the validated total
