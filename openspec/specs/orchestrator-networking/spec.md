## Purpose

This specification governs the zero-trust IPAM bridging logic isolating internal namespace execution paths from global DNS routing structures. It enforces deterministic mathematical boundaries for `/24` subnet allocation, slot reuse, overflow detection, container network membership, and cryptographic proxy authentication.

## Requirements

### Requirement: Global IPv4 and Port Demilitarization
The system SHALL allocate seven consecutive `/24` subnets per sandbox instance from the `10.100.0.0–10.255.255.0` address space, with the allocation ledger mapping `instance_id` to a reusable `base_index` integer.

#### Scenario: Seven /24 Subnet Allocation
- **WHEN** a new sandbox instance requires IPAM allocation
- **THEN** the system assigns the lowest available `base_index` (integer, 0–5704) and derives seven subnets as: `ISOLATED_NET = 10.(100 + g//256).(g%256).0/24`, `CORE_PROXY_NET = 10.(100 + (g+1)//256).((g+1)%256).0/24`, `DNS_NET = 10.(100 + (g+2)//256).((g+2)%256).0/24`, `ADMIN_NET = 10.(100 + (g+3)//256).((g+3)%256).0/24`, `ADMIN_PROXY_NET = 10.(100 + (g+4)//256).((g+4)%256).0/24`, `EGRESS_NET = 10.(100 + (g+5)//256).((g+5)%256).0/24`, `IPC_NET = 10.(100 + (g+6)//256).((g+6)%256).0/24` where `g = base_index * 7`

#### Scenario: Idempotent Re-Allocation on Restart
- **WHEN** `sandbox start` is invoked for an instance already present in `ipam.json`
- **THEN** the previously assigned `base_index` is reused and subnets are re-derived without modifying the ledger

#### Scenario: COMPOSE_PROJECT_NAME Isolation
- **WHEN** two `sandbox start` invocations execute from separate project directories
- **THEN** each receives a distinct `base_index` and non-overlapping `/24` subnet septuples, preventing Docker network and volume name collisions

### Requirement: IPAM Slot Reuse After Destroy
The system SHALL support reuse of previously allocated `base_index` slots once their owning instance is destroyed.

#### Scenario: Freed slot available for new allocation
- **WHEN** `sandbox destroy` removes an `instance_id` entry from `ipam.json`
- **THEN** that `base_index` is eligible for allocation by the next new instance that invokes `sandbox start`

### Requirement: IPAM Overflow Detection
The system SHALL detect when all 5,705 concurrent slots are in use and abort allocation with an actionable error message.

#### Scenario: Exhausted ledger raises error
- **WHEN** all base_index values 0–5704 are in use in `ipam.json` and a new allocation is attempted
- **THEN** the CLI raises `IPAMExhaustedError` with the message: "IPAM address space exhausted. Run 'sandbox destroy' on unused instances to free slots."

### Requirement: Component Static IP Derivation
The system SHALL derive static IP addresses for component containers from the same `base_index` used for infrastructure IPs, using fixed host octets per component. Containers with membership on multiple networks SHALL have distinct static IPs on each network.

#### Scenario: Core IPs derived from base_index
- **WHEN** `derive_static_ips(base_index)` is called
- **THEN** the returned dict includes `agent_isolated_ip` as `<isolated_base>.3`, `agent_proxy_ip` as `<core_proxy_base>.3`, and `core_ipc_ip` as `<ipc_base>.3`

#### Scenario: Admin IPs derived from base_index
- **WHEN** `derive_static_ips(base_index)` is called
- **THEN** the returned dict includes `admin_admin_ip` as `<admin_base>.2`, `admin_proxy_ip` as `<admin_proxy_base>.2`, and `admin_ipc_ip` as `<ipc_base>.2`

#### Scenario: Proxy IPs derived from base_index
- **WHEN** `derive_static_ips(base_index)` is called
- **THEN** the returned dict includes `proxy_core_ip` as `<core_proxy_base>.254` and `proxy_admin_ip` as `<admin_proxy_base>.254`

