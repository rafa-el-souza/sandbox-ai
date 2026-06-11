## Purpose

This specification defines the `sandbox init` command, which scaffolds a new sandbox instance for the current project directory — creating the directory tree, configuration files, registry entry, default ACLs, and the `.initialized` sentinel.
## Requirements
### Requirement: Default Bootstrap is Empty Workspace Named main

When `sandbox init <inst>` is invoked with no `--copy` and no `--empty` flags, the system SHALL scaffold exactly one workspace named `main` with `bootstrap_mode = "empty"`. This default fires only when both flag lists are empty; supplying any flag (even a single `--empty other`) suppresses the default and the operator gets exactly the workspaces named.

#### Scenario: No flags creates main empty workspace
- **WHEN** `sandbox init foo` is invoked with no workspace flags
- **THEN** the resulting `sandbox.toml` contains `[workspaces.main]` with `bootstrap_mode = "empty"` and no `source` field

#### Scenario: Single --empty other suppresses default
- **WHEN** `sandbox init foo --empty other` is invoked
- **THEN** the resulting `sandbox.toml` contains `[workspaces.other]` and NO `[workspaces.main]`

#### Scenario: Single --copy suppresses default
- **WHEN** `sandbox init foo --copy backend=/path` is invoked
- **THEN** the resulting `sandbox.toml` contains `[workspaces.backend]` only

### Requirement: Multi-Workspace Bootstrap Flags

The `sandbox init` command SHALL accept `--copy NAME=PATH` and `--empty NAME` flags, both with `multiple=True` semantics. These flags MAY be repeated to scaffold multiple workspaces in a single invocation. Names supplied across all flags MUST be unique within the invocation; collisions are rejected before any state mutation.

`--copy NAME=PATH` parsing rules:
- Split on the first `=`. NAME = portion before; PATH = portion after.
- NAME MUST pass workspace-name validation (per `instance-workspace-model`).
- PATH MUST be non-empty.
- A `--copy` value with no `=` is rejected with: "--copy requires NAME=PATH form".

The CLI SHALL also accept `--no-default-excludes` and `--exclude=<glob>` (passthrough to rsync; per `cli-workspace`'s "Copy Default-Excludes List" requirement) and `--strip-unsafe-links` (per `cli-workspace`'s "Copy Symlink Security" requirement).

The `--clone` flag SHALL NOT be accepted; operators clone host-side then `--copy`, or `--empty` and clone from inside the container after `sandbox start`.

The `--as` flag (single-workspace name override, sketched in earlier proposals) SHALL NOT be accepted; the multi-flag grammar makes it unnecessary.

#### Scenario: Multi-workspace init
- **WHEN** `sandbox init foo --copy a=/p1 --copy b=/p2 --empty c` is invoked
- **THEN** three workspaces (`a`, `b`, `c`) are scaffolded under instance `foo` in a single invocation

#### Scenario: Duplicate name rejected
- **WHEN** `sandbox init foo --copy a=/p --empty a` is invoked
- **THEN** the CLI exits with a "duplicate workspace name" error before any state mutation

#### Scenario: --copy without = rejected
- **WHEN** `sandbox init foo --copy /path` is invoked
- **THEN** the CLI exits with "--copy requires NAME=PATH form"

#### Scenario: --clone flag rejected
- **WHEN** `sandbox init foo --clone <url>` is invoked
- **THEN** the CLI exits with "unknown option --clone" (the flag is removed; clone host-side then --copy, or --empty and clone in-container)

#### Scenario: --as flag rejected
- **WHEN** `sandbox init foo --as backend` is invoked
- **THEN** the CLI exits with "unknown option --as"

### Requirement: Per-Workspace Source Path Validation

For each `--copy NAME=PATH` flag, the system SHALL validate the source path before any state mutation:
- `realpath(PATH)` MUST exist and be a directory.
- The current user MUST have read access (`os.access(R_OK)`).
- The realpath MUST NOT match the walker boundary list (per `instance-workspace-model`).
- The realpath MUST NOT be inside `~/.sandbox-ai/workspaces/<inst>/` (cycle prevention).
- A size sanity warning SHALL emit if the source tree exceeds 5 GB (informational, not blocking).

#### Scenario: Source must exist
- **WHEN** `sandbox init foo --copy x=/does/not/exist` is invoked
- **THEN** the CLI exits with "source path does not exist" before any state mutation

#### Scenario: Source must be a directory
- **WHEN** `sandbox init foo --copy x=/etc/passwd` is invoked
- **THEN** the CLI exits with "source must be a directory"

