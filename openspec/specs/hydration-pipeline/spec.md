## Purpose

This specification defines the Pydantic + Jinja2 hydration pipeline that renders infrastructure templates from the packaged `templates` Python module into per-instance directories on every `sandbox start`. Template paths in this spec are package-relative (e.g., `templates/docker/compose.yml` resolves to `importlib.resources.files("templates").joinpath("docker/compose.yml")`); the discovery mechanism is owned by the `templates-packaging` capability.
## Requirements
### Requirement: Workspaces Context Key

The Jinja2 context returned by `build_jinja_context()` SHALL include a `workspaces` key whose value is a list of dicts, one per workspace in `[workspaces]`, sorted lexicographically by workspace name. Each dict SHALL have at minimum:
- `name`: the workspace name (string).
- `path`: the absolute path of the workspace tree (string, from `WorkspaceConfig.path`).
- `bootstrap_mode`: the bootstrap mode value (string, `"copy"` or `"empty"`).
- `source`: the source path (string or null; from `WorkspaceConfig.source`).

The list SHALL be non-empty (Pydantic enforces at least one workspace per instance, per the `sandbox-toml-schema` capability).

#### Scenario: workspaces context key present and sorted
- **WHEN** `build_jinja_context()` is called for an instance with workspaces `scratch` and `main` (in TOML insertion order)
- **THEN** the returned context's `workspaces` key contains `[{name: "main", ...}, {name: "scratch", ...}]` (sorted lexicographically by name)

#### Scenario: Each workspace dict has required fields
- **WHEN** `build_jinja_context()` returns the workspaces list
- **THEN** every entry has `name`, `path`, `bootstrap_mode`, and `source` keys

#### Scenario: Empty workspaces list is impossible
- **WHEN** `build_jinja_context()` is called
- **THEN** the workspaces list is never empty (at least one workspace exists; Pydantic guards this at sandbox.toml parse time)

#### Scenario: Compose loop consumes workspaces context
- **WHEN** `compose.yml` is rendered referencing `{% for ws in workspaces %}` ... `{% endfor %}`
- **THEN** the loop body executes once per workspace, with `ws.name`, `ws.path`, etc. accessible

### Requirement: Pydantic Validation Before Rendering

The system SHALL parse `sandbox.toml` into a typed Pydantic model and abort hydration with a structured error if validation fails, before any template is written. Validation SHALL include the non-empty `[workspaces]` map check (per the `sandbox-toml-schema` capability).

#### Scenario: Invalid config aborts before any file write
- **WHEN** `sandbox.toml` fails Pydantic validation (e.g., an invalid field type, an empty `[workspaces]` map, or a workspace missing the `path` field)
- **THEN** no files in `<sandbox_ai_home()>/instances/<inst>/docker/` or `<sandbox_ai_home()>/instances/<inst>/config/` are created or overwritten

### Requirement: Template Rendering on Every Start

The system SHALL render all Jinja2 templates from the `templates` package into the instance directory on every `sandbox start` invocation. The instance directory is at `<sandbox_ai_home()>/instances/<inst>/`. Extension templates SHALL use Jinja2 `{{ var }}` syntax for infrastructure values and Compose-time `${VAR}` syntax only for secrets loaded via `env_file:`.

#### Scenario: Compose and Dockerfile rendered
- **WHEN** `sandbox start <inst>` proceeds to the hydration phase
- **THEN** `templates/docker/compose.yml` is rendered into `<sandbox_ai_home()>/instances/<inst>/docker/compose.yml` with all Jinja2 variables resolved from the Pydantic model context (including the new `workspaces` context key)

#### Scenario: Config templates rendered
- **WHEN** `sandbox start <inst>` proceeds to the hydration phase
- **THEN** all entries in `_JINJA_RENDERED_CONFIG` are rendered into `<sandbox_ai_home()>/instances/<inst>/config/` with all Jinja2 variables resolved from the Pydantic model context. This includes `coredns/Corefile`, `dnsdist/dnsdist.conf`, `proxy/squid.conf`, `core/.gitconfig`, `core/.npmrc`, `core/.bashrc`, `core/CLAUDE.md`, and `core/sshd_config`.

#### Scenario: Extras templates resolve Jinja2 variables
- **WHEN** an enabled extras template (e.g., `db-postgres.yml`) is rendered
- **THEN** all `{{ var }}` tokens are resolved from the Jinja2 context and zero `${VAR:-default}` patterns remain for infrastructure values (IPs, subnets, paths, credentials)

#### Scenario: Extras templates use env_file for secrets
- **WHEN** an enabled extras template contains secret references
- **THEN** the rendered file includes `env_file: "<instance_dir>/.sandbox.env"` and secrets appear as Compose-time `${VAR}` (e.g., `${PG_PASSWORD}`, `${FIRECRAWL_API_KEY}`)