#### Scenario: dnsdist IPs derived from base_index
- **WHEN** `derive_static_ips(base_index)` is called
- **THEN** the returned dict includes `dnsdist_isolated_ip` as `<isolated_base>.56`, `dnsdist_dns_ip` as `<dns_base>.56`, and `dnsdist_admin_ip` as `<admin_base>.56`

#### Scenario: coredns IPs derived from base_index
- **WHEN** `derive_static_ips(base_index)` is called
- **THEN** the returned dict includes `coredns_dns_ip` as `<dns_base>.53`, `coredns_admin_ip` as `<admin_base>.53`, and `coredns_egress_ip` as `<egress_base>.53`

#### Scenario: Admin IPs derived from base_index (legacy position)
- **WHEN** `derive_static_ips(base_index)` is called
- **THEN** the returned dict includes `admin_admin_ip` as `<admin_base>.2` and `admin_proxy_ip` as `<admin_proxy_base>.2`

#### Scenario: db-postgres IPs derived from base_index
- **WHEN** `derive_static_ips(base_index)` is called
- **THEN** the returned dict includes `db_postgres_ip` as `<isolated_base>.54` and `db_postgres_admin_ip` as `<admin_base>.54`

#### Scenario: Firecrawl IPs derived from base_index
- **WHEN** `derive_static_ips(base_index)` is called
- **THEN** the returned dict includes `mcp_firecrawl_proxy_ip` as `<core_proxy_base>.55`, `firecrawl_dns_ip` as `<dns_base>.55`, and `firecrawl_isolated_ip` as `<isolated_base>.55`

#### Scenario: Component IPs are deterministic across restarts
- **WHEN** `derive_static_ips()` is called with the same `base_index` on successive `sandbox start` invocations
- **THEN** all returned IPs are identical

#### Scenario: Legacy IP keys removed
- **WHEN** `derive_static_ips(base_index)` is called
- **THEN** the returned dict does NOT contain `dns_sidecar_ip` (replaced by `coredns_dns_ip`), `admin_isolated_ip` (replaced by `admin_admin_ip`), `mcp_firecrawl_isolated_ip` (replaced by `firecrawl_isolated_ip`), or `proxy_ip` (replaced by `proxy_core_ip`)

### Requirement: Zero-Shared-Network Invariant
The system SHALL ensure that core (agent) and admin (human) containers share zero Docker networks for non-IPC traffic. The `ipc_net` subnet SHALL be the sole shared network, used exclusively for SSH-based IPC. All inter-container communication between core and admin SHALL occur exclusively via TCP on `ipc_net`.

#### Scenario: Core and admin share only ipc_net
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the intersection of networks assigned to the core service and the admin service is exactly `{ipc_net}`

#### Scenario: Core has no path to admin networks
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the core service is NOT on `admin_net` or `admin_proxy_net`

#### Scenario: Admin has no path to core networks
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the admin service is NOT on `isolated_net` or `core_proxy_net`

### Requirement: Container Network Membership
The system SHALL assign each container to the minimum set of networks required for its function. The `ipc_net` network SHALL be `internal: true` with IPv6 disabled and IP masquerade disabled.

#### Scenario: Core network membership
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the core service is on exactly `isolated_net`, `core_proxy_net`, and `ipc_net`

#### Scenario: Admin network membership
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the admin service is on exactly `admin_net`, `admin_proxy_net`, and `ipc_net`

#### Scenario: Firecrawl network membership
- **WHEN** the rendered `mcp-firecrawl.yml` is inspected
- **THEN** the firecrawl service is on exactly `core_proxy_net`, `dns_net`, and `isolated_net`

#### Scenario: ipc_net is internal
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** `ipc_net` has `internal: true`, `enable_ipv6: false`, and `com.docker.network.bridge.enable_ip_masquerade: 'false'`

