## Purpose

This specification governs the zero-trust IPAM bridging logic isolating internal namespace execution paths from global DNS routing structures. It enforces deterministic mathematical boundaries for `/24` subnet allocation, slot reuse, overflow detection, and cryptographic proxy authentication.

## Requirements

### Requirement: Global IPv4 and Port Demilitarization
The system SHALL allocate three consecutive `/24` subnets per sandbox instance from the `10.100.0.0–10.255.255.0` address space, with the allocation ledger mapping `project_id` to a reusable `base_index` integer.

#### Scenario: Triple /24 Subnet Allocation
- **WHEN** a new sandbox instance requires IPAM allocation
- **THEN** the system assigns the lowest available `base_index` (integer, 0–13311) and derives three subnets as: `ISOLATED_NET = 10.(100 + g//256).(g%256).0/24`, `PROXY_NET = 10.(100 + (g+1)//256).((g+1)%256).0/24`, `EGRESS_NET = 10.(100 + (g+2)//256).((g+2)%256).0/24` where `g = base_index * 3`

#### Scenario: Idempotent Re-Allocation on Restart
- **WHEN** `sandbox start` is invoked for an instance already present in `ipam.json`
- **THEN** the previously assigned `base_index` is reused and subnets are re-derived without modifying the ledger

#### Scenario: COMPOSE_PROJECT_NAME Isolation
- **WHEN** two `sandbox start` invocations execute from separate project directories
- **THEN** each receives a distinct `base_index` and non-overlapping `/24` subnet triples, preventing Docker network and volume name collisions

### Requirement: IPAM Slot Reuse After Destroy
The system SHALL support reuse of previously allocated `base_index` slots once their owning instance is destroyed.

#### Scenario: Freed slot available for new allocation
- **WHEN** `sandbox destroy` removes a `project_id` entry from `ipam.json`
- **THEN** that `base_index` is eligible for allocation by the next new instance that invokes `sandbox start`

### Requirement: IPAM Overflow Detection
The system SHALL detect when all 13,312 concurrent slots are in use and abort allocation with an actionable error message.

#### Scenario: Exhausted ledger raises error
- **WHEN** all base_index values 0–13311 are in use in `ipam.json` and a new allocation is attempted
- **THEN** the CLI raises `IPAMExhaustedError` with the message: "IPAM address space exhausted. Run 'sandbox destroy' on unused instances to free slots."

### Requirement: DMZ Egress Constraints & Cryptography
The system SHALL structurally enforce internet isolation patterns natively through Docker Extension Metadata bridging (`x-sandbox-meta`).

#### Scenario: 4-Tier Zero-Trust Trapping
- **WHEN** an infrastructure sidecar (e.g., `mcp-firecrawl` or `puppeteer`) triggers the declarative override `require_egress: true`
- **THEN** the Orchestrator's compiler functionally maps the container into the heavily guarded `proxy_net` bound, skipping standard `isolated_net` lock downs to definitively establish proxy bypass navigation.

#### Scenario: Ephemeral Cryptographic Authentication
- **WHEN** the proxy squid container initializes bounds
- **THEN** the CLI generates a 32-character string employing Python `secrets`, hashes it via bcrypt, and writes the `proxyuser:<hash>` line to `.htpasswd` for Squid proxy authentication.

### Requirement: Component Static IP Derivation
The system SHALL derive static IP addresses for component containers from the same `base_index` used for infrastructure IPs, using fixed host octets per component.

#### Scenario: Firecrawl IPs derived from base_index
- **WHEN** `derive_static_ips(base_index)` is called
- **THEN** the returned dict includes `mcp_firecrawl_isolated_ip` as `<isolated_base>.55` and `mcp_firecrawl_proxy_ip` as `<proxy_base>.55`

#### Scenario: Component IPs are deterministic across restarts
- **WHEN** `derive_static_ips()` is called with the same `base_index` on successive `sandbox start` invocations
- **THEN** the returned firecrawl IPs are identical
