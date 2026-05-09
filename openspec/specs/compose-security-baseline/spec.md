## Purpose

This specification defines the shared Docker Compose security baseline anchor (`x-security-baseline`) that centralizes scalar hardening properties across all services, and the per-service overrides and IP forwarding controls.
## Requirements
### Requirement: Shared Security Baseline YAML Anchor
The `compose.yml` template SHALL declare an `x-security-baseline` YAML extension field with a `&security-baseline` anchor containing scalar hardening properties shared across all services. Each service SHALL merge the baseline via `<<: *security-baseline`.

#### Scenario: Baseline anchor defined
- **WHEN** the `compose.yml` template source is inspected
- **THEN** it contains an `x-security-baseline: &security-baseline` block with `security_opt: [no-new-privileges:true]`, `cap_drop: [ALL]`, `ipc: private`, `init: true`, and `read_only: true`

#### Scenario: All services inherit baseline
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** every service (coredns, proxy, core, admin, dnsdist) contains `security_opt: [no-new-privileges:true]`, `cap_drop: [ALL]`, `ipc: private`, and `init: true`

#### Scenario: Extras services inherit baseline
- **WHEN** the rendered extras templates (`db-postgres.yml`, `mcp-firecrawl.yml`) are inspected
- **THEN** each service block contains `security_opt: [no-new-privileges:true]`, `cap_drop: [ALL]`, `ipc: private`, `init: true`, and `read_only: true`

### Requirement: Baseline Excludes List-Valued Properties
The security baseline anchor SHALL contain only scalar properties. List-valued properties (`cap_add`, `sysctls`, `tmpfs`) SHALL remain per-service because YAML anchor merge (`<<:`) replaces list values rather than appending. Per-service `security_opt:` overrides SHALL be used when a service requires a deviation from the baseline scalar value (e.g., `no-new-privileges:false`).

#### Scenario: Baseline does not contain cap_add
- **WHEN** the `x-security-baseline` block is inspected
- **THEN** it does NOT contain `cap_add`, `sysctls`, or `tmpfs`

#### Scenario: Per-service cap_add preserved
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** coredns has `cap_add: [NET_BIND_SERVICE]`, proxy has `cap_add: [SETUID, SETGID]`, core has `cap_add: [CHOWN]`, and these values are defined per-service, not inherited from the baseline

#### Scenario: Core security_opt overrides baseline
- **WHEN** the rendered `compose.yml` is inspected for the core service
- **THEN** the core service contains `security_opt: [no-new-privileges:false]` as a per-service override, and this value replaces the baseline's `no-new-privileges:true`

#### Scenario: Non-core services retain baseline security_opt
- **WHEN** the rendered `compose.yml` is inspected for coredns, proxy, admin, and dnsdist services
- **THEN** each service contains `security_opt: [no-new-privileges:true]` (inherited from baseline, not overridden)

### Requirement: Core and Admin read_only Override
Core and admin services SHALL inherit the baseline's `read_only: true` without override. The `read_only: false` overrides SHALL be removed.

#### Scenario: Core inherits read_only true
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the core service does NOT contain `read_only: false` and inherits `read_only: true` from the security baseline

#### Scenario: Admin inherits read_only true
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the admin service does NOT contain `read_only: false` and inherits `read_only: true` from the security baseline

#### Scenario: Infrastructure services retain read_only true
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the coredns, proxy, and dnsdist services contain `read_only: true` (inherited from baseline, not overridden)

### Requirement: IP Forwarding Disabled on All Containers
All containers SHALL include `net.ipv4.ip_forward=0` in their `sysctls` block as defense-in-depth against runtime misconfiguration (e.g., fallback to `runc` during debugging where the host default may be `1`).

#### Scenario: Core has ip_forward disabled
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the core service's `sysctls` block contains `net.ipv4.ip_forward=0`

#### Scenario: Admin has ip_forward disabled
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the admin service's `sysctls` block contains `net.ipv4.ip_forward=0`

#### Scenario: db-postgres has ip_forward disabled
- **WHEN** the rendered `db-postgres.yml` is inspected
- **THEN** the db-postgres service's `sysctls` block contains `net.ipv4.ip_forward=0`

#### Scenario: Firecrawl has ip_forward disabled
- **WHEN** the rendered `mcp-firecrawl.yml` is inspected
- **THEN** the firecrawl service's `sysctls` block contains `net.ipv4.ip_forward=0`

### Requirement: CoreDNS Healthcheck Probe
The coredns service in `compose.yml` SHALL use a `CMD` (exec-form) healthcheck with a statically-linked `/wget` binary copied from a busybox multi-stage build. The coredns service SHALL use a `build` directive (not `image`) to incorporate the probe binary.

