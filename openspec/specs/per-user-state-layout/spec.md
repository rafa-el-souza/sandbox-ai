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

### Requirement: Per-User Home Root Path

The system SHALL resolve the per-user orchestrator home root via a single helper `sandbox_ai_user_home()` that returns the value of the `SANDBOX_AI_USER_HOME` environment variable if set, otherwise `os.path.expanduser("~/.sandbox-ai")`. All loaders, the CLI, and the doctor SHALL call this helper rather than hardcoding `~/.sandbox-ai`.

#### Scenario: Default resolution
- **WHEN** `SANDBOX_AI_USER_HOME` is unset and `os.path.expanduser("~/.sandbox-ai")` returns `/home/alice/.sandbox-ai`
- **THEN** `sandbox_ai_user_home()` returns `/home/alice/.sandbox-ai`

#### Scenario: Env override
- **WHEN** `SANDBOX_AI_USER_HOME=/tmp/test-home` is set in the process environment
- **THEN** `sandbox_ai_user_home()` returns `/tmp/test-home` regardless of `~`

#### Scenario: Helper used by all consumers
- **WHEN** any code reads or writes per-user state
- **THEN** the path is derived from `sandbox_ai_user_home()`, not hardcoded

### Requirement: Per-User Subtree Layout

The per-user home SHALL contain two subdirectories: `config/` (for `sandbox-ai.toml` and other host config files) and `state/` (for `instances.json`, `ipam.json`, `state.lock`). Both subdirectories SHALL have mode `0700`. The home root itself SHALL have mode `0700`.

#### Scenario: Subdirectories present after init
- **WHEN** `sandbox init` completes for the first time on a clean host
- **THEN** `<home>/config/` and `<home>/state/` exist with mode `0700`, and the home root itself has mode `0700`

#### Scenario: Subdirectory contents
- **WHEN** the per-user tree is populated after `sandbox init`
- **THEN** `<home>/config/sandbox-ai.toml` exists (host config file) and `<home>/state/instances.json` exists (registry file). `<home>/state/ipam.json` and `<home>/state/state.lock` are created lazily on first need.

### Requirement: Per-User Tree Creation Lifecycle

The system SHALL create the per-user tree exclusively during `sandbox init`. Other commands (`start`, `stop`, `destroy`, `status`, `attach`, `doctor` excluded) SHALL NOT create any part of the tree.

#### Scenario: Init creates missing tree
- **WHEN** `sandbox init` runs and `<home>/`, `<home>/config/`, or `<home>/state/` is missing
- **THEN** init creates each missing directory with mode `0700` via `os.makedirs(..., mode=0o700, exist_ok=True)`

#### Scenario: Init is idempotent on existing tree
- **WHEN** `sandbox init` runs and the tree already exists
- **THEN** init does not raise and does not modify the existing directory modes (mode is preserved as-is, even if more permissive than `0700` — see "Mode Drift Detection" requirement)

#### Scenario: Non-init command does not create the tree
- **WHEN** `sandbox start`, `stop`, `destroy`, `status`, or `attach` is invoked and the tree is missing
- **THEN** the command exits with the per-user-state-not-initialized error (see "Initialization Required for Lifecycle Commands" requirement) and does NOT create the tree

### Requirement: Initialization Required for Lifecycle Commands

The system SHALL refuse to perform lifecycle operations (`start`, `stop`, `destroy`, `status`, `attach`) when the per-user tree is not initialized. The signal of "not initialized" SHALL be the absence of `<home>/state/instances.json`. The error message SHALL identify the resolved home path and direct the user to run `sandbox init`.

#### Scenario: Lifecycle command on uninitialized host
- **WHEN** `sandbox start` (or `stop`, `destroy`, `status`, `attach`) is invoked and `<home>/state/instances.json` does not exist
- **THEN** the CLI exits with: "Error: per-user state not initialized at `<resolved-home>`. Run `sandbox init` first." and exit code 1

#### Scenario: Resolved home path included in error
- **WHEN** the error above is emitted and `SANDBOX_AI_USER_HOME=/tmp/test` is set
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

### Requirement: SANDBOX_AI_USER_HOME Documented as Test-Only

The `SANDBOX_AI_USER_HOME` environment variable SHALL be documented as a test-isolation override only and SHALL NOT be advertised as a user-facing configuration option. The doctor SHALL display the resolved home path in its output so that an unintentionally-set env var is surfaced to the operator.

#### Scenario: Doctor displays resolved home
- **WHEN** `sandbox doctor` runs in any auth mode
- **THEN** the doctor output includes a line of the form: "Per-user home: `<resolved-path>`" so a misconfigured `SANDBOX_AI_USER_HOME` becomes visible

#### Scenario: No XDG fallback
- **WHEN** `XDG_DATA_HOME` is set but `SANDBOX_AI_USER_HOME` is unset
- **THEN** `sandbox_ai_user_home()` ignores `XDG_DATA_HOME` and returns `os.path.expanduser("~/.sandbox-ai")`. XDG paths are not honored.
