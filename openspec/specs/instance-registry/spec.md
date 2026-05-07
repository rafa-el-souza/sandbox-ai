## Purpose

This specification defines the instance registry that maps instance names (per-user globally unique) to instance metadata, enabling multi-instance discovery and lifecycle management.

## Requirements

### Requirement: Compose Project Name Prefix

The daemon-side compose project name for instance `<inst>` SHALL be `<sanitized(dev-username)>-<inst>`, where the dev username is sanitized via `re.sub(r'[^a-z0-9]', '-', name).strip('-')`. Operator-facing names in CLI output and registry entries remain `<inst>` (without prefix); the prefixed name is daemon-side only and appears in `docker compose` invocations and container names.

This prefix prevents cross-user compose project collisions on multi-user hosts where the rootless docker daemon runs under a single shared `<docker_unprivileged_user>`. The legacy `<inst>-<hash>` suffix scheme (which masked the same risk via per-CWD hash salting) is dropped per change 5.

The instance name length cap of 30 characters (per `instance-workspace-model`'s "Instance Name Validation") together with sanitized-username truncation keeps the worst-case container name `<dev>-<inst>-<service>-<idx>` within docker's 64-char container-name limit.

#### Scenario: Compose project name format
- **WHEN** `sandbox start <inst>` invokes `docker compose` for an instance owned by dev user `dev.foo`
- **THEN** the `COMPOSE_PROJECT_NAME` (or `-p` flag) value is `dev-foo-<inst>` (the username sanitized via the regex)

#### Scenario: Sanitization handles non-alphanumeric characters
- **WHEN** the dev username contains `.`, `_`, or other non-`[a-z0-9]` characters
- **THEN** each such character is replaced with `-`, and leading/trailing `-` are stripped, before concatenation with the instance name

#### Scenario: CLI output uses unprefixed name
- **WHEN** `sandbox status <inst>` displays the instance identity
- **THEN** the displayed name is `<inst>` (no `<dev>-` prefix); the prefix is daemon-internal only

#### Scenario: Registry stores unprefixed name
- **WHEN** `instances.json` is inspected
- **THEN** the key is `<inst>` (the operator-supplied name); the daemon-side prefix is computed at runtime, not stored

### Requirement: Per-Instance Backup Lock

The system SHALL maintain per-instance backup locks at `~/.sandbox-ai/state/<inst>.backup.lock` to coordinate long-running backup operations without monopolizing the per-user `state.lock`. The lock file SHALL be created (with `LOCK_NB`) at the start of a backup operation, contain `{pid, started_at_utc}` for stale-detection, and be released on operation completion or process exit (fcntl auto-release). Other lifecycle commands (`start`, `stop`, `workspace add/remove/rename/restore`, `destroy`) SHALL check `<inst>.backup.lock` after acquiring `state.lock` and refuse fast if held.

The grace period before declaring a lock stale (when PID is not alive) SHALL be 60 seconds from `started_at_utc`. Doctor checks `pid`-alive and breaks stale locks with a logged warning.

#### Scenario: Backup lock acquired non-blockingly
- **WHEN** a backup operation begins
- **THEN** `<inst>.backup.lock` is acquired with `LOCK_EX | LOCK_NB`; if already held, the operation fails fast

#### Scenario: Concurrent ops fail-fast while held
- **WHEN** `<inst>.backup.lock` is held by a backup and `sandbox start <inst>` is invoked
- **THEN** start exits with a clear error directing the operator to wait or run `sandbox doctor`

#### Scenario: Lock released on process exit
- **WHEN** the orchestrator process holding the lock terminates (normal or signal-driven)
- **THEN** the fcntl lock is auto-released; the lock file may be left as a residue but is overwritten on next acquire

#### Scenario: Stale lock broken after grace period
- **WHEN** `sandbox doctor` or a lock-acquire path encounters a lock file whose PID is not alive AND `started_at_utc` is older than 60 seconds
- **THEN** the lock is broken (file removed) with a logged warning

### Requirement: Instance Registration

The system SHALL persist a mapping from **instance name** (string, validated per the `instance-workspace-model` capability's "Instance Name Validation" requirement) to instance metadata in `<sandbox_ai_home()>/state/instances.json`. The registry is per-user; instance names are globally unique per-user.

The registry value for each entry SHALL contain at minimum:
- `instance_dir`: absolute path of the instance directory (today always `~/.sandbox-ai/instances/<inst-name>/`; retained as a field for door-keeping per `instance-workspace-model`'s "Future --existing Bootstrap Mode Door-Keeping" pattern).
- `created_at`: ISO-8601 UTC timestamp of registration.

The legacy `instance_id` field (`<name>-<hash>`) SHALL NOT be present. The hash suffix scheme is dropped.

#### Scenario: First invocation creates registry entry
- **WHEN** `sandbox init <inst>` is invoked and `<inst>` is not present in `instances.json`
- **THEN** a new entry is written with key `<inst>` and value `{instance_dir: "~/.sandbox-ai/instances/<inst>", created_at: "<utc>"}` before any per-instance state is created

#### Scenario: Subsequent invocations reuse existing entry
- **WHEN** `sandbox start`, `stop`, `attach`, `destroy`, or any `workspace ...` command is invoked
- **THEN** the `instance_dir` is read from `instances.json[<inst>]` without re-derivation

#### Scenario: Instance name globally unique
- **WHEN** `sandbox init <inst>` is invoked and `<inst>` already exists in `instances.json`
- **THEN** the CLI exits with the re-init guard (per `cli-init`)

#### Scenario: Two invocations under same user from different CWDs
- **WHEN** `sandbox init foo` is invoked from `/home/alice/projects/p1` and `sandbox init bar` is invoked from `/home/alice/projects/p2`
- **THEN** both entries appear in the same `~/.sandbox-ai/state/instances.json` keyed by `foo` and `bar` (CWD has no role in registry keying)

### Requirement: Unregistered Instance Rejection
The system SHALL reject lifecycle commands other than `init` when the supplied instance name is not present in the registry.

#### Scenario: Non-init command on unknown instance
- **WHEN** `sandbox stop foo`, `attach foo`, `destroy foo`, or any `workspace ...` command is invoked for an `<inst>` not present in `instances.json`
- **THEN** the CLI exits with: "No sandbox instance named '<inst>'. Use 'sandbox init <inst>' to create one." and exit code 1

### Requirement: Registry Lock Safety
The system SHALL guard all reads and writes to `instances.json` against concurrent modification using `<sandbox_ai_home()>/state/state.lock`. The lock is per-user; concurrent `sandbox` invocations across any working directories under the same user serialize through the same lock file. Long-running operations (notably backups) SHALL release `state.lock` during their long phase and reacquire for final mutations, coordinating with `<inst>.backup.lock` per the "Per-Instance Backup Lock" requirement.

#### Scenario: Concurrent invocations from different CWDs serialize
- **WHEN** two `sandbox` invocations run concurrently for different instances under the same user
- **THEN** they serialize on `<home>/state/state.lock`; both registry mutations complete without corruption

#### Scenario: Lock is transient, not held during runtime
- **WHEN** a `sandbox start` provisioning phase completes and containers are running
- **THEN** `state.lock` is released; running sandboxes do not hold the lock during their runtime, allowing parallelism across instances at runtime

#### Scenario: Backup operations release state.lock during rsync
- **WHEN** a backup operation is in its long rsync phase
- **THEN** `state.lock` is NOT held; the per-instance backup lock guards mutual exclusion for the affected instance

### Requirement: Registry Entry Removal on Destroy
The system SHALL remove the registry entry for a destroyed instance to free the name for potential reuse.

#### Scenario: Destroy removes registry entry
- **WHEN** `sandbox destroy <inst>` completes successfully
- **THEN** the `instances.json` entry keyed by `<inst>` is removed
