## Purpose

This specification defines the `sandbox init` command, which scaffolds a new sandbox instance for the current project directory — creating the directory tree, configuration files, registry entry, default ACLs, and the `.initialized` sentinel.

## Requirements

### Requirement: Init Command Interface
The system SHALL provide a `sandbox init` command that scaffolds a new sandbox instance for the current working directory. The command SHALL read `docker_unprivileged_user` from the canonical per-host config file (`<sandbox_ai_user_home()>/config/sandbox-ai.toml`). If that file does not exist, the command SHALL seed it via interactive prompt in TTY mode or fail with explicit guidance in non-TTY mode (see "Per-User Tree Creation on Init" requirement). A `--machinectl-auth` flag SHALL accept `"sudo"` or `"polkit"` to override the `machinectl_authentication` value from host config; absent the flag and absent a host config value, the default is `"sudo"`. The previously-supported `--user` flag is removed.

#### Scenario: Init reads docker user from host config
- **WHEN** the operator runs `sandbox init` and `<home>/config/sandbox-ai.toml` contains `docker_unprivileged_user = "sandbox"`
- **THEN** the system uses `"sandbox"` as the docker unprivileged user

#### Scenario: machinectl-auth flag overrides config
- **WHEN** the operator runs `sandbox init --machinectl-auth polkit` and `<home>/config/sandbox-ai.toml` contains `machinectl_authentication = "sudo"`
- **THEN** the system uses `"polkit"` as the authentication mode for this invocation

#### Scenario: machinectl-auth defaults to sudo
- **WHEN** the operator runs `sandbox init` without `--machinectl-auth` and `<home>/config/sandbox-ai.toml` does not specify `machinectl_authentication`
- **THEN** the system uses `"sudo"` as the authentication mode

#### Scenario: --user flag is rejected
- **WHEN** the operator runs `sandbox init --user sandbox`
- **THEN** the CLI exits with an "unknown option" error (the flag has been removed; operators set `docker_unprivileged_user` via the canonical host config file or via the first-run seeding prompt)

### Requirement: Init-Time Auth Mode Probe
The system SHALL validate that the resolved machinectl authentication mode works at init time by executing a probe command against the resolved docker unprivileged user. The probe SHALL use a 5-second timeout.

#### Scenario: Sudo mode probe succeeds
- **WHEN** init resolves `machinectl_authentication = "sudo"` and `sudo machinectl shell <user>@.host /bin/bash -c "echo ok"` returns exit code 0 within 5 seconds
- **THEN** init proceeds normally

#### Scenario: Polkit mode probe succeeds
- **WHEN** init resolves `machinectl_authentication = "polkit"` and `machinectl shell <user>@.host /bin/bash -c "echo ok"` returns exit code 0 within 5 seconds
- **THEN** init proceeds normally

#### Scenario: Sudo mode probe fails with timeout
- **WHEN** the sudo probe times out after 5 seconds
- **THEN** init exits with an error including remediation: "Configure passwordless machinectl access in /etc/sudoers.d/"

#### Scenario: Polkit mode probe fails
- **WHEN** the polkit probe returns a non-zero exit code or times out
- **THEN** init exits with an error including remediation: "Configure polkit rules for org.freedesktop.machine1.shell"

### Requirement: Init Doctor Pre-Flight Auth Mode Awareness
The init command SHALL run a doctor pre-flight covering the `Filesystem` and `Repo Integrity` chains (excluding `ancestor_traverse`, since ACLs are granted during `start`, not `init`). Privilege Boundary verification at init time is delegated to the dedicated init-time auth probe (see "Init-Time Auth Mode Probe" requirement). The pre-flight SHALL forward the resolved `machinectl_authentication` mode to `run_check_subset()` / `build_check_registry()` so that, if and when machinectl-dependent checks enter the pre-flight scope, the `sudo` binary check is conditionally omitted under polkit mode.

#### Scenario: Pre-flight forwards auth mode to the check registry
- **WHEN** init resolves `machinectl_authentication = "polkit"` and runs the Filesystem + Repo Integrity pre-flight
- **THEN** `run_check_subset(..., auth_mode=POLKIT)` is invoked so the registry it builds reflects polkit semantics (sudo check omitted, `machinectl_reachable` depends_on without `sudo`)

