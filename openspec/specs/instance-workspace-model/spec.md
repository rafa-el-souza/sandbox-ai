# instance-workspace-model Specification

## Purpose

This capability defines the conceptual separation between **instances** and **workspaces**, and the validation rules, schema, bootstrap modes, filesystem layout, and ancestor-walker safety rules that govern workspaces. An instance owns a compose stack, registry entry, IPAM allocation, and a single agent set. A workspace is a user code directory bind-mounted into the instance's containers; an instance has one or more workspaces.

## Requirements

### Requirement: Instance and Workspace Concept Separation

The system SHALL distinguish between **instances** (sandbox units owning a compose stack, registry entry, instance dirs at `~/.sandbox-ai/instances/<inst>/`, IPAM allocation, and a single agent set) and **workspaces** (user code dirs, one or more per instance, bind-mounted at `/workspaces/<ws-name>` inside the container). One agent set per instance; multi-workspace fans out only the bind mounts and per-workspace shared-group recipe state.

#### Scenario: Instance has zero workspaces is rejected
- **WHEN** any operation would leave an instance with zero workspaces
- **THEN** the operation is rejected with guidance to add or restore a workspace

#### Scenario: Instance and workspace lifecycles are independent
- **WHEN** a workspace is added, removed, renamed, or restored
- **THEN** the instance's compose stack, registry entry, IPAM allocation, and agent set are unaffected (though the instance must be stopped for these operations per MVP gate)

### Requirement: Workspace Name Validation

Workspace names SHALL match the regex `[a-z0-9_-]+`, MUST NOT start with `-` or `_`, MUST be ≤32 characters, MUST be non-empty, and MUST NOT match any reserved name in the set `{_backups, default, all, none, system}` plus the seven subnet names (`isolated`, `core_proxy`, `dns`, `admin`, `admin_proxy`, `egress`, `ipc`). Workspace names SHALL be unique within their containing instance.

#### Scenario: Valid workspace name accepted
- **WHEN** a workspace name `main`, `backend-api`, or `scratch_2` is supplied
- **THEN** the name passes validation

#### Scenario: Reserved name rejected
- **WHEN** a workspace name `_backups`, `default`, `all`, or `isolated` is supplied
- **THEN** the operation rejects with a "reserved name" error

#### Scenario: Leading underscore rejected
- **WHEN** a workspace name starts with `_` (e.g., `_scratch`)
- **THEN** the operation rejects (leading `_` reserved for orchestrator-internal names)

#### Scenario: Leading hyphen rejected
- **WHEN** a workspace name starts with `-`
- **THEN** the operation rejects (argv-hazard avoidance)

#### Scenario: Length cap enforced
- **WHEN** a workspace name exceeding 32 characters is supplied
- **THEN** the operation rejects with a length-cap error

#### Scenario: Duplicate within instance rejected
- **WHEN** a workspace name already present in the instance's `[workspaces]` is supplied for `add` or `restore`
- **THEN** the operation rejects with a "name already exists" error

### Requirement: Instance Name Validation

