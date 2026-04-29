## Purpose

This specification governs the absolute filesystem boundary constraints separating the Human Host repository from the containerized execution environment. It enforces structural mitigations resolving the rootless SubUID paradox via two-pattern ACL model, establishes topological separation between the immutable tooling plane and mutable per-instance plane, and dictates volume annihilation procedures.

## Requirements

### Requirement: UID Paradox ACL Default Overrides
The system SHALL apply two distinct ACL patterns governing the `dev`/`sandbox` filesystem boundary:
- **Pattern A**: Explicit named ACL granting `sandbox` user read access to rendered compose and config files, read-execute access to the instance root, read access to `.sandbox.env`, and execute-only traverse access to user-owned ancestor directories
- **Pattern B**: Default ACL granting `dev` user access to files created inside bind-mounted directories by container subUIDs

#### Scenario: Pattern A — sandbox reads compose files at start
- **WHEN** `sandbox start` completes hydration and before `docker compose up` is invoked
- **THEN** `setfacl -R -m u:<host_unprivileged_user>:rX` is applied to `sandboxes/<id>/docker/` and `sandboxes/<id>/config/` so the `sandbox` user can read the rendered files

#### Scenario: Pattern A — ancestor traverse at start
- **WHEN** `sandbox start` reaches Phase 5 (ACL grants)
- **THEN** `setfacl -m u:<host_unprivileged_user>:--x` is applied to each user-owned ancestor directory from the instance directory up to the ownership boundary

#### Scenario: Pattern A — sandbox ACL revoked at stop
- **WHEN** `sandbox stop` or `sandbox destroy` executes ACL revocation
- **THEN** `setfacl -x u:<host_unprivileged_user>` is applied to `sandboxes/<id>/docker/`, `sandboxes/<id>/config/`, `sandboxes/<id>/`, and `sandboxes/<id>/.sandbox.env`, using fault-isolated revocation (each target attempted independently, failures reported as warnings)

#### Scenario: Pattern B — dev reads container-created files in log/
- **WHEN** scaffold creates `sandboxes/<id>/log/` for the first time
- **THEN** `setfacl -d -m u:dev:rwx` is applied to `sandboxes/<id>/log/` so any file subsequently created inside that directory by a container subUID automatically inherits `u:dev:rwx`

#### Scenario: Pattern B — dev reads container-created files in cache/
- **WHEN** scaffold creates `sandboxes/<id>/cache/` for the first time
- **THEN** `setfacl -d -m u:dev:rwx` is applied to `sandboxes/<id>/cache/` so container subUID-created files are accessible to `dev`

### Requirement: Ancestor Directory Traverse ACLs
The system SHALL grant execute-only (`--x`) ACLs on user-owned ancestor directories so the sandbox user can traverse from `/` to the instance directory. Ancestor ACLs SHALL NOT be revoked on stop or destroy (grant-only model).

#### Scenario: Ancestor traverse granted at start
- **WHEN** `sandbox start` reaches Phase 5 (ACL grants)
- **THEN** `setfacl -m u:<host_unprivileged_user>:--x` is applied to each directory in the ancestor chain from the instance directory up to (but not including) the first directory not owned by the orchestrator UID or `/`

#### Scenario: Ownership boundary stops ancestor walk
- **WHEN** the ancestor walk encounters a directory owned by a UID other than the current process UID (e.g., root-owned `/home/`)
- **THEN** the walk stops and no ACL is applied to that directory or any of its ancestors

#### Scenario: Root directory excluded
- **WHEN** the ancestor walk reaches `/`
- **THEN** no ACL is applied to `/`

#### Scenario: Ancestor ACLs not revoked on stop
- **WHEN** `sandbox stop` executes ACL revocation
- **THEN** ancestor directory `--x` ACLs are NOT included in the revocation set

#### Scenario: Ancestor ACLs not revoked on destroy
- **WHEN** `sandbox destroy` executes ACL revocation
- **THEN** ancestor directory `--x` ACLs are NOT included in the revocation set

#### Scenario: Concurrent ancestor grants are idempotent
- **WHEN** two instances grant `--x` on the same ancestor directory simultaneously
- **THEN** both `setfacl` calls succeed without error (idempotent — same ACL entry applied twice)