### Requirement: Component-Conditional Template Inclusion

The system SHALL render extension override files only for components that are enabled in `sandbox.toml`. Rendered files land in `<sandbox_ai_home()>/instances/<inst>/docker/extras/`.

#### Scenario: Disabled component skips template rendering
- **WHEN** `components.db_postgres = false`
- **THEN** `<sandbox_ai_home()>/instances/<inst>/docker/extras/db-postgres.yml` is NOT created or overwritten

#### Scenario: Enabled component renders extension template
- **WHEN** `components.db_postgres = true`
- **THEN** `templates/docker/extras/db-postgres.yml` is rendered into `<sandbox_ai_home()>/instances/<inst>/docker/extras/db-postgres.yml`

### Requirement: Extras Jinja2 Context Completeness

The system SHALL include all values required by extras templates and config templates in the Jinja2 context returned by `build_jinja_context()`. The context SHALL include both Squid-format domain lists (leading-dot) and CoreDNS-format domain lists (no leading dot) as distinct keys. The context SHALL include `proxy_whitelist_read_only_domains`, `db_postgres_image`, `proxy_image`, `dns_image`, `dnsdist_image`, `agent_proxy_ip`, AND the `workspaces` key (per the "Workspaces Context Key" requirement above).

#### Scenario: Workspaces context key present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `workspaces` (list of dicts, sorted by name, non-empty)

#### Scenario: Database context keys present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `pg_user`, `pg_db`, `db_postgres_ip`, `db_postgres_image`, `core_pids_limit`, `runtime`, and `instance_dir`

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
- **THEN** the returned context includes `custom_config_core` (value: `/home/agent/.sandbox/custom`)

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
- **THEN** the returned context includes `agent_proxy_ip` (from `derive_static_ips()`)

#### Scenario: Read-only domains context key present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `proxy_whitelist_read_only_domains` (from `config.proxy_whitelist.read_only_domains`)

#### Scenario: Infrastructure image context keys present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `proxy_image` (from `IMAGE_REGISTRY["squid"].pinned`), `dns_image` (from `IMAGE_REGISTRY["coredns"].pinned`), and `dnsdist_image` (from `IMAGE_REGISTRY["dnsdist"].pinned`)

### Requirement: Precious State Preservation

The system SHALL never overwrite the user's persistent state files during hydration. The instance dir is at `<sandbox_ai_home()>/instances/<inst>/`.

#### Scenario: sandbox.toml is not overwritten
- **WHEN** the hydration pipeline runs
- **THEN** `<sandbox_ai_home()>/instances/<inst>/sandbox.toml` is read as input but never written or truncated

#### Scenario: User custom configs are preserved
- **WHEN** the hydration pipeline runs
- **THEN** files under `<sandbox_ai_home()>/instances/<inst>/custom/config/` are NOT modified

#### Scenario: Cache and logs are preserved
- **WHEN** the hydration pipeline runs
- **THEN** `<sandbox_ai_home()>/instances/<inst>/cache/` and `<sandbox_ai_home()>/instances/<inst>/log/` contents are NOT modified

#### Scenario: Workspace trees are not modified by hydration
- **WHEN** the hydration pipeline runs
- **THEN** files under `<sandbox_ai_home()>/workspaces/<inst>/<ws>/` (any workspace tree) are NOT touched by hydration; workspaces are operator/agent state, separate from the rendered instance plane

### Requirement: Core Resource Limit Context Keys
The system SHALL include `core_mem_limit`, `core_memswap_limit`, and `core_cpus` in the Jinja2 context returned by `build_jinja_context()`. `core_memswap_limit` SHALL always equal `core_mem_limit` (zero swap — not independently configurable).

#### Scenario: Core resource context keys present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `core_mem_limit` (from `config.core.mem_limit`), `core_memswap_limit` (equal to `core_mem_limit`), and `core_cpus` (from `str(config.core.cpus)`)

#### Scenario: Core memswap_limit derives from mem_limit
- **WHEN** `build_jinja_context()` is called with `config.core.mem_limit = "16gb"`
- **THEN** the returned context has `core_mem_limit = "16gb"` and `core_memswap_limit = "16gb"`

### Requirement: Compose Template Resource Limits
The rendered `compose.yml` SHALL include `mem_limit`, `memswap_limit`, and `cpus` on the core service, resolved from the Jinja2 context.

#### Scenario: Core service rendered with resource limits
- **WHEN** `compose.yml` is rendered with default config
- **THEN** the core service block contains `mem_limit: "8gb"`, `memswap_limit: "8gb"`, and `cpus: "4.0"`

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

