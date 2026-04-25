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
The security baseline anchor SHALL contain only scalar properties. List-valued properties (`cap_add`, `sysctls`, `tmpfs`) SHALL remain per-service because YAML anchor merge (`<<:`) replaces list values rather than appending.

#### Scenario: Baseline does not contain cap_add
- **WHEN** the `x-security-baseline` block is inspected
- **THEN** it does NOT contain `cap_add`, `sysctls`, or `tmpfs`

#### Scenario: Per-service cap_add preserved
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** coredns has `cap_add: [NET_BIND_SERVICE]`, proxy has `cap_add: [SETUID, SETGID]`, and these values are defined per-service, not inherited from the baseline

### Requirement: Core and Admin read_only Override
Core and admin services SHALL override the baseline's `read_only: true` with `read_only: false` until writable-path enumeration is completed (Wave 4 dependency).

#### Scenario: Core overrides read_only
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the core service contains `read_only: false`

#### Scenario: Admin overrides read_only
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** the admin service contains `read_only: false`

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