### Requirement: Instance Root Read-Execute ACL
The system SHALL grant read-execute (`r-x`) ACL on the instance root directory so the sandbox user can list its contents for compose file resolution.

#### Scenario: Instance root ACL granted at start
- **WHEN** `sandbox start` reaches Phase 5 (ACL grants)
- **THEN** `setfacl -m u:<host_unprivileged_user>:r-x <instance_dir>` is applied

#### Scenario: Instance root ACL revoked at stop
- **WHEN** `sandbox stop` executes ACL revocation
- **THEN** `setfacl -x u:<host_unprivileged_user> <instance_dir>` is applied

### Requirement: Environment File Read ACL
The system SHALL grant read-only ACL on `.sandbox.env` so the sandbox user's `docker compose` process can parse `env_file:` directives and `--env-file` interpolation.

#### Scenario: .sandbox.env ACL granted at start
- **WHEN** `sandbox start` reaches Phase 5 (ACL grants)
- **THEN** `setfacl -m u:<host_unprivileged_user>:r <instance_dir>/.sandbox.env` is applied

#### Scenario: .sandbox.env ACL revoked at stop
- **WHEN** `sandbox stop` executes ACL revocation
- **THEN** `setfacl -x u:<host_unprivileged_user> <instance_dir>/.sandbox.env` is applied

#### Scenario: Missing .sandbox.env fails explicitly
- **WHEN** `.sandbox.env` does not exist at Phase 5
- **THEN** `setfacl` fails and the error is surfaced as a `SandboxExecutionError` indicating instance corruption (no silent skip)

### Requirement: ACL Grant Plan as Single Source of Truth
The system SHALL define ACL grant targets in a single function consumed by both the execution path and the dry-run preview. The grant plan and revoke plan SHALL be separate functions with intentionally asymmetric target sets.

#### Scenario: Grant plan consumed by execution
- **WHEN** `_phase_acl_grant` executes Phase 5
- **THEN** it iterates over the output of `_acl_grant_plan()` to apply each ACL operation

#### Scenario: Grant plan consumed by dry-run preview
- **WHEN** `_dry_run_pipeline` previews the start sequence
- **THEN** it iterates over the output of `_acl_grant_plan()` to display each ACL command

#### Scenario: Revoke plan excludes ancestors
- **WHEN** `_acl_revoke_plan()` is called
- **THEN** the returned target set includes instance root, docker/, config/, and .sandbox.env but does NOT include ancestor directories

### Requirement: Fault-Isolated ACL Revocation
The system SHALL execute each ACL revocation independently with `check=False`. Failures SHALL be collected and reported as warnings. All targets SHALL be attempted regardless of individual failures.

#### Scenario: Partial revocation failure does not abort cleanup
- **WHEN** one ACL revocation target fails (e.g., file already deleted, ACL already absent)
- **THEN** remaining targets are still attempted and the failure is reported as a warning

#### Scenario: All revocation failures reported
- **WHEN** multiple ACL revocation targets fail
- **THEN** each failure is reported with the target description and stderr content

### Requirement: Topographical File Isolation Boundaries
The system SHALL enforce separation between the immutable tooling plane (`.docker/` and `.config/` hidden directories under `SANDBOX_AI_HOME`) and the mutable per-instance plane (`sandboxes/<id>/`).

#### Scenario: The Immutable Tooling Plane (`.docker/` and `.config/`)
- **WHEN** the orchestrator configures infrastructure for an instance
- **THEN** template sources under `SANDBOX_AI_HOME/.docker/` and `SANDBOX_AI_HOME/.config/` are read-only inputs to the hydration pipeline; they are never written to at runtime

#### Scenario: The Mutable Instance Plane (`sandboxes/<id>/`)
- **WHEN** the hydration pipeline runs
- **THEN** all rendered artifacts are written exclusively under `SANDBOX_AI_HOME/sandboxes/<id>/`, which is owned by `dev` and scoped to the instance

#### Scenario: Shell History Isolation via Directory Mount
- **WHEN** the admin and core containers are started
- **THEN** the bind mounts for shell history are at the **directory** level (`sandboxes/<id>/log/admin/` and `sandboxes/<id>/log/core/`), and the `HISTFILE` environment variable inside each container points to a specific path within that mounted directory

#### Scenario: SSH Credentials in Secrets Directory
- **WHEN** the hydration pipeline runs
- **THEN** SSH keypair files are written exclusively under `SANDBOX_AI_HOME/sandboxes/<id>/secrets/`