#### Scenario: Proxy network membership unchanged
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the proxy service is on exactly `core_proxy_net`, `admin_proxy_net`, and `egress_net`

#### Scenario: coredns network membership unchanged
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the coredns service is on exactly `dns_net`, `admin_net`, and `egress_net`

#### Scenario: dnsdist network membership unchanged
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the dnsdist service is on exactly `isolated_net`, `dns_net`, and `admin_net`

#### Scenario: db-postgres network membership unchanged
- **WHEN** the rendered `compose.yml` is inspected (including `db-postgres.yml` extras)
- **THEN** the db-postgres service is on exactly `isolated_net` and `admin_net`

#### Scenario: New networks are internal
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** `core_proxy_net`, `dns_net`, `admin_net`, and `admin_proxy_net` all have `internal: true`, `enable_ipv6: false`, and `com.docker.network.bridge.enable_ip_masquerade: 'false'`

### Requirement: Per-Container NO_PROXY Scoping
The system SHALL set `NO_PROXY` environment variables per container, scoped to only the subnets each container belongs to.

#### Scenario: Core NO_PROXY scoped to its networks
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the core service's `NO_PROXY` includes `{{ isolated_subnet }}`, `{{ core_proxy_subnet }}`, and `{{ ipc_subnet }}` but does NOT include `{{ admin_subnet }}` or `{{ admin_proxy_subnet }}`

#### Scenario: Admin NO_PROXY scoped to its networks
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the admin service's `NO_PROXY` includes `{{ admin_subnet }}`, `{{ admin_proxy_subnet }}`, and `{{ ipc_subnet }}` but does NOT include `{{ isolated_subnet }}` or `{{ core_proxy_subnet }}`

#### Scenario: Firecrawl NO_PROXY scoped to its networks
- **WHEN** the rendered `mcp-firecrawl.yml` is inspected
- **THEN** the firecrawl service's `NO_PROXY` includes `{{ core_proxy_subnet }}` and `{{ dns_subnet }}` but does NOT include `{{ isolated_subnet }}`, `{{ admin_subnet }}`, or `{{ admin_proxy_subnet }}`

### Requirement: Service Rename dns-sidecar to coredns
The system SHALL rename the `dns-sidecar` service to `coredns` in compose templates and rename the configuration directory from `.config/dns-sidecar/` to `.config/coredns/`.

#### Scenario: Service name in compose
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the DNS service is named `coredns`, not `dns-sidecar`

#### Scenario: Config directory renamed
- **WHEN** the tooling plane is inspected
- **THEN** `.config/coredns/Corefile` exists and `.config/dns-sidecar/` does NOT exist

### Requirement: Per-Container extra_hosts Resolution
The system SHALL configure `extra_hosts` entries per container to resolve cross-network service names to the correct static IP on each container's own network. Containers SHALL reference the proxy and dnsdist IPs from their respective networks.

#### Scenario: Core extra_hosts
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the core service's `extra_hosts` includes `proxy:{{ proxy_core_ip }}` and `dnsdist:{{ dnsdist_isolated_ip }}`

#### Scenario: Admin extra_hosts
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the admin service's `extra_hosts` includes `proxy:{{ proxy_admin_ip }}` and `db-postgres:{{ db_postgres_admin_ip }}`

#### Scenario: Firecrawl extra_hosts
- **WHEN** the rendered `mcp-firecrawl.yml` is inspected
- **THEN** the firecrawl service's `extra_hosts` includes `proxy:{{ proxy_core_ip }}` and `dnsdist:{{ dnsdist_dns_ip }}`

#### Scenario: Firecrawl stale extra_hosts removed
- **WHEN** the rendered `mcp-firecrawl.yml` is inspected
- **THEN** the firecrawl service's `extra_hosts` does NOT contain entries for `db-postgres` or `dns-sidecar` (unreachable after network move)