#### Scenario: CoreDNS healthcheck uses exec-form CMD with /wget
- **WHEN** the rendered `compose.yml` is inspected for the coredns service
- **THEN** the healthcheck test is `CMD ["/wget", "--spider", "-q", "http://127.0.0.1:8080/health"]`

#### Scenario: CoreDNS uses build directive
- **WHEN** the rendered `compose.yml` is inspected for the coredns service
- **THEN** it uses `build:` with `context`, `dockerfile`, and `args` keys (not a top-level `image:` directive)

#### Scenario: CoreDNS build args include base images
- **WHEN** the rendered `compose.yml` is inspected for the coredns service
- **THEN** the `build.args` include `CORE_BASE` (from `dns_image`) and `BUSYBOX_BASE` (from `busybox_image`)

#### Scenario: CMD-SHELL wget is not used
- **WHEN** the rendered `compose.yml` is inspected for the coredns service
- **THEN** the healthcheck does NOT use `CMD-SHELL` form (scratch images have no `/bin/sh`)

### Requirement: Proxy Healthcheck Probe
The proxy service in `compose.yml` SHALL use a `CMD` (exec-form) healthcheck with Bash's `/dev/tcp` pseudo-device for a TCP liveness probe against port 3128. This SHALL NOT require any external binary beyond `bash`.

#### Scenario: Proxy healthcheck uses bash /dev/tcp
- **WHEN** the rendered `compose.yml` is inspected for the proxy service
- **THEN** the healthcheck test is `CMD ["bash", "-c", ": > /dev/tcp/127.0.0.1/3128"]`

#### Scenario: squidclient is not used in healthcheck
- **WHEN** the rendered `compose.yml` is inspected for the proxy service
- **THEN** the healthcheck does NOT reference `squidclient`

#### Scenario: CMD-SHELL is not used for proxy healthcheck
- **WHEN** the rendered `compose.yml` is inspected for the proxy service
- **THEN** the healthcheck uses `CMD` exec form (not `CMD-SHELL`), because the default `/bin/sh` (`dash`) does not support `/dev/tcp`

### Requirement: CoreDNS Dockerfile Multi-Stage Build
The system SHALL provide `templates/docker/coredns/Dockerfile.coredns` as a static (non-Jinja2) Dockerfile that uses a multi-stage build to copy a statically-linked `wget` binary from a busybox image into the coredns image.

#### Scenario: Dockerfile uses multi-stage build
- **WHEN** `templates/docker/coredns/Dockerfile.coredns` is inspected
- **THEN** it contains a `FROM ${BUSYBOX_BASE} AS probe` stage and a `FROM ${CORE_BASE}` stage with `COPY --from=probe /bin/wget /wget`

#### Scenario: Dockerfile accepts build args
- **WHEN** `templates/docker/coredns/Dockerfile.coredns` is inspected
- **THEN** it declares `ARG CORE_BASE` and `ARG BUSYBOX_BASE` at the top

#### Scenario: No Jinja2 syntax in Dockerfile
- **WHEN** `templates/docker/coredns/Dockerfile.coredns` is inspected
- **THEN** it contains zero `{{ }}` or `{% %}` markers (values come via compose build.args, not Jinja2)

### Requirement: Extras Services Zero-Capability Posture
Extras services that run as non-root users SHALL have zero capabilities — no `cap_add` entries. Services that require root-mode entrypoints SHALL declare the minimum capability set.

#### Scenario: db-postgres runs as postgres user
- **WHEN** the rendered `db-postgres.yml` is inspected
- **THEN** the db-postgres service contains `user: "70:70"` (postgres uid:gid in Alpine)

#### Scenario: db-postgres has zero capabilities
- **WHEN** the rendered `db-postgres.yml` is inspected
- **THEN** the db-postgres service does NOT contain a `cap_add` block and contains `cap_drop: [ALL]`

#### Scenario: db-postgres cap_add is absent
- **WHEN** the rendered `db-postgres.yml` is inspected
- **THEN** the db-postgres service does NOT contain any of `CHOWN`, `FOWNER`, `SETGID`, `SETUID` in a `cap_add` block

### Requirement: tmpfs Mode for sshd StrictModes Compliance
Services with sshd auth paths through `/run` SHALL mount the `/run` tmpfs with `mode=0755`. The kernel default `1777` is rejected by sshd's `safe_path()` directory walk.

