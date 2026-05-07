# cli-workspace Specification

## Purpose

The `sandbox workspace` subcommand group provides operator-facing lifecycle management for workspaces within an instance: adding, removing, renaming, restoring from backups, and listing live workspaces and available backups. Mutating operations require the target instance to be stopped (MVP simplification); live workspace lifecycle ops are out of scope.

## Requirements

### Requirement: Workspace Subcommand Surface

The CLI SHALL provide a `sandbox workspace` subcommand group with five operations: `add`, `remove`, `rename`, `restore`, `list`. All operations except `list` SHALL require the target instance to be STOPPED (no running containers); attempting any of them on a running instance SHALL be rejected with guidance to run `sandbox stop <inst>` first. The MVP simplification of "stopped-only" applies; live workspace lifecycle ops are out of scope.

#### Scenario: Subcommand group is discoverable
- **WHEN** the operator runs `sandbox workspace --help`
- **THEN** the help output lists `add`, `remove`, `rename`, `restore`, and `list` as subcommands

#### Scenario: Mutating ops require stopped instance
- **WHEN** any of `workspace add`, `workspace remove`, `workspace rename`, or `workspace restore` is invoked against an instance whose containers are running
- **THEN** the CLI exits with: "Instance '<inst>' must be stopped. Run `sandbox stop <inst>` first." and exit code 1

### Requirement: workspace add Command Grammar

The `sandbox workspace add` command SHALL accept the same multi-flag grammar as `sandbox init`: `[--copy NAME=PATH ...] [--empty NAME ...]`. Both flag types SHALL be `multiple=True`. Names supplied across all flags in a single invocation MUST be unique (no duplicate `NAME` across `--copy` and `--empty`, and no duplicate within either). At least one flag MUST be supplied; an invocation with neither `--copy` nor `--empty` SHALL be rejected (no-op guard). Names already present in the target instance's `[workspaces]` SHALL be rejected.

The `--copy NAME=PATH` value is parsed by splitting on the first `=`. NAME must pass workspace-name validation (per the `instance-workspace-model` capability). PATH is the rest of the value; values without `=` are rejected with "--copy requires NAME=PATH form". Empty NAME or empty PATH is rejected.

#### Scenario: Multiple workspaces added in one call
- **WHEN** `sandbox workspace add foo --copy a=/p1 --copy b=/p2 --empty c` is invoked
- **THEN** three workspaces (`a`, `b`, `c`) are added to instance `foo` in a single invocation

#### Scenario: Empty invocation rejected
- **WHEN** `sandbox workspace add foo` is invoked with no `--copy` or `--empty` flags
- **THEN** the CLI exits with: "Specify at least one --copy or --empty flag." and exit code 1

#### Scenario: Duplicate name across flags rejected
- **WHEN** `sandbox workspace add foo --copy a=/p --empty a` is invoked
- **THEN** the CLI exits with a "duplicate workspace name" error before any state mutation

#### Scenario: Name collision with existing workspace rejected
- **WHEN** `sandbox workspace add foo --empty main` is invoked and instance `foo` already has a workspace named `main`
- **THEN** the CLI exits with a "workspace already exists" error

#### Scenario: --copy without = rejected
- **WHEN** `sandbox workspace add foo --copy /path/to/dir` is invoked
- **THEN** the CLI exits with: "--copy requires NAME=PATH form" and exit code 1

### Requirement: workspace add Pre-Flight Gates

For each `--copy NAME=PATH` flag, the system SHALL validate the source path before any state mutation:
- The path's `realpath` MUST exist and be a directory.
- The current user MUST have read access to the path.
- The realpath MUST NOT match any entry in the walker boundary list (per `instance-workspace-model`).
- The realpath MUST NOT be inside `~/.sandbox-ai/workspaces/<inst>/` (cycle prevention).
- A size sanity warning SHALL emit if the source tree exceeds 5 GB (informational; not blocking).

Pre-flight gates run before any workspace dir is created. If any gate fails, the command exits with a structured error identifying the failing source.

#### Scenario: Nonexistent source rejected
- **WHEN** `sandbox workspace add foo --copy x=/does/not/exist` is invoked
- **THEN** the CLI exits with "source path does not exist" before any state mutation

#### Scenario: Source in boundary list rejected
- **WHEN** `sandbox workspace add foo --copy x=/etc` is invoked
- **THEN** the CLI exits with a walker-boundary error

