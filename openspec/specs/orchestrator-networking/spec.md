## Purpose

This specification governs the zero-trust IPAM bridging logic natively isolating internal namespace execution paths from global DNS routing structures. It enforces deterministic mathematical boundaries spanning L4 loopback IP subsetting, declarative DMZ egress constraints mapping explicitly to `proxy_net`, and cryptographic slugging guarantees against collision overlaps.

## Requirements

### Requirement: Global IPv4 and Port Demilitarization
The system SHALL strictly bind individual Linux Loopback IPC matrices and `/16` subsets sequentially across initialized environments to bypass global DNS fragmentation.

#### Scenario: Ephemeral Loopback Allocation
- **WHEN** an orchestrator spin-up sequence triggers
- **THEN** the system mathematically claims the next `/16` subnet bound and unique `127.x.x.x` Loopback IP for namespace execution.

#### Scenario: Caddy Proxy Service Mesh Injection
- **WHEN** the Orchestrator finalizes the IPAM ledger assignment
- **THEN** it structurally grafts `caddy.listen=127.X.X.X:443` Docker labels into the ephemeral `dynamic.override.yml`, guaranteeing safe local proxy navigation without escalating `sudo` to manipulate host DNS logic.

#### Scenario: DBeaver Layer-4 Host UI Extraction
- **WHEN** the underlying Docker matrix bounds Database sidecars (`db-postgres`) purely behind `backend_net`
- **THEN** the Orchestrator mechanically injects a mapped Host L4 binding tracking exclusively to the project's unique loopback `127.x.x.x:5432:5432`, granting frictionless external human DataGrip connectivity without broadcasting across internal LAN vectors.

### Requirement: DMZ Egress Constraints & Cryptography
The system SHALL structurally enforce internet isolation patterns natively through Docker Extension Metadata bridging (`x-sandbox-meta`).

#### Scenario: 4-Tier Zero-Trust Trapping
- **WHEN** an infrastructure sidecar (e.g., `mcp-firecrawl` or `puppeteer`) triggers the declarative override `require_egress: true`
- **THEN** the Orchestrator's compiler functionally maps the container into the heavily guarded `proxy_net` bound, skipping standard `isolated_net` lock downs to definitively establish proxy bypass navigation.

#### Scenario: Ephemeral Cryptographic Authentication
- **WHEN** the proxy squid container initializes bounds
- **THEN** the CLI generates a 32-character string employing Python `secrets`, mathematically formats it to Apache `$apr1$` spec, and explicitly writes the hash natively inside `./.sandbox/configs/squid.htpasswd` to securely authenticate the Squid array.

### Requirement: Target Path Cryptographic Slugging
The system SHALL guarantee mathematical environment isolation when overlapping Host directories share identically named variables.

#### Scenario: `COMPOSE_PROJECT_NAME` Isolation
- **WHEN** two identical `sandbox start` loops execute from separate `/api` repositories on the filesystem
- **THEN** the Orchestrator automatically resolves a structural bound computing an MD5 hash of the absolute directory path (`api-8f3a9e`), preventing default network / volume corruption directly inside the Compose execution matrix.
