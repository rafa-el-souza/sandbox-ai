## Purpose

This specification defines the `sandbox init` command, which scaffolds a new sandbox instance for the current project directory — creating the directory tree, configuration files, registry entry, default ACLs, and the `.initialized` sentinel.

## Requirements

### Requirement: Init Command Interface
The system SHALL provide a `sandbox init` command that scaffolds a new sandbox instance for the current project directory. The `--user` flag SHALL be mandatory with no default value.

#### Scenario: Init invoked with user parameter
- **WHEN** the operator runs `sandbox init --user sandbox` from a project directory
- **THEN** the system executes the full scaffold sub-sequence (S1-S7), creating the instance directory tree, sandbox.toml, .sandbox.env, default ACLs, registry entry, secret prompts, and .initialized sentinel

#### Scenario: Init invoked without user parameter
- **WHEN** the operator runs `sandbox init` without `--user`
- **THEN** the CLI exits with an error indicating that `--user` is required

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