#### Scenario: Source inside instance's workspace tree rejected
- **WHEN** `sandbox workspace add foo --copy x=~/.sandbox-ai/workspaces/foo/main` is invoked
- **THEN** the CLI exits with a "cycle prevention" error

### Requirement: workspace add Per-Workspace Scaffold

For each workspace flag, the system SHALL execute the per-workspace scaffold steps (W1–W3 from the `instance-workspace-model` lifecycle):

1. `mkdir -p ~/.sandbox-ai/workspaces/<inst>/<ws>/` mode `0700` dev:dev.
2. For `--copy NAME=PATH`: execute the rsync recipe per the `--copy` semantics (default-excludes from this capability's "Copy Default-Excludes" requirement; pre-copy symlink scan with refuse-by-default, `--strip-unsafe-links` opt-in; rsync `-a --no-owner --no-group <excludes...> [--safe-links] <src>/ <ws-dir>/`). For `--empty NAME`: no-op (W1's mkdir is sufficient).
3. Add a corresponding `[workspaces.<ws>]` entry to `sandbox.toml` with `bootstrap_mode`, `source` (for `copy` only), and `path` set to the resolved workspace dir.

The next `sandbox start <inst>` picks up the new workspace via re-hydration; the change-4 shared-group recipe's drift detection triggers recursive setup on the new tree.

#### Scenario: Scaffold creates workspace dir at 0700 dev:dev
- **WHEN** `workspace add` runs the per-workspace scaffold
- **THEN** the workspace directory exists at mode `0700` ownership dev:dev

#### Scenario: --copy invokes rsync recipe
- **WHEN** a `--copy NAME=PATH` flag is processed
- **THEN** rsync runs with the default-exclude list, ownership-stripping flags (`--no-owner --no-group`), and (conditionally) `--safe-links` per this capability's "Copy Symlink Security" requirement

#### Scenario: sandbox.toml gains workspace entry
- **WHEN** scaffold completes for `<ws>` with bootstrap mode `copy` and source `<src>`
- **THEN** `sandbox.toml` contains a `[workspaces.<ws>]` section with `bootstrap_mode = "copy"`, `source = "<src>"`, and `path = "<resolved-ws-dir>"`

### Requirement: Copy Default-Excludes List

The `--copy` codepath (used by `init`, `workspace add`, and `workspace restore`) SHALL apply a default-exclude list to the rsync invocation. The list SHALL be:

```
node_modules/  .pnpm-store/  .yarn/cache/  .yarn/install-state.gz
.venv/  venv/  env/  __pycache__/  *.pyc  .pytest_cache/  .mypy_cache/  .ruff_cache/  .tox/
target/  bin/  dist/  build/  out/  .next/  .nuxt/  .tsbuildinfo  .turbo/
vendor/  vendor/bundle/  .bundle/
.gradle/  cmake-build-*/
.idea/  .vscode/  .DS_Store  *.swp  *.log
coverage/  .nyc_output/  .cache/
```

`.git/` SHALL NOT be excluded (portable history).

The CLI SHALL accept a `--no-default-excludes` flag to disable the list, and `--exclude=<glob>` (`multiple=True`) to append additional excludes (rsync passthrough).

#### Scenario: Default-excludes applied
- **WHEN** `--copy main=/path/to/project` runs against a tree containing `node_modules/`, `.venv/`, `.git/`, and source files
- **THEN** the resulting workspace contains `.git/` and source files but NOT `node_modules/` or `.venv/`

#### Scenario: --no-default-excludes disables list
- **WHEN** `--copy main=/path/to/project --no-default-excludes` is invoked
- **THEN** the rsync command does not include any default-exclude entries (operator-supplied `--exclude=<glob>` flags still apply)

#### Scenario: --exclude appends to list
- **WHEN** `--copy main=/path --exclude=secrets/` is invoked
- **THEN** the rsync command includes both the default-exclude list AND `secrets/`

### Requirement: Copy Symlink Security

After applying default-excludes, the `--copy` codepath SHALL walk the surviving source tree and identify symlinks whose `realpath` escapes the source root (with `lstat` discipline, no symlink dereference at the walker level). If any external symlinks are found:

- Without `--strip-unsafe-links`: refuse with a structured error listing count + samples and three remediation options (use default-excludes, pass `--strip-unsafe-links`, or pivot to `--empty`). NO state mutation occurs.
- With `--strip-unsafe-links`: the rsync invocation gains the `--safe-links` flag (drops external symlinks during copy). The CLI logs a warning indicating how many links were stripped.