The system SHALL use Jinja2 context variables — not hardcoded paths — for all custom config override locations within rendered config files. No rendered config file SHALL contain literal references to custom config under `/workspace` (legacy singular mount, removed in change 5) or `/workspaces/...` (new multi-workspace bind-mount paths). Custom config belongs in the agent's home directory, not in any workspace mount.

#### Scenario: Core config files use templatized custom path
- **WHEN** `templates/config/core/.gitconfig` and `templates/config/core/.bashrc` are rendered
- **THEN** custom config references resolve to the value of `{{ custom_config_core }}` (typically `/home/agent/.sandbox/custom`); they do NOT contain `/workspace/.sandbox/custom/` (legacy) or `/workspaces/<ws>/.sandbox/custom/` (post-change-5 violation)

#### Scenario: No hardcoded workspace sandbox paths in rendered output
- **WHEN** all config templates are rendered
- **THEN** zero files in the rendered instance contain `/workspace/.sandbox/`, `/workspace/.tmux_resurrect`, or any analogous path under `/workspaces/<ws>/`

### Requirement: Gitconfig Default Filter Removal
The `templates/config/core/.gitconfig` template SHALL use bare `{{ git_user }}` and `{{ git_email }}` without Jinja2 `| default()` filters. Default resolution is the responsibility of `build_jinja_context()`.

#### Scenario: Gitconfig uses bare context variables
- **WHEN** `templates/config/core/.gitconfig` template source is inspected
- **THEN** it contains `{{ git_user }}` and `{{ git_email }}` without `| default(...)` filters

### Requirement: Read-Only Domains Context Key
The system SHALL include `proxy_whitelist_read_only_domains` in the Jinja2 context returned by `build_jinja_context()`, sourced from `config.proxy_whitelist.read_only_domains`.

#### Scenario: Read-only domains context key present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `proxy_whitelist_read_only_domains` (from `config.proxy_whitelist.read_only_domains`)

### Requirement: Read-Only Domains File Generation

The system SHALL generate `config/proxy/read_only_domains.txt` during `render_templates()`, containing one domain per line from the `proxy_whitelist_read_only_domains` context key. The file lands at `<sandbox_ai_home()>/instances/<inst>/config/proxy/read_only_domains.txt`. This follows the same generation pattern as `allowed_domains.txt`.

#### Scenario: read_only_domains.txt generated
- **WHEN** `render_templates()` completes
- **THEN** `<sandbox_ai_home()>/instances/<inst>/config/proxy/read_only_domains.txt` exists and contains one domain per line from the configured `read_only_domains` list

#### Scenario: Empty read_only_domains produces empty file
- **WHEN** `render_templates()` runs with `proxy_whitelist.read_only_domains = []`
- **THEN** `<sandbox_ai_home()>/instances/<inst>/config/proxy/read_only_domains.txt` is created but empty

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

### Requirement: Five-Subnet Context Keys
The system SHALL include all five subnet CIDR strings and all multi-network container IP addresses in the Jinja2 context returned by `build_jinja_context()`.

#### Scenario: All five subnet context keys present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `isolated_subnet`, `core_proxy_subnet`, `dns_subnet`, `egress_subnet`, and `ipc_subnet`

#### Scenario: IPC IP context keys present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `core_ipc_ip` and `admin_ipc_ip`

#### Scenario: Firecrawl isolated IP context key present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `firecrawl_isolated_ip`

#### Scenario: Proxy core IP context key present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `proxy_core_ip`

#### Scenario: dnsdist multi-network IP context keys present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `dnsdist_isolated_ip` and `dnsdist_dns_ip`

#### Scenario: coredns multi-network IP context keys present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `coredns_dns_ip` and `coredns_egress_ip`

#### Scenario: db-postgres IP context key present
- **WHEN** `build_jinja_context()` is called
- **THEN** the returned context includes `db_postgres_ip`

### Requirement: Programmatic .claude.json Generation

The system SHALL generate `.claude.json` programmatically in `render_templates()` using `json.dump`, with conditional `mcpServers` registration. The file lands at `<sandbox_ai_home()>/instances/<inst>/config/core/.claude.json`. This follows the existing pattern for `allowed_domains.txt` and `read_only_domains.txt`.

`.claude.json` is the canonical example of the **rw config file recipe class** (`RW_FILE_RECIPES` in `cli.main`): hydration writes the file at the same restrictive mode used by ro-files (`0o640` via `write_restricted` / `os.open(O_WRONLY | O_CREAT | O_EXCL, 0o640)`), bypassing the orchestrator process's umask. The helper-cp recipe later chowns the file to `agent:agent` and chmods it to `0o660` (the rw recipe target mode) so the agent in core can write to it at runtime. The hydration-time mode is therefore restrictive (`0o640`); the helper-cp post-recipe mode is `0o660`. Both are documented in `orchestrator-volumes`'s `Helper-CP Source Files Daemon-Readable Pre-Recipe` requirement (RW recipe sub-table).

