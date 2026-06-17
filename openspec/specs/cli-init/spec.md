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

The system SHALL provide a `sandbox init <inst> [--copy NAME=PATH ...] [--empty NAME ...] [--secrets-from-env | --secrets-from-file PATH]` command that scaffolds a new sandbox instance and one or more workspaces. The command SHALL build its host config from the setup marker via `HostConfig.from_marker(operator)` (per the `host-config` capability) — **setup is a prerequisite for init**. On a host where setup has not run for the invoking operator (no marker entry), the command SHALL stop with a friendly, actionable message — *"This host isn't set up yet. Run `sudo sandbox setup` first, then `sandbox init`."* — that does NOT reference the internal marker. The setup-first check SHALL run **before any state mutation** — before the per-user tree is created (`ensure_per_user_state`) and before any instance scaffold — so a marker-absent host is never left half-initialized. The command does not accept a `--machinectl-auth` flag and there is no authentication field. The previously-supported `--user` flag is removed.

The `--secrets-from-env` and `--secrets-from-file` flags are mutually exclusive — supplying both SHALL be rejected before any state mutation. Their secret-seeding behavior is defined in the "Non-Interactive Secret Seeding" requirement.

CWD-based instance discovery is removed: the `<inst>` argument is positional and required.

#### Scenario: Init builds host config from the marker

- **WHEN** the operator runs `sandbox init foo` on a host where setup has provisioned the invoking operator
- **THEN** the system builds the host config via `HostConfig.from_marker(operator)` and uses the resolved daemon owner (the operator in operator-rootless, the `docker_unprivileged_user` in separate-user)

#### Scenario: Init on an unprovisioned host directs to setup

- **WHEN** the operator runs `sandbox init foo` on a host where setup has not run for them
- **THEN** the CLI exits non-zero — before creating the per-user tree or any scaffold — with "This host isn't set up yet. Run `sudo sandbox setup` first, then `sandbox init`." and names neither the marker nor `setup-state.json`; no `<home>/` tree is left behind

#### Scenario: --user flag is rejected
- **WHEN** the operator runs `sandbox init foo --user sandbox`
- **THEN** the CLI exits with an "unknown option" error

#### Scenario: Instance name argument required
- **WHEN** the operator runs `sandbox init` without an `<inst>` argument
- **THEN** the CLI exits with a typer "missing argument" error

#### Scenario: Conflicting secret-source flags rejected
- **WHEN** the operator runs `sandbox init foo --secrets-from-env --secrets-from-file /tmp/s.env`
- **THEN** the CLI exits with a "mutually exclusive" error before any state mutation

### Requirement: Init-Time Auth Mode Probe

The system SHALL validate that the privilege boundary is reachable at init time by executing the dispatcher `preflight` op against the resolved daemon owner (via `core.dispatch.probe("preflight", [], host_config, timeout=15)`). The probe is **mode-aware**: in operator-rootless mode it runs as a local subprocess (no boundary crossing); in separate-user mode it crosses via `sudo_pipe_cmd` (the privileged byte-pipe). The probe SHALL use a 15-second timeout. The crossing succeeding (its bundled segments parse and the `auth-probe` segment is present) IS the reachability signal — there is no separate standalone `auth-probe` crossing at init; the single `preflight` bundle is the source of truth for boundary reachability at init time, and its bundled `compose-ls` segment additionally feeds the compose-project-name collision pre-flight (so init fires no second `compose-ls` crossing). The probe is NOT deferred by the non-interactive secret-seeding path. The active execution mode the probe uses is resolved from the setup marker (`resolve_execution_mode`, per the `host-config` capability's "Docker Execution Mode Selector"); because init is setup-first (see "Init Command Interface"), a concrete mode is always available by the time the probe runs — init never probes under a guessed mode.

#### Scenario: Preflight probe succeeds
- **WHEN** init resolves the execution mode and `core.dispatch.probe("preflight", [], host_config, timeout=15)` returns a bundle whose reachability gate is satisfied within 15 seconds
- **THEN** init proceeds normally

#### Scenario: Preflight probe fails with timeout
- **WHEN** the preflight probe times out after 15 seconds (`probe(...)` returns `timed_out=True`)
- **THEN** init exits non-zero with an error reporting "probe timed out after 15 seconds" and mode-appropriate reachability remediation guidance

#### Scenario: Preflight crossing unreachable
- **WHEN** the preflight crossing returns but the reachability gate is not satisfied (the `auth-probe` segment is absent or failed)
- **THEN** init exits non-zero with the boundary-unreachable diagnostic derived from the bundle's `auth-probe` segment (or the probe's own failure message), before scaffold begins

### Requirement: Init Doctor Pre-Flight Auth Mode Awareness
The init command SHALL run a doctor pre-flight covering the `Filesystem` and `Repo Integrity` chains (excluding `ancestor_traverse`, since ACLs are granted during `start`, not `init`). Privilege Boundary verification at init time is delegated to the dedicated init-time preflight probe (see "Init-Time Auth Mode Probe" requirement). The pre-flight SHALL run the `Filesystem` and `Repo Integrity` checks via `run_check_subset()` / `build_check_registry()`; the privilege-boundary checks are not in the pre-flight scope.

