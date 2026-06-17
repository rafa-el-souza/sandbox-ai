# per-user-state-layout

## Purpose

Defines the canonical `~/.sandbox-ai/` directory layout that holds the user's
orchestrator-owned config and state — independent of the working directory
the user invokes `sandbox` from. Owns the resolution helper
(`sandbox_ai_user_home()`), the `SANDBOX_AI_USER_HOME` test-isolation
override, the mode-`0700` enforcement on `config/` and `state/`, and the
creation lifecycle (created exclusively by `sandbox init`, required by all
other lifecycle commands). Does not own the contents of those directories;
that's covered by `host-config` (config file) and `instance-registry`
(state files).

## Requirements

### Requirement: instances and workspaces Subtrees in Per-User Home

The per-user home tree SHALL include two additional subdirectories beyond `config/` and `state/`: `instances/` (containing per-instance dirs `<inst>/`) and `workspaces/` (containing per-instance workspace parents `<inst>/<ws>/` plus the lazy `_backups/<inst>/<ws>/<ts>/` subtree). All four top-level subdirectories (`config/`, `state/`, `instances/`, `workspaces/`) SHALL have mode `0700` dev:dev. The home root itself SHALL have mode `0700` dev:dev.

`instances/` and `workspaces/` SHALL be created during `sandbox init` via the shared `ensure_per_user_state()` helper. `_backups/` is lazily created on first backup invocation.

#### Scenario: instances and workspaces subtrees created
- **WHEN** `sandbox init` runs on a clean host
- **THEN** `~/.sandbox-ai/instances/` and `~/.sandbox-ai/workspaces/` exist with mode `0700` dev:dev (alongside the existing `config/` and `state/`)

#### Scenario: _backups created lazily
- **WHEN** the first backup operation runs on a host
- **THEN** `~/.sandbox-ai/workspaces/_backups/` is created with mode `0700` dev:dev; absent on hosts where no backup has yet occurred

### Requirement: ensure_per_user_state Idempotent Helper

The system SHALL provide a `core.host_config.ensure_per_user_state()` (or sibling module) helper that idempotently creates the per-user home tree (`<home>/`, `<home>/config/`, `<home>/state/`, `<home>/instances/`, `<home>/workspaces/`) with mode `0700` dev:dev. The helper is callable by `sandbox init` (current consumer) and a future `sandbox setup` command without ordering or duplication concerns.

