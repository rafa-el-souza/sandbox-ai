## Purpose

This specification defines the `sandbox status` command, which displays the current state of the sandbox instance for the current project directory — including container health, IPAM allocation, enabled components, and configuration completeness warnings.

## Requirements

### Requirement: Status Command Interface
The system SHALL provide a `sandbox status` command that displays the current state of the sandbox instance for the current project directory.

#### Scenario: Status of running instance
- **WHEN** `sandbox status` is invoked and the instance containers are running
- **THEN** the system displays a Rich Panel with instance identity (name, ID, project path, user), a Rich Table with per-container health (service name, health status, network IPs), IPAM allocation (slot and subnets), and enabled components

#### Scenario: Status of stopped instance
- **WHEN** `sandbox status` is invoked and the instance exists but containers are not running
- **THEN** the system displays a Rich Panel with instance identity and state "stopped", without querying container health

#### Scenario: Status with no instance
- **WHEN** `sandbox status` is invoked and no instance is registered for the current directory
- **THEN** the system prints "No sandbox instance found for this directory." and exits with code 1

### Requirement: Container Health Display
The system SHALL query per-container health via `docker compose ps --format json` through machinectl using the configured authentication mode and display results in a Rich Table.

#### Scenario: All containers healthy
- **WHEN** all containers with healthchecks report "healthy" and all containers are in "running" state
- **THEN** the Panel border is green and the Table shows `●` with "healthy" or "up" for each service

#### Scenario: Degraded state
- **WHEN** one or more containers report "unhealthy" or are not running
- **THEN** the Panel header includes "⚠ degraded" and the Panel border is yellow, and unhealthy containers are highlighted with `✗` in the Table

#### Scenario: Container health query via machinectl (sudo mode)
- **WHEN** the status command queries container state and `machinectl_authentication` is `"sudo"`
- **THEN** it invokes `docker compose ps --format json` via `sudo machinectl shell <user>@.host /bin/bash -c "<command>"`

#### Scenario: Container health query via machinectl (polkit mode)
- **WHEN** the status command queries container state and `machinectl_authentication` is `"polkit"`
- **THEN** it invokes `docker compose ps --format json` via `machinectl shell <user>@.host /bin/bash -c "<command>"` without `sudo` prefix

### Requirement: IPAM Display
The system SHALL display the instance's IPAM allocation including slot index and derived subnets.

#### Scenario: IPAM slot displayed
- **WHEN** status is invoked for a running or stopped instance with an IPAM allocation
- **THEN** the slot index and six derived subnets (isolated, core_proxy, dns, admin, admin_proxy, egress) are displayed

#### Scenario: No IPAM allocation
- **WHEN** status is invoked for an instance with no IPAM ledger entry
- **THEN** the IPAM section displays "No allocation"

### Requirement: Config Completeness Warnings
The system SHALL display warnings when the instance configuration has issues that would prevent a successful `start`.

#### Scenario: Missing secrets detected
- **WHEN** `sandbox.toml` enables a component (e.g., `db_postgres.enabled = true`) but `.sandbox.env` lacks the corresponding secret (e.g., `PG_PASSWORD`)
- **THEN** the warnings section displays "⊘ PG_PASSWORD missing in .sandbox.env"

#### Scenario: No warnings
- **WHEN** all enabled components have their required secrets populated
- **THEN** the warnings section is omitted from the output

### Requirement: Static IP Display
The system SHALL display the correct static IPs from `derive_static_ips(base_index)` for each service in the container table.

#### Scenario: Correct IPs for slot 0
- **WHEN** the instance has IPAM slot 0
- **THEN** the table shows coredns at `10.100.2.53` (dns_net), dnsdist at `10.100.0.56` (isolated_net), proxy at `10.100.1.254` (core_proxy_net) / `10.100.4.254` (admin_proxy_net), core at `10.100.0.3` (isolated_net) / `10.100.1.3` (core_proxy_net), admin at `10.100.3.2` (admin_net) / `10.100.4.2` (admin_proxy_net), db-postgres at `10.100.0.54` (isolated_net) / `10.100.3.54` (admin_net)

### Requirement: Status IP Map Structure
The `ip_map` used by `sandbox status` SHALL include entries for all containers with their primary network IPs: `core`, `admin`, `coredns`, `dnsdist`, `proxy`, `db-postgres`, and `mcp-firecrawl`. Legacy key names (`dns-sidecar`, `proxy_ip`, `admin_isolated_ip`) SHALL NOT appear.

#### Scenario: ip_map contains all services
- **WHEN** `sandbox status` constructs the IP display map for a running instance
- **THEN** the map includes keys for `core`, `admin`, `coredns`, `dnsdist`, `proxy`, `db-postgres`, and `mcp-firecrawl` with IPs from `derive_static_ips(base_index)`

#### Scenario: ip_map uses new key names
- **WHEN** `sandbox status` constructs the IP display map
- **THEN** the map does NOT reference `dns_sidecar_ip`, `proxy_ip`, `admin_isolated_ip`, or `mcp_firecrawl_isolated_ip`