#### Scenario: Source in walker boundary rejected
- **WHEN** `sandbox init foo --copy x=/etc` is invoked
- **THEN** the CLI exits with a walker-boundary error

### Requirement: Init Command Interface
The system SHALL provide a `sandbox init <inst> [--copy NAME=PATH ...] [--empty NAME ...]` command that scaffolds a new sandbox instance and one or more workspaces. The command SHALL read `docker_unprivileged_user` from the canonical per-host config file (`<sandbox_ai_home()>/config/sandbox-ai.toml`). If that file does not exist, the command SHALL seed it via interactive prompt in TTY mode or fail with explicit guidance in non-TTY mode (see "Per-User Tree Creation on Init" requirement). The command does not accept a `--machinectl-auth` flag; the `machinectl_authentication` value is read from host config and defaults to `"sudo"` (the only supported value, established by seeding). The previously-supported `--user` flag is removed.

CWD-based instance discovery is removed: the `<inst>` argument is positional and required.

#### Scenario: Init reads docker user from host config
- **WHEN** the operator runs `sandbox init foo` and `<home>/config/sandbox-ai.toml` contains `docker_unprivileged_user = "sandbox"`
- **THEN** the system uses `"sandbox"` as the docker unprivileged user

#### Scenario: machinectl auth defaults to sudo
- **WHEN** the operator runs `sandbox init foo` and `<home>/config/sandbox-ai.toml` does not specify `machinectl_authentication`
- **THEN** the system uses `"sudo"` as the authentication mode

#### Scenario: --user flag is rejected
- **WHEN** the operator runs `sandbox init foo --user sandbox`
- **THEN** the CLI exits with an "unknown option" error

#### Scenario: Instance name argument required
- **WHEN** the operator runs `sandbox init` without an `<inst>` argument
- **THEN** the CLI exits with a typer "missing argument" error

