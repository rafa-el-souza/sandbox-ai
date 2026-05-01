## Purpose

This specification defines the `sandbox init` command, which scaffolds a new sandbox instance for the current project directory — creating the directory tree, configuration files, registry entry, default ACLs, and the `.initialized` sentinel.

## Requirements

### Requirement: Init Command Interface
The system SHALL provide a `sandbox init` command that scaffolds a new sandbox instance for the current project directory. The `--user` flag SHALL be optional — when omitted, the system SHALL read `docker_unprivileged_user` from `sandbox-ai.toml` in the project root. If neither `--user` nor `sandbox-ai.toml` provides a user, init SHALL exit with an error. A new `--machinectl-auth` flag SHALL accept `"sudo"` or `"polkit"` to override the `machinectl_authentication` value from project config, defaulting to `"sudo"` when neither flag nor config is present.

#### Scenario: Init invoked with user parameter
- **WHEN** the operator runs `sandbox init --user sandbox` from a project directory
- **THEN** the system uses `"sandbox"` as the docker unprivileged user regardless of `sandbox-ai.toml` content

#### Scenario: Init invoked without user parameter, project config exists
- **WHEN** the operator runs `sandbox init` and `sandbox-ai.toml` contains `docker_unprivileged_user = "sandbox"`
- **THEN** the system uses `"sandbox"` as the docker unprivileged user sourced from project config

#### Scenario: Init invoked without user parameter, no project config
- **WHEN** the operator runs `sandbox init` without `--user` and `sandbox-ai.toml` does not exist
- **THEN** the CLI exits with an error: "No user specified. Create sandbox-ai.toml with [host].docker_unprivileged_user or pass --user."

#### Scenario: machinectl-auth flag overrides config
- **WHEN** the operator runs `sandbox init --machinectl-auth polkit` and `sandbox-ai.toml` contains `machinectl_authentication = "sudo"`
- **THEN** the system uses `"polkit"` as the authentication mode

#### Scenario: machinectl-auth defaults to sudo
- **WHEN** the operator runs `sandbox init` without `--machinectl-auth` and `sandbox-ai.toml` does not specify `machinectl_authentication`
- **THEN** the system uses `"sudo"` as the authentication mode

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
- **WHEN** `init` is invoked and setfacl is on PATH, filesystem supports ACLs, tooling plane files are present, and .state/ is writable
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
- **WHEN** `sandbox init --dry-run --user sandbox` is invoked
- **THEN** the system prints the instance ID, directory path, generated sandbox.toml content, list of secrets that would be prompted, and ACL commands that would execute, without creating any files or registry entries

#### Scenario: Dry-run exits cleanly
- **WHEN** `sandbox init --dry-run` completes
- **THEN** no files exist in the sandboxes directory and instances.json is unmodified