This refuse-by-default posture forces a conscious operator decision rather than silently mutating the source tree's shape.

#### Scenario: External symlink without --strip-unsafe-links rejected
- **WHEN** `--copy main=/path` runs against a tree containing a symlink with absolute target `/etc/passwd`, and `--strip-unsafe-links` is NOT passed
- **THEN** the CLI exits with a structured error listing the unsafe-link count and remediation options before any state mutation

#### Scenario: --strip-unsafe-links proceeds with safe-links flag
- **WHEN** the same tree is copied with `--strip-unsafe-links` passed
- **THEN** rsync runs with `--safe-links` added; external symlinks are dropped from the resulting workspace; a warning logs the count of stripped links

#### Scenario: No external symlinks proceeds without prompt
- **WHEN** the source tree (after default-excludes) contains no symlinks pointing outside the source root
- **THEN** rsync runs without `--safe-links`; no warning is emitted

### Requirement: workspace remove Command

`sandbox workspace remove <inst> <ws-name> [--backup | --purge]` SHALL remove a workspace. Flags `--backup` and `--purge` are mutually exclusive (typer callback). Behavior:

- TTY mode + neither flag: prompt "Backup workspace '<ws>' before removing? [Y/n]". The default is Yes.
- Non-TTY mode + neither flag: refuse with "use --backup or --purge".
- `--backup`: create a backup per the "Workspace Backup Recipe" requirement, then `shutil.rmtree(ws.path)`.
- `--purge`: `shutil.rmtree(ws.path)` directly, no backup.

Backup failure (rsync error, disk full, etc.) SHALL abort the remove. The `<ts>.partial/` backup directory is retained for diagnosis. `sandbox.toml` is NOT mutated. The command is idempotent on retry.

After successful removal, `sandbox.toml` drops the `[workspaces.<ws>]` entry. If no workspaces remain in the instance, the command emits a warning that `sandbox start <inst>` will fail without a workspace; the operation does not block.

#### Scenario: --backup and --purge mutually exclusive
- **WHEN** `workspace remove foo bar --backup --purge` is invoked
- **THEN** the CLI exits with a typer mutex error before any state mutation

#### Scenario: TTY no-flag prompts
- **WHEN** `workspace remove foo bar` is invoked in a TTY with neither flag
- **THEN** the CLI prompts the operator; on `Y` it runs the backup-then-remove flow; on `n` it runs the purge flow

#### Scenario: Non-TTY no-flag refuses
- **WHEN** `workspace remove foo bar` is invoked in a non-TTY context with neither flag
- **THEN** the CLI exits with: "Use --backup or --purge to specify removal mode in non-interactive contexts." and exit code 1

#### Scenario: Backup failure aborts removal
- **WHEN** `workspace remove foo bar --backup` is invoked and the rsync backup fails partway
- **THEN** the workspace tree is NOT removed; sandbox.toml is NOT modified; the partial backup at `<ts>.partial/` is retained for diagnosis

#### Scenario: Successful remove drops sandbox.toml entry
- **WHEN** `workspace remove foo bar --purge` completes successfully
- **THEN** `sandbox.toml` no longer contains `[workspaces.bar]`; `~/.sandbox-ai/workspaces/foo/bar/` no longer exists

#### Scenario: Last workspace removal warns
- **WHEN** `workspace remove foo bar` completes and `bar` was the only workspace in `foo`
- **THEN** the CLI emits a warning that `sandbox start foo` will fail until a workspace is added

### Requirement: workspace rename Command

`sandbox workspace rename <inst> <old> <new>` SHALL rename a workspace via three coordinated edits: an on-disk dir rename, a sandbox.toml section-key rename, and a sandbox.toml `path` field update. The implementation SHALL exploit `os.rename`'s atomic same-filesystem semantics so that POSIX ACLs (xattrs), ownership, mode bits, setgid bit, and persistent default ACL all carry over without re-applying.

Phases:

```
[R1] gates:
     • instance exists in registry
     • instance STOPPED
     • <old> exists in [workspaces]
     • <new> passes name regex/reserved/length validation
     • <new> NOT already in [workspaces]
     • <old> != <new> (rename to same name → reject; don't silently no-op)
[R2] os.rename(workspaces/<inst>/<old>, workspaces/<inst>/<new>)
     — atomic; xattrs/ACLs/setgid preserved
     — if EXDEV (cross-filesystem): reject explicitly with operator guidance
[R3] sandbox.toml: rewrite [workspaces.<old>] section key → [workspaces.<new>],
     update `path` field from .../<old> to .../<new>
```