#### Scenario: Pre-flight runs the filesystem and repo-integrity chains
- **WHEN** init runs the Filesystem + Repo Integrity pre-flight
- **THEN** only those chains' checks execute; `run_check_subset(...)` builds the registry with dependency graph and cascading-skip logic preserved

#### Scenario: Privilege Boundary verification is performed by the init-time probe, not the pre-flight
- **WHEN** init runs
- **THEN** Chain 1 (Privilege Boundary) checks are NOT executed by the pre-flight; the init-time preflight probe (15-second timeout) is the single source of truth for boundary reachability at init time

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
The system SHALL skip interactive secret prompting when stdin is not a TTY **and no secret-source flag (`--secrets-from-env` / `--secrets-from-file`) is given**. In that case it SHALL stub each required secret slot in `.sandbox.env` with a clearly-fake placeholder of the form `YOUR_<NAME>_HERE` — **not** leave it empty — so the subsequent `sandbox start` "missing required secrets" pre-flight does not block automation flows for sandboxes that do not use those services at runtime. Pre-existing non-empty (operator-edited) values SHALL be preserved. If any required secret slot is absent or not in the canonical `<NAME>=""` form (so the stub cannot be applied), init SHALL fail loud, naming the unresolved slot(s), rather than print a misleading "stub values written" message. When a secret-source flag IS given, the "Non-Interactive Secret Seeding" requirement governs instead (populate-or-refuse), in both TTY and non-TTY contexts.

#### Scenario: Non-TTY init completes with stub placeholders
- **WHEN** `init` is invoked in a non-TTY environment (e.g., CI pipeline) with no secret-source flag and each required secret is present in `.sandbox.env` in the canonical `<NAME>=""` form
- **THEN** scaffold completes through S7 (sentinel written), each required secret slot holds a `YOUR_<NAME>_HERE` placeholder value (not empty), and no RuntimeError is raised

#### Scenario: Non-TTY stub fails loud on a non-canonical slot
- **WHEN** `init` runs non-TTY with no secret-source flag and a required secret slot is absent or not in the canonical `<NAME>=""` form
- **THEN** init raises (rather than printing a "stub values written" message), naming the unresolved slot(s)

#### Scenario: Non-TTY guidance printed
- **WHEN** `init` completes in non-TTY mode with no secret-source flag
- **THEN** the CLI prints the path to `.sandbox.env` with instructions to edit it and set real values before running `sandbox start`

### Requirement: Init Dry-Run Preview
The system SHALL support `sandbox init --dry-run` that previews the scaffold output without writing any state. The preview SHALL report the secret-seeding SOURCE the real run would use, derived from the flags and context: with `--secrets-from-env`, that secrets would be seeded from the named environment variables (flagging any currently unset); with `--secrets-from-file PATH`, that secrets would be seeded from `PATH` (flagging any missing required keys); with no flag in a TTY, that the named secrets would be prompted interactively; with no flag in non-TTY, that the required secret slots would be stubbed with `YOUR_<NAME>_HERE` placeholders. The dry-run SHALL NOT print any secret value, and (consistent with its no-mutation contract) SHALL report missing secrets rather than refusing.

#### Scenario: Dry-run previews config
- **WHEN** `sandbox init --dry-run` is invoked
- **THEN** the system prints the instance ID, directory path, generated sandbox.toml content, the secret-seeding source the real run would use, and ACL commands that would execute, without creating any files or registry entries

#### Scenario: Dry-run reports the secret source for --secrets-from-env
- **WHEN** `sandbox init foo --dry-run --secrets-from-env` is invoked
- **THEN** the preview states that secrets would be seeded from the required-secret environment variables (naming them), flags any that are currently unset, and prints no secret values

#### Scenario: Dry-run exits cleanly
- **WHEN** `sandbox init --dry-run` completes
- **THEN** no files exist in the sandboxes directory and instances.json is unmodified

### Requirement: Per-User Tree Creation on Init

The system SHALL create the per-user tree (`<home>/`, `<home>/config/`, `<home>/state/`) during `sandbox init` if any of the directories is missing, with mode `0700`. The creation SHALL be idempotent — re-running `sandbox init` against an existing tree SHALL NOT raise an error and SHALL NOT modify existing directory modes. `sandbox init` SHALL NOT create a `sandbox-ai.toml` host config file (host facts now live in the setup marker; see the `host-config` capability). After init completes, `<home>/state/instances.json` SHALL exist (created empty if not present) so that subsequent lifecycle commands recognize the host as initialized — `instances.json` is the sole "init has run" gate.

#### Scenario: Tree and instances.json created, no toml
- **WHEN** `sandbox init` is invoked on a host with no per-user tree
- **THEN** init creates `<home>/`, `<home>/config/`, and `<home>/state/` with mode `0700`, and writes an empty `<home>/state/instances.json`, and does NOT write any `<home>/config/sandbox-ai.toml`