### Requirement: Init-Time Auth Mode Probe
The system SHALL validate that the resolved machinectl authentication mode works at init time by executing the dispatcher `auth-probe` op against the resolved docker unprivileged user (via `core.dispatch.probe("auth-probe", [], host_config)` — which crosses the bare `dispatch auth-probe` payload, NOT an inline `"echo ok"` string; the dispatcher's `auth-probe` target argv is `["/bin/bash", "-c", "echo ok"]`). The resolved `machinectl_authentication` mode is always SUDO, which crosses via `sudo_pipe_cmd` (the privileged byte-pipe). The probe SHALL use a 5-second timeout.

#### Scenario: Sudo mode probe succeeds
- **WHEN** init resolves `machinectl_authentication = "sudo"` and `core.dispatch.probe("auth-probe", [], host_config)` — crossing via `sudo_pipe_cmd(<user>)` as `[*sudo_pipe_cmd(<user>), "/bin/bash", "-c", "/usr/local/libexec/sandbox-ai/dispatch auth-probe"]` — returns `ok=True` within 5 seconds
- **THEN** init proceeds normally

#### Scenario: Sudo mode probe fails with timeout
- **WHEN** the sudo probe times out after 5 seconds (`probe(...)` returns `timed_out=True`)
- **THEN** init exits with an error including remediation: "Configure passwordless machinectl access in /etc/sudoers.d/"

### Requirement: Init Doctor Pre-Flight Auth Mode Awareness
The init command SHALL run a doctor pre-flight covering the `Filesystem` and `Repo Integrity` chains (excluding `ancestor_traverse`, since ACLs are granted during `start`, not `init`). Privilege Boundary verification at init time is delegated to the dedicated init-time auth probe (see "Init-Time Auth Mode Probe" requirement). The pre-flight SHALL run the `Filesystem` and `Repo Integrity` checks via `run_check_subset()` / `build_check_registry()`; the privilege-boundary checks are not in the pre-flight scope.

#### Scenario: Pre-flight runs the filesystem and repo-integrity chains
- **WHEN** init runs the Filesystem + Repo Integrity pre-flight
- **THEN** only those chains' checks execute; `run_check_subset(...)` builds the registry with dependency graph and cascading-skip logic preserved

#### Scenario: Privilege Boundary verification is performed by the init-time probe, not the pre-flight
- **WHEN** init runs
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
The system SHALL execute doctor Chain 2 (Filesystem) and Chain 3 (Repo Integrity) checks before beginning scaffold. The pre-flight SHALL also verify bridge-group existence and the dev process's supplementary-group membership (catches the post-`usermod`/pre-relogin pitfall). If any check fails, init SHALL abort with the doctor diagnostic output and exit code 1.

#### Scenario: Pre-flight passes
- **WHEN** `init` is invoked and setfacl is on PATH, filesystem supports ACLs, tooling plane files are present, `<sandbox_ai_home()>/state/` is writable, the bridge group exists, and the dev process has the bridge gid in its supplementary groups
- **THEN** scaffold proceeds normally

#### Scenario: Bridge group missing fails pre-flight
- **WHEN** `init` is invoked and the configured bridge group does not exist
- **THEN** init aborts with the doctor failure output (including copy-pasteable `groupadd`/`usermod` commands), before creating any files or directories

#### Scenario: Pre-flight fails on missing setfacl
- **WHEN** `init` is invoked and setfacl is not on PATH
- **THEN** init aborts with the doctor failure output including remediation guidance, before creating any files or directories

### Requirement: Init Re-Init Guard
The system SHALL reject init for an instance name that already has an entry in the registry.

#### Scenario: Re-init rejected
- **WHEN** `sandbox init <inst>` is invoked and `<inst>` already exists in `instances.json`
- **THEN** the CLI exits with: "Instance '<inst>' already initialized. Use `sandbox workspace add` to add workspaces, or `sandbox destroy <inst>` first." and exit code 1

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
When `<home>/config/sandbox-ai.toml` does not exist, `sandbox init` SHALL seed it. In TTY mode, the system SHALL prompt the operator interactively for `docker_unprivileged_user` (required) and `machinectl_authentication` (optional, default `"sudo"`, only accepting `"sudo"`). In non-TTY mode, the system SHALL exit with a clear error directing the operator to create the file manually before retrying.

The seeded file SHALL begin with a leading managed-comment header — `# sandbox-ai managed — values are setup-determined; do not edit (rerun setup to change)` — guarding the setup-determined `[host]` fields (D10 stopgap). The seed SHALL NOT write a `docker_execution_mode` field: the execution mode is no longer a toml field (it is resolved at runtime from the setup-state marker — see the `host-config` capability's "Docker Execution Mode Selector").

#### Scenario: TTY clean install — interactive seed
- **WHEN** `sandbox init` runs in a TTY and `<home>/config/sandbox-ai.toml` does not exist
- **THEN** the CLI prompts: "docker_unprivileged_user (e.g., sandbox):" — accepts a non-empty value — then prompts: "machinectl_authentication [sudo, default sudo]:" — accepts `sudo` or empty (defaulting to `sudo`) — then writes the seeded values to `<home>/config/sandbox-ai.toml`, preceded by the managed-comment header and with no `docker_execution_mode` field

#### Scenario: TTY clean install — empty user rejected
- **WHEN** the operator presses Enter without typing a value at the `docker_unprivileged_user` prompt
- **THEN** the CLI re-prompts (empty values are not accepted; the field is required)

#### Scenario: Non-TTY clean install — fail with guidance
- **WHEN** `sandbox init` runs in a non-TTY environment (e.g., CI pipeline) and `<home>/config/sandbox-ai.toml` does not exist
- **THEN** the CLI exits with: "Cannot prompt for `docker_unprivileged_user` in non-interactive mode. Create `<resolved-home>/config/sandbox-ai.toml` with a `[host]` section containing `docker_unprivileged_user` before running `sandbox init`." and exit code 1

#### Scenario: Existing host config is not re-seeded
- **WHEN** `sandbox init` runs and `<home>/config/sandbox-ai.toml` already exists with valid content
- **THEN** the system does NOT prompt and does NOT overwrite; init proceeds using the existing values

#### Scenario: seeded toml carries the managed-comment header
- **WHEN** `sandbox init` seeds `<home>/config/sandbox-ai.toml`
- **THEN** the file's first line is the `# sandbox-ai managed — …; do not edit (rerun setup to change)` comment and the file contains no `docker_execution_mode` key

### Requirement: Init Detects Legacy CWD-Local Files
During `sandbox init`, the system SHALL inspect the current working directory for legacy `<cwd>/sandbox-ai.toml` and `<cwd>/.state/` and warn the operator that these files are no longer used.

#### Scenario: Legacy host config detected
- **WHEN** `sandbox init` runs and `<cwd>/sandbox-ai.toml` exists
- **THEN** init prints a warning: "Found legacy `<cwd>/sandbox-ai.toml`. Per-host config now lives at `<resolved-home>/config/sandbox-ai.toml`. Migrate manually or delete the legacy file."

#### Scenario: Legacy state directory detected
- **WHEN** `sandbox init` runs and `<cwd>/.state/` exists
- **THEN** init prints a warning: "Found legacy `<cwd>/.state/`. Orchestrator state now lives at `<resolved-home>/state/`. Migrate manually or delete the legacy directory."