R3 SHALL be idempotent on retry: a follow-up invocation with the same arguments after a partial-rename failure resumes correctly.

#### Scenario: Same-fs rename preserves ACL state
- **WHEN** `workspace rename foo old-name new-name` runs against a workspace with mode 2770 sb-ws + named ACL + persistent default ACL
- **THEN** after the rename, the new path has identical mode bits, named ACLs, and default ACL; no re-application by sandbox-ai is needed

#### Scenario: Cross-fs rename rejected with EXDEV
- **WHEN** `os.rename` raises `OSError` with `errno.EXDEV`
- **THEN** the command rejects with a clear error message indicating cross-filesystem rename is unsupported

#### Scenario: Same-name rename rejected
- **WHEN** `workspace rename foo main main` is invoked
- **THEN** the command rejects (do not silently no-op; surface typo)

#### Scenario: Name collision rejected
- **WHEN** `workspace rename foo old new` is invoked and `new` already exists in `[workspaces]`
- **THEN** the command rejects before any state mutation

#### Scenario: Re-running completed rename rejects clearly
- **WHEN** `workspace rename foo old new` is invoked after a successful prior rename (so `<old>` is no longer in `[workspaces]`)
- **THEN** the command rejects with: "workspace `<old>` not found in [workspaces]" — clear signal the rename already happened

### Requirement: workspace restore Command

`sandbox workspace restore <dest-inst> <dest-ws-name> [--from <backup-spec>]` SHALL restore a workspace from a backup. The `<backup-spec>` SHALL be parsed as one of three forms:

- Omitted: pick the latest backup whose source `<ws-name>` matches `<dest-ws-name>`, across all source instances. If multiple candidates exist, refuse with a list and exit code 1 (operator must disambiguate via `--from`).
- `<src-inst>/<src-ws>`: pick the latest backup of that source pair.
- `<src-inst>/<src-ws>/<ts>`: fully qualified specification.

Phases:

```
[X1] gates:
     • dest instance exists in registry
     • dest instance STOPPED
     • dest workspace name passes validation
     • dest workspace name NOT already in [workspaces] (no-overwrite policy)
     • <backup-spec> resolves to exactly one backup tree
     • acquire state.lock + <dest-inst>.backup.lock
[X2] internally invokes workspace add semantics with --copy:
     • mkdir -p workspaces/<dest-inst>/<dest-ws>/   0700 dev:dev
     • rsync -a --no-owner --no-group --safe-links <backup>/<tree>/ <ws-dir>/
       (defense in depth — backup is already stripped, but apply uniformly)
[X3] sandbox.toml gains [workspaces.<dest-ws-name>] with bootstrap_mode="copy",
     source = "<backup-path>" (the full backup-tree path, recorded for audit),
     path = ".../<dest-inst>/<dest-ws>"
[X4] release locks; next sandbox start triggers shared-group recipe on the new tree
```

The no-overwrite policy: restore refuses if `<dest-ws-name>` already exists in `<dest-inst>`. Operator must `workspace remove` first.

#### Scenario: Restore with omitted --from picks latest matching name
- **WHEN** `workspace restore foo main` is invoked and exactly one backup exists with source workspace name `main`
- **THEN** the latest such backup is restored into `foo` as workspace `main`

#### Scenario: Ambiguous --from omission refuses
- **WHEN** `workspace restore foo main` is invoked and backups for source workspace name `main` exist under multiple instances
- **THEN** the command refuses with a list of candidates and exit code 1

#### Scenario: --from accepts <src-inst>/<src-ws>
- **WHEN** `workspace restore foo main --from oldfoo/main` is invoked
- **THEN** the latest backup matching `_backups/oldfoo/main/<ts>/` is restored

#### Scenario: --from accepts <src-inst>/<src-ws>/<ts>
- **WHEN** `workspace restore foo main --from oldfoo/main/2026-05-06-12-00-00` is invoked
- **THEN** the exact backup at `_backups/oldfoo/main/2026-05-06-12-00-00/` is restored

#### Scenario: No-overwrite policy enforced
- **WHEN** `workspace restore foo main --from <spec>` is invoked and `foo` already has a workspace named `main`
- **THEN** the command rejects with guidance to `workspace remove main` first