#### Scenario: Core /run tmpfs has mode 0755
- **WHEN** the `compose.yml` template source is inspected for the core service
- **THEN** the `/run` tmpfs entry includes `mode=0755`

#### Scenario: Proxy /run tmpfs has mode 0755
- **WHEN** the `compose.yml` template source is inspected for the proxy service
- **THEN** the `/run` tmpfs entry includes `mode=0755`

#### Scenario: Admin has no /run tmpfs
- **WHEN** the `compose.yml` template source is inspected for the admin service
- **THEN** the admin service's `tmpfs` block does NOT include a `/run` entry

### Requirement: Multi-Workspace Volumes Loop in Compose Template

The `compose.yml` template SHALL render workspace bind mounts via a Jinja2 loop over the `workspaces` context key (a list of `{name, path}` dicts sorted by name). Each iteration emits one volume entry of the form `{{ ws.path }}:/workspaces/{{ ws.name }}:rw` on each agent service (core and admin). The legacy single `user_project_root` volume entry is removed.

#### Scenario: Loop renders one volume per workspace per service
- **WHEN** `compose.yml` is rendered with the workspaces context list `[{name: "main", path: "/p1"}, {name: "scratch", path: "/p2"}]`
- **THEN** core and admin services each contain exactly two volume entries: `/p1:/workspaces/main:rw` and `/p2:/workspaces/scratch:rw`

#### Scenario: Loop iterates in sort order for determinism
- **WHEN** the same instance's compose.yml is rendered twice
- **THEN** the resulting volume entries appear in identical order (workspaces sorted lexicographically by name in the hydration context)

#### Scenario: No user_project_root volume
- **WHEN** the rendered compose.yml is inspected
- **THEN** there is NO volume entry referencing `user_project_root` or targeting `/workspace` (singular) on any service

### Requirement: Workspace Bridge Group Membership for core and admin

The rendered `compose.yml` SHALL include `group_add` (or compose-equivalent) entries on the `core` and `admin` services that add the in-container gid corresponding to the host workspace bridge group. The value is computed at hydration time via `in_container_gid_for_host_gid(workspace_bridge_gid(host), host.docker_unprivileged_user)`. The bridge gid is per-host (not per-workspace); a single `group_add` entry serves all bind-mounted workspaces because they share the same bridge group on the host.

#### Scenario: core service has group_add for bridge gid
- **WHEN** `compose.yml` is rendered with a configured workspace bridge group
- **THEN** the `core` service block contains `group_add: ["{{ in_container_workspace_bridge_gid }}"]` (or equivalent), with the value resolved to the in-container gid that maps to the host bridge group

#### Scenario: admin service has group_add for bridge gid
- **WHEN** `compose.yml` is rendered
- **THEN** the `admin` service block contains the same `group_add` entry

#### Scenario: One group_add entry serves all workspaces
- **WHEN** the instance has multiple workspaces
- **THEN** core and admin still have exactly one `group_add` entry each (NOT one per workspace); the bridge group is per-host, not per-workspace

#### Scenario: Numeric group_add only — no in-image group required
- **WHEN** the agent images are inspected
- **THEN** they do NOT need to define an internal `sb-ws` (or similarly-named) group at the bridge gid; Linux access checks operate on numeric gids — supplementary group membership conferred via `group_add` is sufficient regardless of whether a named group exists in `/etc/group` inside the image

### Requirement: Agent Shell Init Sets Restrictive Umask for Workspace Writes

The agent shell init files (`templates/config/core/.bashrc`, `templates/config/admin/.zshrc`) SHALL set `umask 007` so that files created by the agent under any workspace land at mode `0660` group `<bridge-group>` (via setgid + supplementary group inheritance). The umask is set across all workspace mounts (not per-workspace); a single shell-init umask covers every `/workspaces/<ws>` bind mount.

#### Scenario: Core .bashrc sets umask 007
- **WHEN** the rendered `templates/config/core/.bashrc` is inspected
- **THEN** it contains `umask 007` early in the file, before any user override hook

#### Scenario: Admin .zshrc sets umask 007
- **WHEN** the rendered `templates/config/admin/.zshrc` is inspected
- **THEN** it contains `umask 007` early in the file, before any user override hook

#### Scenario: New files in workspaces land at mode 0660
- **WHEN** the agent (in-container uid 1000, supplementary gid `<bridge-gid>`) creates a new file under any of `/workspaces/<ws>` (multiple workspaces)
- **THEN** the resulting file has mode `0660` and group `<bridge-gid>`, allowing dev (sb-ws member on host) to read/write via group bits — same behavior across all workspaces