#### Scenario: Privilege Boundary verification is performed by the init-time probe, not the pre-flight
- **WHEN** init runs in either auth mode
- **THEN** Chain 1 (Privilege Boundary) checks are NOT executed by the pre-flight; the init-time auth probe (5-second timeout) is the single source of truth for machinectl reachability at init time

### Requirement: Init Git Config Auto-Detection
The system SHALL auto-detect `git_user` and `git_email` from the host's `git config --global` during scaffold and write them into `sandbox.toml`. The `--git-user` and `--git-email` flags SHALL override auto-detected values.

#### Scenario: Git config detected
- **WHEN** `init` is invoked and `git config --global user.name` returns a value
- **THEN** `sandbox.toml` is written with `git_user` set to the detected value

#### Scenario: Git not installed
- **WHEN** `init` is invoked and `git` is not on PATH
- **THEN** `sandbox.toml` is written with `git_user = ""` and `git_email = ""`

#### Scenario: Flag overrides auto-detection
- **WHEN** `init` is invoked with `--git-user "Jane Doe"`
- **THEN** `sandbox.toml` is written with `git_user = "Jane Doe"` regardless of `git config` output

### Requirement: Init Doctor Pre-Flight
The system SHALL execute doctor Chain 2 (Filesystem) and Chain 3 (Repo Integrity) checks before beginning scaffold. If any check fails, init SHALL abort with the doctor diagnostic output and exit code 1.

#### Scenario: Pre-flight passes
- **WHEN** `init` is invoked and setfacl is on PATH, filesystem supports ACLs, tooling plane files are present, and `<sandbox_ai_user_home()>/state/` is writable
- **THEN** scaffold proceeds normally

#### Scenario: Pre-flight fails
- **WHEN** `init` is invoked and setfacl is not on PATH
- **THEN** init aborts with the doctor failure output including remediation guidance, before creating any files or directories

### Requirement: Init Re-Init Guard
The system SHALL reject init for a project directory that already has a registered instance.

#### Scenario: Re-init rejected
- **WHEN** `init` is invoked and the project directory already has an entry in `instances.json`
- **THEN** the CLI exits with "Instance already initialized for this directory. Run `sandbox destroy` first." and exit code 1

### Requirement: Init Non-TTY Mode
The system SHALL skip interactive secret prompting when stdin is not a TTY, completing scaffold without secrets populated.

#### Scenario: Non-TTY init completes
- **WHEN** `init` is invoked in a non-TTY environment (e.g., CI pipeline)
- **THEN** scaffold completes through S7 (sentinel written), `.sandbox.env` contains empty secret values, and no RuntimeError is raised

#### Scenario: Non-TTY guidance printed
- **WHEN** `init` completes in non-TTY mode
- **THEN** the CLI prints the path to `.sandbox.env` with instructions to populate secrets before running `sandbox start`

### Requirement: Init Dry-Run Preview
The system SHALL support `sandbox init --dry-run` that previews the scaffold output without writing any state.

#### Scenario: Dry-run previews config
- **WHEN** `sandbox init --dry-run` is invoked
- **THEN** the system prints the instance ID, directory path, generated sandbox.toml content, list of secrets that would be prompted, and ACL commands that would execute, without creating any files or registry entries

#### Scenario: Dry-run exits cleanly
- **WHEN** `sandbox init --dry-run` completes
- **THEN** no files exist in the sandboxes directory and instances.json is unmodified

### Requirement: Per-User Tree Creation on Init
The system SHALL create the per-user tree (`<home>/`, `<home>/config/`, `<home>/state/`) during `sandbox init` if any of the directories is missing, with mode `0700`. The creation SHALL be idempotent — re-running `sandbox init` against an existing tree SHALL NOT raise an error and SHALL NOT modify existing directory modes. The host config file (`<home>/config/sandbox-ai.toml`) SHALL be created if it does not exist; it SHALL NOT be overwritten if it already exists. After init completes, `<home>/state/instances.json` SHALL exist (created empty if not present) so that subsequent lifecycle commands recognize the host as initialized.