### Requirement: workspace list Command

`sandbox workspace list <inst> [--no-backups] [--json]` SHALL display the live workspaces and (by default) the available backups for the instance. The default human-readable output includes two sections: `Live workspaces` and `Backups (<count>)`. Backups are listed for ALL instances whose names match `<inst>` *and* (post-MVP) for any other instance whose backups are available; for MVP, list only backups whose source instance matches `<inst>`.

Output columns:
- Live: NAME, MODE (`copy`/`empty`), PATH.
- Backups: ID (`<src-inst>/<src-ws>/<ts>`), SIZE, AGE.

Flags:
- `--no-backups`: suppress the Backups section.
- `--json`: emit structured JSON containing both sections.

#### Scenario: Default output lists live + backups
- **WHEN** `workspace list myinst` is invoked
- **THEN** the output contains a `Live workspaces:` section enumerating the workspaces in `[workspaces]` and a `Backups (<n>):` section enumerating the backup IDs for `myinst`

#### Scenario: --no-backups suppresses backup section
- **WHEN** `workspace list myinst --no-backups` is invoked
- **THEN** the output contains only the `Live workspaces:` section

#### Scenario: --json emits structured output
- **WHEN** `workspace list myinst --json` is invoked
- **THEN** the output is valid JSON with top-level keys `workspaces` (array of `{name, bootstrap_mode, path}`) and `backups` (array of `{id, source_instance, source_workspace, timestamp, size_bytes}`)

### Requirement: Workspace Backup Filesystem Layout

Backups SHALL live under `~/.sandbox-ai/workspaces/_backups/<inst>/<ws>/<YYYY-MM-DD-HH-MM-SS>/` where the timestamp is in **UTC**. The `_backups/` parent SHALL be lazily created on first backup with mode `0700` dev:dev. Per-instance and per-workspace parents are created with mode `0700` dev:dev as needed.

Each backup directory SHALL contain:
- A `.backup-info.json` metadata sidecar (per the "Backup Metadata Schema" requirement).
- The copied tree (rsync output per the "Workspace Backup Recipe" requirement).

#### Scenario: Backup root layout
- **WHEN** a backup is created for instance `myinst` workspace `main` at UTC time `2026-05-06T12:00:00Z`
- **THEN** the backup tree is at `~/.sandbox-ai/workspaces/_backups/myinst/main/2026-05-06-12-00-00/` with mode `0700` dev:dev

