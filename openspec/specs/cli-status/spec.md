## Purpose

This specification defines the `sandbox status` command, which displays the current state of the sandbox instance for the current project directory — including container health, IPAM allocation, enabled components, and configuration completeness warnings.
## Requirements
### Requirement: Workspaces Section in Status Panel

`sandbox status [<inst>]` SHALL display a Workspaces section in the Rich panel listing each workspace's name, bootstrap mode, path, and state. State values:

- `● ok`: workspace path exists and bridge-group state is correct (setgid bit + group ownership match `workspace_bridge_gid(host)`).
- `⚠ drift`: workspace path exists but bridge-group state is inconsistent (next start triggers recipe re-run via change-4 drift detection).
- `✗ missing`: `sandbox.toml` lists the workspace but `workspace.path` does not exist on disk.

Size column SHALL be opt-in via `sandbox status <inst> --detailed` (`du -sh` per workspace can be slow on large trees). Default output omits the size column.

#### Scenario: Workspaces section displayed
- **WHEN** `sandbox status foo` is invoked for an instance with workspaces `main` and `scratch`
- **THEN** the panel includes a Workspaces section with rows for `main` and `scratch`, each showing name, mode, path, and state

#### Scenario: Drift state displayed
- **WHEN** `sandbox status foo` runs and a workspace's path lacks the setgid bit (drift)
- **THEN** the workspace's State column shows `⚠ drift`

#### Scenario: Missing state displayed
- **WHEN** `sandbox status foo` runs and a workspace's `path` does not exist on disk
- **THEN** the workspace's State column shows `✗ missing`

#### Scenario: Size column opt-in via --detailed
- **WHEN** `sandbox status foo --detailed` is invoked
- **THEN** the Workspaces section includes a Size column populated via `du -sh` per workspace

#### Scenario: Size column omitted by default
- **WHEN** `sandbox status foo` is invoked without `--detailed`
- **THEN** the Workspaces section does NOT include a Size column

### Requirement: Status Command Interface
The system SHALL provide a `sandbox status [<inst>]` command that displays the current state of one or all sandbox instances. When `<inst>` is omitted, the command displays a summary table of all registered instances. When `<inst>` is supplied, the command displays a detailed Rich Panel for that instance.

#### Scenario: Status of a specific running instance
- **WHEN** `sandbox status <inst>` is invoked and `<inst>`'s containers are running
- **THEN** the system displays a Rich Panel with instance identity (name, dir, user), a Rich Table with per-container health (service name, health status, network IPs), IPAM allocation (slot and subnets), enabled components, and a Workspaces section

#### Scenario: Status of a specific stopped instance
- **WHEN** `sandbox status <inst>` is invoked and `<inst>` exists but containers are not running
- **THEN** the system displays a Rich Panel with instance identity and state "stopped", without querying container health, plus a Workspaces section

#### Scenario: Status of unknown instance
- **WHEN** `sandbox status <inst>` is invoked and `<inst>` is not in the registry
- **THEN** the system prints "No sandbox instance named '<inst>'." and exits with code 1

#### Scenario: Status with no argument displays all instances
- **WHEN** `sandbox status` is invoked without an `<inst>` argument
- **THEN** the system displays a summary table of all entries in `instances.json`, each row showing name, state (running/stopped), workspace count, and IPAM slot

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
- **THEN** the slot index and four derived subnets (isolated, core_proxy, dns, egress) plus `ipc_net` are displayed

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
- **THEN** the table shows coredns at `10.100.2.53` (dns_net), dnsdist at `10.100.0.56` (isolated_net), proxy at `10.100.1.254` (core_proxy_net), core at `10.100.0.3` (isolated_net) / `10.100.1.3` (core_proxy_net), admin at `10.100.4.2` (ipc_net), db-postgres at `10.100.0.54` (isolated_net)

### Requirement: Status IP Map Structure
The `ip_map` used by `sandbox status` SHALL include entries for all containers with their primary network IPs: `core`, `admin`, `coredns`, `dnsdist`, `proxy`, `db-postgres`, and `mcp-firecrawl`. Legacy key names (`dns-sidecar`, `proxy_ip`, `admin_isolated_ip`, `admin_admin_ip`, `admin_proxy_ip`, `coredns_admin_ip`, `dnsdist_admin_ip`, `proxy_admin_ip`, `db_postgres_admin_ip`) SHALL NOT appear.

#### Scenario: ip_map contains all services
- **WHEN** `sandbox status` constructs the IP display map for a running instance
- **THEN** the map includes keys for `core`, `admin`, `coredns`, `dnsdist`, `proxy`, `db-postgres`, and `mcp-firecrawl` with IPs from `derive_static_ips(base_index)`; admin's entry holds a single IP (its `admin_ipc_ip`); coredns holds its `dns_net` and `egress_net` IPs; dnsdist holds its `isolated_net` and `dns_net` IPs; proxy holds its `core_proxy_net` and `egress_net` IPs; db-postgres holds its `isolated_net` IP

#### Scenario: ip_map uses new key names
- **WHEN** `sandbox status` constructs the IP display map
- **THEN** the map does NOT reference `dns_sidecar_ip`, `proxy_ip`, `admin_isolated_ip`, `mcp_firecrawl_isolated_ip`, `admin_admin_ip`, `admin_proxy_ip`, `coredns_admin_ip`, `dnsdist_admin_ip`, `proxy_admin_ip`, or `db_postgres_admin_ip`

### Requirement: Per-User State Initialization Required
The `sandbox status` command SHALL refuse to operate when the per-user state tree is not initialized. Initialization is signaled by the presence of `<sandbox_ai_user_home()>/state/instances.json`. On absence, the command SHALL exit with a clear error directing the operator to run `sandbox init`.

#### Scenario: Status on uninitialized host
- **WHEN** `sandbox status` is invoked and `<home>/state/instances.json` does not exist
- **THEN** the CLI exits with: "Error: per-user state not initialized at `<resolved-home>`. Run `sandbox init` first." and exit code 1

#### Scenario: Resolved home in error message
- **WHEN** the status command above runs with `SANDBOX_AI_USER_HOME=/tmp/test-home` set
- **THEN** the error message contains `/tmp/test-home`

### Requirement: Container-Status Probe Honors Execution Mode

The container-status query that backs `sandbox status` and the lifecycle warm-checks (`_container_status` → `compose-ps`) SHALL honor `docker_execution_mode`. In `operator-rootless` mode it SHALL obtain container status by running `docker compose ps` as a local subprocess (via `core.dispatch.probe(Op.COMPOSE_PS, …)`, which itself routes to the local path), with no `machinectl` crossing. The returned status semantics (running / stopped / not-created) and the non-raising warm-check behavior SHALL be identical across both modes.

#### Scenario: status query runs locally in operator-rootless mode

- **WHEN** `sandbox status <inst>` (or a lifecycle warm-check) queries container status with `docker_execution_mode == operator-rootless`
- **THEN** the `compose-ps` probe runs as a local `docker compose ps` subprocess with no `machinectl` crossing, and yields the same running/stopped/not-created determination it would in `separate-user` mode

#### Scenario: warm-check remains non-raising across modes

- **WHEN** the warm-check probe fails (daemon down, instance absent) in `operator-rootless` mode
- **THEN** it returns a non-running result without raising, identically to `separate-user` mode