### Requirement: Deep VFS Annihilation
The system SHALL support volume removal on explicit operator request, scoped strictly to Docker named volumes owned by the instance.

#### Scenario: The `--clean` Flag Termination Sequence
- **WHEN** the human operator executes `sandbox stop --clean`
- **THEN** the orchestrator executes `docker compose down -v`, removing all named Docker volumes for the instance (e.g., Postgres data), while leaving `sandboxes/<id>/log/` and `sandboxes/<id>/cache/` on the host filesystem intact

#### Scenario: The `destroy` Full Annihilation
- **WHEN** the human operator confirms `sandbox destroy`
- **THEN** `docker compose down -v` removes all named Docker volumes, after which `shutil.rmtree(sandboxes/<id>/)` removes the entire instance directory including logs, cache, and secrets

#### Scenario: admin-ipc_vol absent from volumes
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** there is NO volume definition for `admin-ipc_vol`

#### Scenario: mcp-ipc_vol absent from volumes
- **WHEN** the rendered `compose.yml` and `mcp-firecrawl.yml` are inspected
- **THEN** there is NO volume definition for `mcp-ipc_vol`

#### Scenario: /sock mount absent from all services
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** no service contains a volume mount referencing `/sock`

### Requirement: Custom Config Override Bind Mounts
The rendered `compose.yml` SHALL include read-only bind mounts provisioning the custom config override directories inside each container. Without these mounts, the override hooks in `.bashrc`, `.gitconfig`, `.zshrc`, and `.tmux.conf` would silently no-op.

#### Scenario: Core custom config mount
- **WHEN** the rendered `compose.yml` is inspected for the core service
- **THEN** it contains a volume entry `{{ instance_dir }}/custom/config/core:{{ custom_config_core }}:ro`

#### Scenario: Admin custom config mount
- **WHEN** the rendered `compose.yml` is inspected for the admin service
- **THEN** it contains a volume entry `{{ instance_dir }}/custom/config/admin:{{ custom_config_admin }}:ro`

### Requirement: Tmux Resurrect State Bind Mount
The rendered `compose.yml` SHALL include a read-write bind mount for the tmux resurrect session state directory in the admin container, sourced from the `cache/admin/tmux_resurrect` instance subdirectory. This relocates the plugin state from the agent-writable workspace.

#### Scenario: Tmux resurrect mount in admin service
- **WHEN** the rendered `compose.yml` is inspected for the admin service
- **THEN** it contains a volume entry `{{ instance_dir }}/cache/admin/tmux_resurrect:{{ tmux_resurrect_dir }}:rw`

### Requirement: Container-Namespaced Cache Directory
The `cache/` subtree SHALL follow a container-namespaced convention. The Claude Code cache directory SHALL be at `cache/core/.claude` (not `cache/.claude`).

#### Scenario: Scaffold creates namespaced cache directory
- **WHEN** scaffold creates a new instance via `INSTANCE_SUBDIRS`
- **THEN** `sandboxes/<id>/cache/core/.claude` is created (not `sandboxes/<id>/cache/.claude`)

#### Scenario: Scaffold creates tmux resurrect cache directory
- **WHEN** scaffold creates a new instance via `INSTANCE_SUBDIRS`
- **THEN** `sandboxes/<id>/cache/admin/tmux_resurrect` is created

#### Scenario: Compose mount references namespaced cache path
- **WHEN** the rendered `compose.yml` is inspected for the core service
- **THEN** the `.claude` directory mount references `{{ instance_dir }}/cache/core/.claude` (not `{{ instance_dir }}/cache/.claude`)

### Requirement: Stale Proxy Seed File Removal
The following files in `.config/proxy/` SHALL be deleted from the tooling plane: `allowed_domains.txt`, `trusted_clients.acl`, `.htpasswd`. These files are overridden by programmatic generation in `render_templates()` or by `core/crypto.py` during scaffold. Their presence in the tooling plane is misleading — edits to them have no effect.

#### Scenario: No stale proxy seed files in tooling plane
- **WHEN** the `.config/proxy/` directory in the tooling plane is inspected
- **THEN** it contains only `squid.conf` and `ERR_SANDBOX_403` (the Jinja2 template and the static error page)
