## Purpose

This specification governs the absolute filesystem boundary constraints separating the Human Host repository from the containerized execution environment. It enforces structural mitigations resolving the rootless SubUID paradox via two-pattern ACL model, establishes topological separation between the immutable tooling plane and mutable per-instance plane, and dictates volume annihilation procedures.

## Requirements

### Requirement: UID Paradox ACL Default Overrides
The system SHALL apply two distinct ACL patterns governing the `dev`/`sandbox` filesystem boundary:
- **Pattern A**: Explicit named ACL granting `sandbox` user read access to rendered compose and config files
- **Pattern B**: Default ACL granting `dev` user access to files created inside bind-mounted directories by container subUIDs

#### Scenario: Pattern A — sandbox reads compose files at start
- **WHEN** `sandbox start` completes hydration and before `docker compose up` is invoked
- **THEN** `setfacl -R -m u:<host_unprivileged_user>:rX` is applied to `sandboxes/<id>/docker/` and `sandboxes/<id>/config/` so the `sandbox` user can read the rendered files

#### Scenario: Pattern A — sandbox ACL revoked at stop
- **WHEN** `sandbox stop` or `sandbox destroy` confirms containers are down
- **THEN** `setfacl -R -x u:<host_unprivileged_user>` is applied to `sandboxes/<id>/docker/` and `sandboxes/<id>/config/`, removing the named ACL entries

#### Scenario: Pattern B — dev reads container-created files in log/
- **WHEN** scaffold creates `sandboxes/<id>/log/` for the first time
- **THEN** `setfacl -d -m u:dev:rwx` is applied to `sandboxes/<id>/log/` so any file subsequently created inside that directory by a container subUID automatically inherits `u:dev:rwx`

#### Scenario: Pattern B — dev reads container-created files in cache/
- **WHEN** scaffold creates `sandboxes/<id>/cache/` for the first time
- **THEN** `setfacl -d -m u:dev:rwx` is applied to `sandboxes/<id>/cache/` so container subUID-created files are accessible to `dev`

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

### Requirement: Deep VFS Annihilation
The system SHALL support volume removal on explicit operator request, scoped strictly to Docker named volumes owned by the instance.

#### Scenario: The `--clean` Flag Termination Sequence
- **WHEN** the human operator executes `sandbox stop --clean`
- **THEN** the orchestrator executes `docker compose down -v`, removing all named Docker volumes for the instance (e.g., Postgres data), while leaving `sandboxes/<id>/log/` and `sandboxes/<id>/cache/` on the host filesystem intact

#### Scenario: The `destroy` Full Annihilation
- **WHEN** the human operator confirms `sandbox destroy`
- **THEN** `docker compose down -v` removes all named Docker volumes, after which `shutil.rmtree(sandboxes/<id>/)` removes the entire instance directory including logs and cache

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