#### Scenario: _backups/ is reserved
- **WHEN** any operator attempts to create an instance named `_backups`
- **THEN** the operation rejects (per `instance-workspace-model`'s reserved-name rules)

#### Scenario: Lazy creation
- **WHEN** the first backup is created on a host with no existing `_backups/` directory
- **THEN** `~/.sandbox-ai/workspaces/_backups/` is created with mode `0700` dev:dev

### Requirement: Backup Metadata Schema

Each backup tree SHALL contain a `.backup-info.json` file at the top level with the following schema:

```json
{
  "schema_version": 1,
  "source_instance": "<inst>",
  "source_workspace": "<ws>",
  "source_bootstrap_mode": "copy" | "empty",
  "source_path": "<absolute path to source workspace at backup time>",
  "created_at_utc": "<ISO-8601 UTC timestamp>",
  "size_bytes": <integer>,
  "file_count": <integer>,
  "sandbox_ai_version": "<from package metadata>",
  "rsync_excludes_applied": [<list of exclude patterns>],
  "stripped_unsafe_links_count": <integer>,
  "tooling": {
    "rsync_version": "<string>",
    "rsync_xattrs_supported": <boolean>
  }
}
```

`workspace list` and `workspace restore` SHALL parse `.backup-info.json` to enrich their output and resolve backup specs. The metadata is informational; restoration does NOT depend on it being present (the source tree path is sufficient).

#### Scenario: Metadata file written after rsync
- **WHEN** the backup recipe completes its rsync phase
- **THEN** `.backup-info.json` is written into the `<ts>.partial/` directory before the atomic rename to `<ts>/`

#### Scenario: Metadata schema validates
- **WHEN** `.backup-info.json` is read by `workspace list` or `workspace restore`
- **THEN** the contents match the documented schema; missing fields default to null/empty

### Requirement: Workspace Backup Recipe

The backup recipe SHALL invoke rsync with the following flag set:

```
rsync -aHXS \
      --no-owner --no-group \
      --chmod=Du+rwx,Dg-s,Dgo-rwx,Fu+rw,Fgo-rwx,Fa-st \
      <default-excludes> [<extra-excludes>] \
      <src>/ <dest>.partial/
```

With `--no-owner --no-group`, the destination files inherit the rsync
invoker's user and primary group. To force the backup tree to the dev
user's **primary** group regardless of the invoker's effective primary
(defensive against the corner case where dev has the bridge group set as
their primary), the recipe SHALL walk `<dest>.partial/` after rsync
returns and `os.chown(path, -1, dev_primary_gid)` on every entry. The
group walk is part of the same atomic creation phase: it runs before the
metadata write and the `os.rename(.partial, final)` finalization.

The originally-drafted `--group=<dev-primary-gid>` rsync flag is **not a
real rsync option** (rsync 3.x rejects it as a parse error); the
post-rsync chown walk provides the equivalent guarantee without
filesystem-flag ambiguity.

The `-X` flag SHALL be guarded by a runtime probe (`rsync_supports_xattrs()`); if rsync does not support xattrs, the flag is omitted with a doctor warning. The `-S` flag preserves sparse-file representation. The `-A` flag SHALL NOT be present (ACLs are intentionally NOT carried; the `-a` flag does NOT include ACLs).

The default-excludes are the same set as the `--copy` codepath. The recipe SHALL accept `--no-default-excludes` and `--exclude=<glob>` flags for the rare full-tree-backup case.

The resulting tree SHALL have:
- Owner dev:dev's-primary-group (regardless of source ownership).
- Directories: mode `0700` (no setgid, no group/other rwx).
- Files: mode `0600` or `0700` (no setuid/setgid/sticky, no group/other rwx, executable bit preserved on owner).
- No POSIX ACLs (named entries or default ACLs).

Symlinks pointing outside the source root SHALL be auto-stripped with a warning logged and counted in `.backup-info.json` (`stripped_unsafe_links_count`). This inverts the `--copy` default (which refuses) because backups are recovery-context safety nets where refusal would be operator-hostile.

#### Scenario: Backup tree has stripped permissions
- **WHEN** a workspace at mode 2770 sb-ws + named ACL is backed up
- **THEN** the resulting backup tree has dirs at mode 0700, files at mode 0600/0700, ownership dev:dev's-primary-group, and no ACL entries

#### Scenario: -X xattrs probe gates the flag
- **WHEN** the host's rsync does not support `-X` (e.g., older builds)
- **THEN** the rsync invocation omits `-X`; doctor surfaces a warning that user xattrs are not preserved in backups

#### Scenario: -A flag never used
- **WHEN** the rsync invocation is constructed
- **THEN** the `-A` flag (preserve ACLs) is NEVER added; backups intentionally drop ACL state

#### Scenario: Unsafe symlinks auto-stripped with warning
- **WHEN** the workspace tree contains a symlink whose target escapes the workspace root
- **THEN** the rsync invocation includes `--safe-links`; the link is dropped from the backup; the count is logged and recorded in `.backup-info.json`

#### Scenario: Default-excludes applied to backup
- **WHEN** the workspace tree contains `node_modules/` and `.venv/` directories
- **THEN** these are NOT copied into the backup (same default-excludes as `--copy`)

### Requirement: Atomic Backup Creation

The backup recipe SHALL create the backup tree atomically: rsync writes into `<dest>.partial/`, the metadata is written, and the directory is then renamed to `<dest>/` only after all writes succeed. If rsync fails partway:

- The `<dest>.partial/` directory is retained for diagnosis.
- `sandbox.toml` is NOT mutated.
- The atomic rename to `<dest>/` does NOT occur, so `workspace list` and `workspace restore` see the partial as not-yet-complete.

The doctor check `backups_partial_dirs_present` (warn) flags `*.partial/` directories older than 1 hour.

#### Scenario: Atomic rename on success
- **WHEN** rsync and metadata write both succeed
- **THEN** `os.rename` moves `<dest>.partial/` → `<dest>/`; the final tree is visible to subsequent `workspace list` calls

#### Scenario: Partial retained on failure
- **WHEN** rsync fails partway (e.g., disk full)
- **THEN** `<dest>.partial/` is retained; no atomic rename occurs; the failure is logged with operator-readable error

#### Scenario: Doctor flags stale partials
- **WHEN** `sandbox doctor` runs and a `*.partial/` directory exists older than 1 hour
- **THEN** the `backups_partial_dirs_present` check emits a warning identifying the stale partial(s) and recommending manual cleanup

### Requirement: Per-Instance Backup Lock

The backup recipe SHALL acquire a per-instance backup lock at `~/.sandbox-ai/state/<inst>.backup.lock` before the long-running rsync phase. The lock file SHALL contain the orchestrator's PID and start timestamp for stale-detection. The lock SHALL be acquired with `LOCK_NB`; concurrent backups on the same instance fail-fast.

The phase ordering SHALL release the per-user `state.lock` for the duration of rsync and reacquire it for the final mutation steps:

```
1. Acquire state.lock (LOCK_NB).
2. Validate gates; instance must be stopped.
3. Acquire <inst>.backup.lock (LOCK_NB). Fail-fast if held.
4. Write {pid, started_at_utc} into <inst>.backup.lock.
5. RELEASE state.lock.    ← other ops on OTHER instances proceed
6. Run rsync (long).
7. Write .backup-info.json into <ts>.partial/.
8. Reacquire state.lock.
9. Atomic rename of .partial → final.
10. Apply toml/registry mutations (workspace remove, destroy, etc.).
11. Release state.lock and <inst>.backup.lock.
```

Other commands (`start`, `stop`, `workspace add/remove/rename/restore`, `destroy`) SHALL check `<inst>.backup.lock` after acquiring `state.lock` and refuse fast if held. Doctor checks `pid`-alive for stale detection; fcntl auto-releases on process exit. The grace period before declaring a lock stale SHALL be 60 seconds.

#### Scenario: Lock acquired before rsync
- **WHEN** the backup recipe begins
- **THEN** `<inst>.backup.lock` is acquired before any rsync invocation; the file contents include `{pid, started_at_utc}`

#### Scenario: Concurrent ops blocked while backup runs
- **WHEN** a backup is in progress and `sandbox start <inst>` is invoked
- **THEN** start fails fast with: "Backup in progress for <inst>; wait or `sandbox doctor` to inspect."

#### Scenario: state.lock released during rsync
- **WHEN** the backup recipe is in its rsync phase
- **THEN** `state.lock` is NOT held; concurrent operations on OTHER instances may acquire `state.lock` and proceed

#### Scenario: Stale lock detected and broken
- **WHEN** doctor or a lock-acquire path encounters a `<inst>.backup.lock` file whose PID is not alive
- **THEN** the lock is broken (file removed) with a logged warning, after a 60-second grace period from `started_at_utc`

### Requirement: Backup Disk-Pressure and Partial-Dir Doctor Checks

The doctor SHALL include two warn-only checks specific to backups:

- `backups_disk_pressure`: warn if `~/.sandbox-ai/workspaces/_backups/` total size exceeds 5 GB OR entry count (number of `<ts>/` dirs across all `<inst>/<ws>/`) exceeds 50. Recommend manual `rm -rf` of stale backups.
- `backups_partial_dirs_present`: warn if any `*.partial/` directory in `_backups/` is older than 1 hour. Recommend manual cleanup or auto-cleanup with operator approval (post-MVP).

Both are warn-only; doctor exits with code 0 on these warnings alone.

#### Scenario: Disk pressure warning at threshold
- **WHEN** `sandbox doctor` runs and `_backups/` total size exceeds 5 GB
- **THEN** `backups_disk_pressure` emits a warning identifying the size and recommending cleanup

#### Scenario: Entry count warning at threshold
- **WHEN** `sandbox doctor` runs and `_backups/` contains more than 50 backup entries
- **THEN** `backups_disk_pressure` emits a warning identifying the count

#### Scenario: Stale partial warning
- **WHEN** `sandbox doctor` runs and a `*.partial/` directory exists in `_backups/` older than 1 hour
- **THEN** `backups_partial_dirs_present` emits a warning identifying the stale partial path

### Requirement: Backup Retention is Manual for MVP

The system SHALL NOT automatically garbage-collect backups. Retention is the operator's responsibility, surfaced via the `backups_disk_pressure` doctor warning. Per-workspace retention caps (e.g., "keep last N") and time-based rotation are explicitly out of scope for MVP.

#### Scenario: Backups accumulate without auto-GC
- **WHEN** repeated backup operations create multiple backups for the same workspace
- **THEN** all backups are retained; sandbox-ai does NOT delete older entries automatically