Instance names SHALL match the same regex/character/leading-character/reserved rules as workspace names but with a length cap of **30 characters** (tightened from 32 to keep headroom under docker's 64-char container-name cap given the `<dev>-<inst>-<service>-<idx>` compose-project prefixing per the `instance-registry` capability). Instance names SHALL be globally unique per-user.

#### Scenario: Instance name length cap is 30
- **WHEN** an instance name exceeding 30 characters is supplied to `sandbox init`
- **THEN** the operation rejects with a length-cap error

#### Scenario: Instance name globally unique per-user
- **WHEN** `sandbox init <inst>` is invoked and `<inst>` already exists in `~/.sandbox-ai/state/instances.json`
- **THEN** init rejects with the re-init guard (consistent with the `cli-init` capability's "Re-Init Guard" requirement)

### Requirement: Workspace Schema in sandbox.toml

The per-instance `sandbox.toml` SHALL contain a `[workspaces]` map-of-tables. Each entry SHALL have keys: `bootstrap_mode` (string, one of `"copy"` or `"empty"`), `source` (string, present when `bootstrap_mode == "copy"`, absent or null when `bootstrap_mode == "empty"`), and `path` (string, always present, the absolute path of the workspace tree on disk).

#### Scenario: Valid workspaces section parses
- **WHEN** `sandbox.toml` contains `[workspaces.main]` with `bootstrap_mode = "copy"`, `source = "/path/to/src"`, `path = "/home/dev/.sandbox-ai/workspaces/myinst/main"`
- **THEN** the Pydantic model parses without error and exposes a `WorkspaceConfig` with those values

#### Scenario: Empty bootstrap mode without source is valid
- **WHEN** `sandbox.toml` contains `[workspaces.scratch]` with `bootstrap_mode = "empty"` and no `source` key, with `path` present
- **THEN** the Pydantic model parses without error; `source` is `None`

#### Scenario: Copy bootstrap mode without source is rejected
- **WHEN** `sandbox.toml` contains `[workspaces.main]` with `bootstrap_mode = "copy"` and no `source`
- **THEN** Pydantic validation raises a structured error before any lifecycle operation

#### Scenario: Path field always required
- **WHEN** `sandbox.toml` contains a `[workspaces.<ws>]` entry without a `path` field
- **THEN** Pydantic validation raises a structured error

#### Scenario: Workspace name uniqueness enforced by TOML parser
- **WHEN** `sandbox.toml` contains two sections with the same `[workspaces.<name>]` header
- **THEN** TOML parsing fails before Pydantic validation runs

### Requirement: Bootstrap Modes copy and empty

The system SHALL support two bootstrap modes for workspace creation: `copy` (workspace tree seeded by rsync from a host path) and `empty` (workspace tree created empty for in-container population, e.g., agent-side `git clone`). The `clone` mode is NOT supported; operators clone host-side then `--copy`, or `--empty` and clone from inside the container after `sandbox start`.

#### Scenario: Copy mode requires source path
- **WHEN** the operator invokes a command with `--copy NAME=PATH` or with `bootstrap_mode = "copy"` in sandbox.toml
- **THEN** the system validates the source path exists, is readable by dev, and is not in the walker boundary list

#### Scenario: Empty mode creates bare workspace tree
- **WHEN** the operator invokes a command with `--empty NAME` or with `bootstrap_mode = "empty"` in sandbox.toml
- **THEN** the system creates `~/.sandbox-ai/workspaces/<inst>/<ws>/` mode 0700 dev:dev with no contents

#### Scenario: Clone mode is not supported
- **WHEN** the operator invokes any command attempting to specify a `clone` bootstrap mode
- **THEN** the operation rejects with guidance to use `--copy` (host-side clone first) or `--empty` (clone from inside the container)

### Requirement: Workspace Tree Location

Workspace trees SHALL live under `~/.sandbox-ai/workspaces/<inst>/<ws>/`. The `~/.sandbox-ai/workspaces/` parent SHALL be mode `0700` dev:dev, lazily created on first workspace scaffold. The `<inst>/` parent SHALL be mode `0700` dev:dev, created when the instance's first workspace is scaffolded. The `<ws>/` directory SHALL be mode `0700` dev:dev at scaffold time; the `compose-security-baseline` capability's shared-group recipe transitions it to mode `2770` `<bridge-group>` on first start.

#### Scenario: Workspace parent created lazily
- **WHEN** the first workspace is scaffolded under any instance
- **THEN** `~/.sandbox-ai/workspaces/` exists with mode `0700` dev:dev (or is created with that mode if absent)

#### Scenario: Per-instance workspace parent created lazily
- **WHEN** the first workspace under instance `<inst>` is scaffolded
- **THEN** `~/.sandbox-ai/workspaces/<inst>/` exists with mode `0700` dev:dev

#### Scenario: Workspace dir mode at scaffold
- **WHEN** `~/.sandbox-ai/workspaces/<inst>/<ws>/` is freshly scaffolded
- **THEN** the directory has mode `0700` and ownership dev:dev

### Requirement: Walker Safety Rules

The ACL ancestor walker SHALL be a planning-only pure function (no side effects) that emits `(path, acl-operation)` tuples for execution by a separate layer. The walker SHALL apply seven safety rules:

1. **Resolve realpath first**: Targets are resolved via `os.path.realpath` before walking. Symlinks in any path component cause explicit failure.
2. **Boundary stop list**: Never emit ACL grants for paths in `{/, /etc, /usr, /var, /tmp, /proc, /sys, /dev, /boot, /run, /home, /root}` or for the invoking user's home directory itself (`~/`). Walks that would need to grant on these surface as a doctor failure with operator-resolvable remediation; the walker does NOT silently apply the ACL.
3. **Reject targets in the boundary list at init time**: A workspace whose resolved path matches the boundary list is rejected at `init` (or `workspace add`/`restore`), not at walk time.
4. **Bound walk depth**: Walks exceeding 64 path components fail explicitly.
5. **lstat throughout**: All path-component inspection uses `lstat` (no symlink dereference).
6. **Fault-isolated grant at execution**: Grants are applied independently, with failures collected as warnings (per the `orchestrator-volumes` capability's existing fault-isolation requirement).
7. **Per-target walk**: Each workspace target gets its own walk; results are deduplicated at execution rather than during planning.

#### Scenario: Target with symlink in chain rejected
- **WHEN** a workspace path contains a symlink in any of its ancestor components
- **THEN** the walker rejects with a "symlink in ancestor chain" error

#### Scenario: Walker does not grant on boundary path
- **WHEN** the ancestor chain of a workspace path includes `~/`, `/home`, or any other boundary-list entry
- **THEN** the walker emits NO ACL grant for that boundary path; the walk surfaces the requirement to a doctor check rather than silently mutating the path

#### Scenario: Workspace at boundary path rejected at init
- **WHEN** a workspace's resolved path is `/etc`, `/`, or another boundary-list entry
- **THEN** the operation rejects at the `init`/`add`/`restore` gate stage

#### Scenario: Walk exceeding depth bound fails
- **WHEN** a workspace path has more than 64 ancestor components
- **THEN** the walker rejects with a "depth bound exceeded" error

#### Scenario: lstat used throughout walk
- **WHEN** the walker inspects path components
- **THEN** every component check uses `lstat` (not `stat`); symlink targets are never dereferenced during planning

#### Scenario: Walker is per-target with execution-side dedup
- **WHEN** multiple workspaces share an ancestor chain (typical: all under `~/.sandbox-ai/workspaces/<inst>/`)
- **THEN** the walker emits per-workspace plans; the execution layer deduplicates so each ACL operation runs at most once per unique path

### Requirement: Future --existing Bootstrap Mode Door-Keeping

The implementation SHALL persist the `path` field in every `WorkspaceConfig` even when redundantly derivable (current `copy` and `empty` modes always produce `~/.sandbox-ai/workspaces/<inst>/<ws>/`). Every consumer (compose template, ACL plan, ancestor walker, registry display, doctor checks) SHALL read `workspace.path` rather than re-deriving from instance/workspace names. The implementation SHALL provide a destroy-time helper `is_orchestrator_owned(workspace.path) -> bool` returning whether the path is under `~/.sandbox-ai/workspaces/<inst>/`. Today the helper returns `True` always; the false branch is a one-line skip.

This requirement codifies the "doors-open" anchors for a future `--existing <path>` bootstrap mode without writing today-unused code.

#### Scenario: path field always present
- **WHEN** a workspace is scaffolded via `--copy` or `--empty`
- **THEN** the resulting `[workspaces.<ws>].path` in sandbox.toml is the resolved absolute path (today: under `~/.sandbox-ai/workspaces/<inst>/<ws>/`)

#### Scenario: Consumers read path field
- **WHEN** the compose template, ACL plan, ancestor walker, or doctor checks need a workspace's location
- **THEN** they read `workspace.path` (no re-derivation from instance/workspace names)

#### Scenario: Destroy gate uses is_orchestrator_owned
- **WHEN** `sandbox destroy` processes each workspace's cleanup
- **THEN** the destroy gate calls `is_orchestrator_owned(workspace.path)`; under current bootstrap modes the gate returns `True` and the standard rmtree path runs

### Requirement: Workspace Bind Mount Layout

Each workspace listed in `sandbox.toml [workspaces]` SHALL be bind-mounted into the agent containers at `/workspaces/<ws-name>:rw`. The compose template's bind-mount section SHALL iterate the `[workspaces]` map sorted by workspace name (lexicographic) for render determinism. The agent's working directory inside the container SHALL be `/workspaces/<ws>` for the workspace selected at attach time; this is set via `docker exec -w` per the `cli-attach` capability, NOT via Dockerfile `WORKDIR`. The legacy `/workspace` mount point and the `instance.user_project_root` field SHALL NOT be present.

#### Scenario: One bind mount per workspace
- **WHEN** the compose template is rendered with N workspaces
- **THEN** the rendered `compose.yml` contains exactly N volume entries of the form `<workspace.path>:/workspaces/<ws-name>:rw` on each agent service

#### Scenario: Bind mounts sorted by name for determinism
- **WHEN** the compose template is rendered twice with the same `sandbox.toml`
- **THEN** the resulting volume entries appear in identical order (sorted by workspace name lexicographically)

#### Scenario: Runtime cwd set at attach time, not image-build time
- **WHEN** an operator runs `sandbox attach <inst> <ws>`
- **THEN** the in-container cwd is `/workspaces/<ws>` (set via `docker exec -w` per `cli-attach`); the Dockerfile WORKDIR is NOT relied on for this guarantee

#### Scenario: No /workspace mount
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** there is NO bind mount of source `<anything>` to target `/workspace`

#### Scenario: No user_project_root in schema
- **WHEN** `sandbox.toml` is parsed by the Pydantic model
- **THEN** `instance.user_project_root` is NOT present (the field is removed from `SandboxInstanceSection`)
