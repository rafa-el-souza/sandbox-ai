## Purpose

This specification defines the dnsdist-based DNS exfiltration defense layer, including container deployment, wire-length and label-count filtering, per-container DNS routing, healthcheck configuration, container hardening, and service dependency ordering.

## Requirements

### Requirement: dnsdist Infrastructure Container
The system SHALL deploy a `dnsdist` container (PowerDNS dnsdist 1.9.x) as a DNS filtering proxy on `isolated_net`, `dns_net`, and `admin_net`. The container SHALL forward surviving queries to coredns on `dns_net` and SHALL have no direct egress path to the internet.

#### Scenario: dnsdist forwards to coredns
- **WHEN** a DNS query survives all filtering rules
- **THEN** dnsdist forwards it to coredns at `{{ coredns_dns_ip }}:53` on `dns_net`

#### Scenario: dnsdist has no egress network membership
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the dnsdist service has network memberships on `isolated_net`, `dns_net`, and `admin_net` only — it is NOT on `egress_net`, `core_proxy_net`, or `admin_proxy_net`

#### Scenario: dnsdist listens on all interfaces
- **WHEN** the rendered `dnsdist.conf` is inspected
- **THEN** it contains `setLocal("0.0.0.0:53")`

### Requirement: DNS Wire-Length Exfiltration Filter
The system SHALL drop DNS queries whose QNAME wire-format length exceeds 65 bytes, using the native C++ `QNameWireLengthRule` selector (not a Lua per-packet rule).

#### Scenario: Long QNAME query dropped
- **WHEN** a DNS query with a QNAME wire length exceeding 65 bytes reaches dnsdist
- **THEN** dnsdist drops the query via `QNameWireLengthRule(0, 65)` paired with `DropAction()`

#### Scenario: Normal QNAME query passes
- **WHEN** a DNS query with a QNAME wire length of 30 bytes reaches dnsdist
- **THEN** the query is forwarded to coredns

#### Scenario: Rule uses native C++ selector
- **WHEN** the rendered `dnsdist.conf` is inspected
- **THEN** it contains `addAction(QNameWireLengthRule(0, 65), DropAction())` — not a `LuaRule`

### Requirement: DNS Label-Count Exfiltration Filter
The system SHALL drop DNS queries whose QNAME has more than 7 labels, using the native C++ `QNameLabelsCountRule` selector.

#### Scenario: Deep label chain dropped
- **WHEN** a DNS query with 8 or more labels (e.g., `a.b.c.d.e.f.g.h.example.com`) reaches dnsdist
- **THEN** dnsdist drops the query via `QNameLabelsCountRule(0, 7)` paired with `DropAction()`

#### Scenario: Normal label count passes
- **WHEN** a DNS query with 4 labels (e.g., `api.github.com`) reaches dnsdist
- **THEN** the query is forwarded to coredns

### Requirement: Per-Container DNS Routing Through dnsdist
The system SHALL configure each DNS-consuming container to resolve through dnsdist (not coredns directly). Core SHALL use `dnsdist_isolated_ip`, admin SHALL use `dnsdist_admin_ip`, and firecrawl SHALL use `dnsdist_dns_ip`.

#### Scenario: Core DNS points to dnsdist on isolated_net
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the core service's `dns` directive points to `{{ dnsdist_isolated_ip }}`

#### Scenario: Admin DNS points to dnsdist on admin_net
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the admin service's `dns` directive points to `{{ dnsdist_admin_ip }}`

#### Scenario: Firecrawl DNS points to dnsdist on dns_net
- **WHEN** the rendered `mcp-firecrawl.yml` is inspected
- **THEN** the firecrawl service's `dns` directive points to `{{ dnsdist_dns_ip }}`

### Requirement: dnsdist Healthcheck via Control Socket
The system SHALL configure a loopback-only control socket for dnsdist healthchecks. The healthcheck SHALL use `dnsdist -e 'showServers()'` to verify the forwarding backend is reachable.

#### Scenario: Control socket bound to loopback
- **WHEN** the rendered `dnsdist.conf` is inspected
- **THEN** it contains `controlSocket("127.0.0.1:5199")`

#### Scenario: Healthcheck command configured
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the dnsdist service's healthcheck test is `["CMD-SHELL", "dnsdist -e 'showServers()' || exit 1"]`

### Requirement: dnsdist Container Hardening
The dnsdist container SHALL inherit the security baseline and re-grant only `NET_BIND_SERVICE` for port 53 binding. It SHALL run as `pdns:pdns` (uid 953, gid 953), disable IP forwarding, and disable IPv6.

#### Scenario: dnsdist inherits security baseline
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the dnsdist service contains `security_opt: [no-new-privileges:true]`, `cap_drop: [ALL]`, `ipc: private`, `init: true`, and `read_only: true`

#### Scenario: dnsdist capability grant
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the dnsdist service contains `cap_add: [NET_BIND_SERVICE]`

#### Scenario: dnsdist runs as pdns user
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the dnsdist service contains `user: "pdns:pdns"`

#### Scenario: dnsdist IP forwarding disabled
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the dnsdist service's `sysctls` block contains `net.ipv4.ip_forward=0`

#### Scenario: dnsdist resource limits
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the dnsdist service contains `pids_limit: 100`, `mem_limit: "512m"`, `memswap_limit: "512m"`, and `cpus: "0.5"`

#### Scenario: dnsdist logging configuration
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the dnsdist service's `logging` block contains `driver: "json-file"` with options `max-size: "50m"` and `max-file: "5"`

### Requirement: dnsdist Service Dependency
The dnsdist container SHALL depend on coredns being healthy. Containers that consume DNS through dnsdist (core, admin, firecrawl) SHALL depend on dnsdist being healthy.

#### Scenario: dnsdist depends on coredns
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the dnsdist service has `depends_on: coredns: condition: service_healthy`

#### Scenario: Core depends on dnsdist
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the core service has `depends_on` including `dnsdist: condition: service_healthy`

#### Scenario: Admin depends on dnsdist
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the admin service has `depends_on` including `dnsdist: condition: service_healthy`

#### Scenario: Firecrawl depends on dnsdist
- **WHEN** the rendered `mcp-firecrawl.yml` is inspected
- **THEN** the firecrawl service has `depends_on` including `dnsdist: condition: service_healthy`

### Requirement: dnsdist Command Array Content
The compose template's dnsdist `command:` array SHALL contain only CLI arguments — it SHALL NOT include the binary name as the first element. The `powerdns/dnsdist-19` entrypoint wrapper (`dnsdist-startup`) prepends the binary via `os.execv(program, [program] + args + sys.argv[1:])`, so compose's `command:` becomes `sys.argv[1:]`. Including the binary name causes it to be parsed as a positional listen address, resulting in a `ComboAddress` parse failure.

#### Scenario: dnsdist command array excludes binary name
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the dnsdist service's `command` field is `["--supervised", "-C", "/etc/dnsdist/dnsdist.conf"]`

#### Scenario: dnsdist command array does not contain bare binary name
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the dnsdist service's `command` array does not contain `"dnsdist"` as any element