#### Scenario: Init on clean host creates the tree
- **WHEN** `sandbox init` is invoked and `<home>/` does not exist
- **THEN** init creates `<home>/`, `<home>/config/`, and `<home>/state/` with mode `0700`, writes a default `<home>/config/sandbox-ai.toml`, and writes an empty `<home>/state/instances.json`

#### Scenario: Init on partially-initialized host is idempotent
- **WHEN** `sandbox init` is invoked and `<home>/config/` exists but `<home>/state/` is missing
- **THEN** init creates `<home>/state/` with mode `0700` and leaves `<home>/config/` (and its contents) untouched

#### Scenario: Init does not overwrite existing host config
- **WHEN** `sandbox init` is invoked and `<home>/config/sandbox-ai.toml` already exists with custom values
- **THEN** init does not overwrite the file; the operator's customizations are preserved

#### Scenario: Init does not overwrite existing registry
- **WHEN** `sandbox init` is invoked and `<home>/state/instances.json` already contains entries
- **THEN** init does not overwrite the file; existing instance registrations are preserved

#### Scenario: SANDBOX_AI_USER_HOME redirects creation
- **WHEN** `sandbox init` is invoked with `SANDBOX_AI_USER_HOME=/tmp/test-home` set
- **THEN** the per-user tree is created under `/tmp/test-home/` rather than `~/.sandbox-ai/`

### Requirement: Host Config Seeding (TTY Prompt or Non-TTY Fail)
When `<home>/config/sandbox-ai.toml` does not exist, `sandbox init` SHALL seed it. In TTY mode, the system SHALL prompt the operator interactively for `docker_unprivileged_user` (required) and `machinectl_authentication` (optional, default `"sudo"`). In non-TTY mode, the system SHALL exit with a clear error directing the operator to create the file manually before retrying.

#### Scenario: TTY clean install — interactive seed
- **WHEN** `sandbox init` runs in a TTY and `<home>/config/sandbox-ai.toml` does not exist
- **THEN** the CLI prompts: "docker_unprivileged_user (e.g., sandbox):" — accepts a non-empty value — then prompts: "machinectl_authentication [sudo/polkit, default sudo]:" — accepts `sudo`, `polkit`, or empty (defaulting to `sudo`) — then writes the seeded values to `<home>/config/sandbox-ai.toml`

#### Scenario: TTY clean install — empty user rejected
- **WHEN** the operator presses Enter without typing a value at the `docker_unprivileged_user` prompt
- **THEN** the CLI re-prompts (empty values are not accepted; the field is required)

#### Scenario: Non-TTY clean install — fail with guidance
- **WHEN** `sandbox init` runs in a non-TTY environment (e.g., CI pipeline) and `<home>/config/sandbox-ai.toml` does not exist
- **THEN** the CLI exits with: "Cannot prompt for `docker_unprivileged_user` in non-interactive mode. Create `<resolved-home>/config/sandbox-ai.toml` with a `[host]` section containing `docker_unprivileged_user` before running `sandbox init`." and exit code 1

#### Scenario: Existing host config is not re-seeded
- **WHEN** `sandbox init` runs and `<home>/config/sandbox-ai.toml` already exists with valid content
- **THEN** the system does NOT prompt and does NOT overwrite; init proceeds using the existing values

### Requirement: Init Detects Legacy CWD-Local Files
During `sandbox init`, the system SHALL inspect the current working directory for legacy `<cwd>/sandbox-ai.toml` and `<cwd>/.state/` and warn the operator that these files are no longer used.

#### Scenario: Legacy host config detected
- **WHEN** `sandbox init` runs and `<cwd>/sandbox-ai.toml` exists
- **THEN** init prints a warning: "Found legacy `<cwd>/sandbox-ai.toml`. Per-host config now lives at `<resolved-home>/config/sandbox-ai.toml`. Migrate manually or delete the legacy file."

#### Scenario: Legacy state directory detected
- **WHEN** `sandbox init` runs and `<cwd>/.state/` exists
- **THEN** init prints a warning: "Found legacy `<cwd>/.state/`. Orchestrator state now lives at `<resolved-home>/state/`. Migrate manually or delete the legacy directory."