The helper SHALL:
- Create missing directories with `os.makedirs(path, mode=0o700, exist_ok=True)`.
- Leave existing directories untouched (mode is preserved as-is, even if more permissive than `0700` — see `cli-doctor`'s mode-drift check).
- Return without error when all directories already exist.

#### Scenario: Helper creates missing tree
- **WHEN** `ensure_per_user_state()` is called on a host with no `~/.sandbox-ai/`
- **THEN** the home root and all four subdirectories are created with mode `0700` dev:dev

#### Scenario: Helper idempotent on existing tree
- **WHEN** `ensure_per_user_state()` is called on a host where the tree already exists
- **THEN** no error is raised; existing directory modes are not modified

#### Scenario: Helper creates missing subdirectory only
- **WHEN** `ensure_per_user_state()` is called and `<home>/instances/` is missing but other subdirs exist
- **THEN** only `<home>/instances/` is created; other subdirs are untouched

### Requirement: Per-User Home Root Path

The system SHALL resolve the per-user orchestrator home root via a single helper `sandbox_ai_home()` (renamed from `sandbox_ai_user_home()`) that returns the value of the `SANDBOX_AI_HOME` environment variable if set, otherwise `os.path.expanduser("~/.sandbox-ai")`. All loaders, the CLI, and the doctor SHALL call this helper rather than hardcoding `~/.sandbox-ai`.

#### Scenario: Default resolution
- **WHEN** `SANDBOX_AI_HOME` is unset and `os.path.expanduser("~/.sandbox-ai")` returns `/home/alice/.sandbox-ai`
- **THEN** `sandbox_ai_home()` returns `/home/alice/.sandbox-ai`

#### Scenario: Env override
- **WHEN** `SANDBOX_AI_HOME=/tmp/test-home` is set in the process environment
- **THEN** `sandbox_ai_home()` returns `/tmp/test-home` regardless of `~`

#### Scenario: Helper used by all consumers
- **WHEN** any code reads or writes per-user state
- **THEN** the path is derived from `sandbox_ai_home()`, not hardcoded; the legacy `sandbox_ai_user_home()` function name is removed

#### Scenario: SANDBOX_AI_USER_HOME no longer honored
- **WHEN** `SANDBOX_AI_USER_HOME` is set in the environment but `SANDBOX_AI_HOME` is not
- **THEN** `sandbox_ai_home()` ignores `SANDBOX_AI_USER_HOME` and returns the default `~/.sandbox-ai` (the rename is atomic; the old name is not honored as a fallback)

### Requirement: Per-User Subtree Layout

The per-user home SHALL contain four subdirectories: `config/` (reserved for host config files; host provisioning facts are setup-determined and no longer written here), `state/` (for `instances.json`, `ipam.json`, `state.lock`, `ipam.json.lock`, `instances.json.lock`, and per-instance backup locks `<inst>.backup.lock`), `instances/` (per-instance dirs), and `workspaces/` (per-instance workspace parents plus `_backups/`). All four SHALL have mode `0700`. The home root itself SHALL have mode `0700`.

The `ipam.json.lock` file is owned by the IPAM ledger (per `orchestrator-networking`'s "IPAM Ledger Lock File" requirement) and is distinct from the per-user `state.lock`. The `instances.json.lock` file is owned by the instance registry (per `instance-registry`'s "Registry Lock Safety" requirement) and is also distinct from `state.lock`. Both resource locks are siblings of each other; the lock-acquisition ordering rule (state outer, resource locks inner and never nesting with each other) is normative and documented in `instance-registry`'s "Registry Lock Safety" requirement.

#### Scenario: Subdirectories present after init

- **WHEN** `sandbox init` completes for the first time on a clean host
- **THEN** `<home>/config/`, `<home>/state/`, `<home>/instances/`, and `<home>/workspaces/` exist with mode `0700`, and the home root itself has mode `0700`

#### Scenario: Subdirectory contents

- **WHEN** the per-user tree is populated after `sandbox init <inst>`
- **THEN** `<home>/state/instances.json` exists; `<home>/instances/<inst>/` exists with the scaffolded instance contents; `<home>/workspaces/<inst>/<ws>/` exists for each workspace. `<home>/state/ipam.json`, `<home>/state/ipam.json.lock`, `<home>/state/instances.json.lock`, `<home>/state/state.lock`, `<home>/state/<inst>.backup.lock` (lazy), and `<home>/workspaces/_backups/` (lazy) are created on first need.

#### Scenario: ipam.json.lock created lazily on first IPAM acquisition

- **WHEN** the per-user tree exists from a prior `sandbox init` but no `sandbox start` has yet been run, and `sandbox start <inst>` is invoked
- **THEN** `<home>/state/ipam.json.lock` is created on first IPAM acquisition by `IPAMLedger.allocate`, distinct from `<home>/state/state.lock`; existing `<home>/state/ipam.json` ledgers from the redesign require no migration

#### Scenario: instances.json.lock created lazily on first registry mutation

- **WHEN** the per-user tree exists and registry mutation is invoked (e.g., `sandbox init` reaching `InstanceRegistry.register`, or `sandbox destroy` reaching `InstanceRegistry.remove`)
- **THEN** `<home>/state/instances.json.lock` is created on first acquisition by `InstanceRegistry`, distinct from both `<home>/state/state.lock` and `<home>/state/ipam.json.lock`; existing `<home>/state/instances.json` ledgers from the redesign require no migration

### Requirement: Per-User Tree Creation Lifecycle

The system SHALL create the per-user tree exclusively during `sandbox init` (until `sandbox setup` lands as a separate change; both will share the `ensure_per_user_state()` helper). Other commands (`start`, `stop`, `destroy`, `status`, `attach`, all `workspace ...` subcommands) SHALL NOT create any part of the tree.

#### Scenario: Init creates missing tree
- **WHEN** `sandbox init <inst>` runs and any of `<home>/`, `<home>/config/`, `<home>/state/`, `<home>/instances/`, or `<home>/workspaces/` is missing
- **THEN** init creates each missing directory with mode `0700` via `ensure_per_user_state()`

#### Scenario: Init is idempotent on existing tree
- **WHEN** `sandbox init <inst>` runs and the tree already exists
- **THEN** init does not raise and does not modify the existing directory modes

#### Scenario: Non-init command does not create the tree
- **WHEN** `sandbox start`, `stop`, `destroy`, `status`, `attach`, or any `workspace ...` subcommand is invoked and the tree is missing
- **THEN** the command exits with the per-user-state-not-initialized error and does NOT create the tree

### Requirement: Initialization Required for Lifecycle Commands

The system SHALL refuse to perform lifecycle operations (`start`, `stop`, `destroy`, `status`, `attach`, all `workspace ...` subcommands) when the per-user tree is not initialized. The signal of "not initialized" SHALL be the absence of `<home>/state/instances.json`. The error message SHALL identify the resolved home path and direct the user to run `sandbox init`.

#### Scenario: Lifecycle command on uninitialized host
- **WHEN** any of the lifecycle commands is invoked and `<home>/state/instances.json` does not exist
- **THEN** the CLI exits with: "Error: per-user state not initialized at `<resolved-home>`. Run `sandbox init <inst>` first." and exit code 1

#### Scenario: Resolved home path included in error
- **WHEN** the error above is emitted with `SANDBOX_AI_HOME=/tmp/test` set
- **THEN** the error message contains `/tmp/test` (not `~/.sandbox-ai`), so the user can see exactly which path was checked

### Requirement: Mode Drift Detection in Doctor

The `sandbox doctor` command SHALL inspect `<home>/`, `<home>/config/`, and `<home>/state/` and warn if any has a mode more permissive than `0700`. The doctor SHALL NOT auto-fix; the warning SHALL include the offending path, the actual mode, and the expected mode.

#### Scenario: Tree mode is correct
- **WHEN** `sandbox doctor` runs and all three directories have mode `0700`
- **THEN** the per-user-tree-mode check passes

#### Scenario: Tree mode drift
- **WHEN** `sandbox doctor` runs and `<home>/state/` has mode `0755`
- **THEN** the per-user-tree-mode check warns: "WARNING: <home>/state/ has mode 0755; expected 0700. Run `chmod 0700 <home>/state/` to remediate."

#### Scenario: Tree absent
- **WHEN** `sandbox doctor` runs and `<home>/` does not exist
- **THEN** the per-user-tree-mode check reports the tree as missing and directs the user to run `sandbox init` (does NOT report a mode-drift warning for a non-existent path)