#### Scenario: Idempotent re-init preserves existing tree
- **WHEN** `sandbox init` is invoked and the per-user tree already exists
- **THEN** init does not raise and does not modify existing directory modes

### Requirement: Init Detects Legacy CWD-Local Files
During `sandbox init`, the system SHALL inspect the current working directory for legacy `<cwd>/sandbox-ai.toml` and `<cwd>/.state/` and warn the operator that these files are no longer used.

#### Scenario: Legacy host config detected
- **WHEN** `sandbox init` runs and `<cwd>/sandbox-ai.toml` exists
- **THEN** init prints a warning: "Found legacy `<cwd>/sandbox-ai.toml`. Per-host config is now setup-determined — run `sudo sandbox setup`. Delete the legacy file."

#### Scenario: Legacy state directory detected
- **WHEN** `sandbox init` runs and `<cwd>/.state/` exists
- **THEN** init prints a warning: "Found legacy `<cwd>/.state/`. Orchestrator state now lives at `<resolved-home>/state/`. Migrate manually or delete the legacy directory."

### Requirement: Non-Interactive Secret Seeding
The system SHALL provide two mutually-exclusive opt-in flags on `sandbox init` for populating the required instance secrets without an interactive prompt:

- `--secrets-from-env` — read each required secret from the process environment.
- `--secrets-from-file PATH` — read each required secret from a `KEY=VALUE` file at `PATH`.

The required secrets are `CORE_ANTHROPIC_API_KEY` and `CORE_GITHUB_TOKEN` (the same names used as env keys and as file keys). When either flag is present, init is in explicit non-interactive secret mode and SHALL either populate ALL required secrets into `.sandbox.env` or REFUSE before any state mutation, exiting non-zero with an error naming exactly which required secrets are missing. This populate-or-refuse contract is distinct from the "Init Non-TTY Mode" stub-placeholder default (which writes `YOUR_<NAME>_HERE` placeholders), and applies only when a secret-source flag is given.

The `--secrets-from-file` format SHALL be: one `KEY=VALUE` per line; a line whose first non-whitespace character is `#` is a comment and is ignored; blank lines are ignored; no `export ` prefix is accepted; each KEY MUST be a member of the required-secret set (an unrecognized key SHALL be rejected). A required key absent from the file, or present with an empty value, SHALL trigger the refusal above.

The seeding SHALL be honored regardless of whether stdin is a TTY (an interactive operator MAY also use the flags to skip the prompts).

The system SHALL NOT implicitly consume secrets from the environment. When NO secret-source flag is given and one or more required-secret environment variables are nonetheless set, the CLI SHALL emit a single informational hint that names the detected variable(s) and recommends `--secrets-from-env`, WITHOUT seeding from them — the no-flag prompt (TTY) / skip (non-TTY) behavior is unchanged. The hint SHALL reveal only variable names, never values.

#### Scenario: --secrets-from-env populates secrets
- **WHEN** `sandbox init foo --secrets-from-env` is invoked with `CORE_ANTHROPIC_API_KEY` and `CORE_GITHUB_TOKEN` both set in the environment
- **THEN** scaffold completes with `.sandbox.env` containing those secret values, with no interactive prompt

#### Scenario: --secrets-from-env refuses on a missing required var
- **WHEN** `sandbox init foo --secrets-from-env` is invoked with `CORE_GITHUB_TOKEN` set but `CORE_ANTHROPIC_API_KEY` unset
- **THEN** the CLI exits non-zero with an error naming `CORE_ANTHROPIC_API_KEY` as the missing required secret, before any state mutation

#### Scenario: --secrets-from-file populates secrets
- **WHEN** `sandbox init foo --secrets-from-file /tmp/secrets.env` is invoked and the file contains `CORE_ANTHROPIC_API_KEY=...` and `CORE_GITHUB_TOKEN=...` (one KEY=VALUE per line, optionally with `#` comment lines)
- **THEN** scaffold completes with `.sandbox.env` populated from the file, with no interactive prompt

#### Scenario: --secrets-from-file refuses on a missing required key
- **WHEN** `sandbox init foo --secrets-from-file /tmp/secrets.env` is invoked and the file omits `CORE_GITHUB_TOKEN`
- **THEN** the CLI exits non-zero naming `CORE_GITHUB_TOKEN` as missing, before any state mutation

#### Scenario: --secrets-from-file rejects an unrecognized key
- **WHEN** the file contains a `KEY=VALUE` line whose KEY is not a member of the required-secret set
- **THEN** the CLI exits non-zero identifying the unrecognized key, before any state mutation

#### Scenario: Detected env vars emit a hint without consumption
- **WHEN** `sandbox init foo` is invoked with NO secret-source flag and `CORE_ANTHROPIC_API_KEY` is set in the environment
- **THEN** the CLI prints a single informational hint naming `CORE_ANTHROPIC_API_KEY` and recommending `--secrets-from-env`, does NOT seed any secret from the environment, and the no-flag prompt (TTY) / skip (non-TTY) behavior is unchanged