#### Scenario: .claude.json generated during render_templates
- **WHEN** `render_templates()` completes
- **THEN** `<sandbox_ai_home()>/instances/<inst>/config/core/.claude.json` exists and contains valid JSON

#### Scenario: .claude.json written at restrictive mode
- **WHEN** `render_templates()` writes `.claude.json`
- **THEN** the file is created via `write_restricted(path, content, 0o640)` (which delegates to `os.open(path, O_WRONLY | O_CREAT | O_EXCL, 0o640)` and `os.write`); the on-disk mode at hydration time is `0o640` (NOT respecting the orchestrator's umask). The helper-cp recipe subsequently chmods it to `0o660` per `RW_FILE_RECIPES`'s target mode.

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

The system SHALL copy `templates/docker/coredns/Dockerfile.coredns` as a static file (not Jinja2-rendered) during `render_templates()`, using the existing `_copy_file()` mechanism (or its `importlib.resources`-based equivalent). The `docker/coredns` subdirectory SHALL be included in `INSTANCE_SUBDIRS`. The destination is `<sandbox_ai_home()>/instances/<inst>/docker/coredns/Dockerfile.coredns`.

#### Scenario: CoreDNS Dockerfile copied to instance
- **WHEN** `render_templates()` completes
- **THEN** `<sandbox_ai_home()>/instances/<inst>/docker/coredns/Dockerfile.coredns` exists and is identical to `templates/docker/coredns/Dockerfile.coredns` (the source from the templates package)

#### Scenario: docker/coredns directory created by scaffold
- **WHEN** `create_instance_dirs()` is called
- **THEN** a `docker/coredns` subdirectory exists in the instance directory at `<sandbox_ai_home()>/instances/<inst>/docker/coredns`

#### Scenario: CoreDNS Dockerfile validated as static file
- **WHEN** `validate_templates()` runs
- **THEN** `templates/docker/coredns/Dockerfile.coredns` is included in the static file existence check and counts toward the validated total

### Requirement: Workspace Bridge gid Context Key

The Jinja2 context returned by `build_jinja_context()` SHALL include `in_container_workspace_bridge_gid`, computed at hydration time via `in_container_gid_for_host_gid(workspace_bridge_gid(host), host.docker_unprivileged_user)`. The compose template references this key for the `group_add` entries on `core` and `admin` services (per `compose-security-baseline`'s "Workspace Bridge Group Membership" requirement).

#### Scenario: Context includes in_container_workspace_bridge_gid
- **WHEN** `build_jinja_context()` is called and the host has a configured bridge group at a valid subgid-range gid
- **THEN** the returned context includes `in_container_workspace_bridge_gid` with the integer value of the in-container gid

#### Scenario: Bridge group resolution failure aborts hydration
- **WHEN** `build_jinja_context()` is called and `workspace_bridge_gid(host)` raises (group missing or out of subgid range)
- **THEN** hydration aborts with the propagated error before any template is rendered; the operator is directed to `sandbox doctor` for remediation

#### Scenario: StrictUndefined catches missing bridge gid in compose template
- **WHEN** `compose.yml` references `{{ in_container_workspace_bridge_gid }}` and the context omits the key (e.g., due to a build_jinja_context regression)
- **THEN** `jinja2.StrictUndefined` raises `UndefinedError` during rendering, identifying the missing key

### Requirement: Hydration Writes Sensitive Files at Restrictive Mode

Hydration SHALL write sensitive files at restrictive modes from creation, bypassing the orchestrator process's umask. This requirement complements `orchestrator-volumes`'s "Hydration Writes Sensitive Files at Restrictive Mode" by anchoring the contract in the hydration pipeline's responsibility.

#### Scenario: Ro config files use os.open with explicit mode 0640
- **WHEN** `render_templates()` writes a file in the consumer-uid-0-chown ro-files set (Corefile, dnsdist conf, dotfiles, sshd_config, all 5 proxy files)
- **THEN** the file is created via `os.open(path, O_WRONLY | O_CREAT | O_EXCL, 0o640)` and content is written via `os.write` (or equivalent that does NOT pass through `open(..., "w")` which would respect umask)

#### Scenario: Programmatically-generated ro files also use restrictive mode
- **WHEN** `render_templates()` programmatically generates `allowed_domains.txt`, `read_only_domains.txt`, or `.htpasswd`
- **THEN** the same `os.open` + explicit mode 0640 pattern is used

