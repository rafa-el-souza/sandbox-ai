## Purpose

This specification defines the `sandbox destroy` command lifecycle, governing confirmation, opt-in workspace backups, unconditional volume teardown, IPAM/registry cleanup, and safe instance directory removal.
## Requirements
### Requirement: Explicit Instance Argument

`sandbox destroy <inst>` SHALL require the instance name as a positional argument.

#### Scenario: Instance argument required
- **WHEN** `sandbox destroy` is invoked without an `<inst>` argument
- **THEN** the CLI exits with a typer "missing argument" error

### Requirement: Backup-Workspaces Flag

The `sandbox destroy <inst>` command SHALL accept `--backup-workspaces=<spec>` where `<spec>` is one of:
- `all`: every workspace in the instance is backed up before destruction.
- `none`: no workspaces are backed up; equivalent to "purge all".
- `<csv>` (e.g., `foo,bar`): the named workspaces are backed up; unselected workspaces are purged.

The flag SHALL reject `all,foo` (cannot combine `all` with names). Unknown names in `<csv>` SHALL be rejected before any deletion.

In TTY mode without the flag, the system SHALL display an interactive Rich toggleable list letting the operator pick which workspaces to backup. Unselected workspaces are purged.

In non-TTY mode without the flag, the system SHALL refuse with: "destroy in non-interactive mode requires --backup-workspaces=...".

#### Scenario: --backup-workspaces=all backs up everything
- **WHEN** `sandbox destroy foo --backup-workspaces=all` is invoked
- **THEN** every workspace in `foo` is backed up; then all workspace trees are rmtree'd; then the instance is destroyed

#### Scenario: --backup-workspaces=none equivalent to purge-all
- **WHEN** `sandbox destroy foo --backup-workspaces=none` is invoked
- **THEN** no backups are created; all workspace trees are rmtree'd; the instance is destroyed

#### Scenario: --backup-workspaces=<csv> backs up named, purges rest
- **WHEN** `sandbox destroy foo --backup-workspaces=main,backend` is invoked and `foo` has workspaces `main`, `backend`, `scratch`
- **THEN** `main` and `backend` are backed up; all three workspace trees are rmtree'd

#### Scenario: --backup-workspaces=all,foo rejected
- **WHEN** `sandbox destroy foo --backup-workspaces=all,foo` is invoked
- **THEN** the CLI exits with: "cannot combine 'all' with named workspaces"

#### Scenario: Unknown workspace name rejected
- **WHEN** `sandbox destroy foo --backup-workspaces=missing` is invoked and `foo` has no workspace named `missing`
- **THEN** the CLI exits with a "workspace not found" error before any deletion

#### Scenario: TTY no-flag prompts interactively
- **WHEN** `sandbox destroy foo` is invoked in a TTY without the flag
- **THEN** the CLI displays a Rich toggleable list of workspaces; on confirmation, selected workspaces are backed up

#### Scenario: Non-TTY no-flag refuses
- **WHEN** `sandbox destroy foo` is invoked in non-TTY context without the flag
- **THEN** the CLI exits with: "destroy in non-interactive mode requires --backup-workspaces=..." and exit code 1

### Requirement: Phase Order Preserves Recoverability Through Backup

`sandbox destroy <inst>` SHALL execute phases in an order that preserves recoverability through the backup phase. Up to and including D4, the operator may abort and run `sandbox start <inst>` to bring the instance back; from D5 onward, mutations are irreversible.

```
[D1]  confirmation (typed name) unless --force
      TTY + no flag: interactive Rich toggleable list selects backup set
      Non-TTY + no flag: refuse
[D2]  acquire state.lock + <inst>.backup.lock
[D3]  compose down (REVERSIBLE — operator can `sandbox start` if abort)
[D4]  for each workspace selected for backup: rsync to .partial/, atomic rename
      If any backup fails: ABORT destroy, partial retained, no irreversible
      mutation. Operator retries or aborts.
[D5]  compose down -v (IRREVERSIBLE from here)
[D6]  revoke ACLs symmetric with stop (per workspace)
[D7]  shutil.rmtree(instances/<inst>/)
[D8]  shutil.rmtree(workspaces/<inst>/<ws>/) for ALL workspaces
      (backups are separate trees; originals always rmtree'd at destroy)
[D9]  try: rmdir(workspaces/<inst>/) (succeeds iff empty)
[D10] IPAM release
[D11] registry remove
[D12] release locks
```

#### Scenario: Backup failure aborts destroy without irreversible mutation
- **WHEN** `sandbox destroy foo --backup-workspaces=all` is invoked and a backup fails partway at phase D4
- **THEN** the destroy aborts; `compose down -v` is NOT run; instance dir is NOT removed; partial backup at `<ts>.partial/` is retained; operator may retry destroy or `sandbox start foo`

#### Scenario: Phase D5 onward is irreversible
- **WHEN** destroy reaches phase D5 (`compose down -v`)
- **THEN** all subsequent phases (revoke, rmtree, IPAM release, registry remove) execute even if individual operations fail (best-effort cleanup with warnings)

#### Scenario: Workspace trees rmtree'd at destroy regardless of backup status
- **WHEN** destroy completes phase D4 with selected backups created
- **THEN** ALL workspace trees (backed up or not) are rmtree'd in phase D8; the backups exist as independent copies under `_backups/`

### Requirement: Confirmation Required Before Destruction
The system SHALL require explicit confirmation before executing any irreversible destroy operation. Confirmation is via typing the instance name unless `--force` is passed.

#### Scenario: Name-typing confirmation prevents accidental destroy
- **WHEN** `sandbox destroy <inst>` is invoked without `--force` and the user types a name that does not match `<inst>`
- **THEN** the CLI aborts with no state changes and exits 0

#### Scenario: Force flag bypasses confirmation
- **WHEN** `sandbox destroy <inst> --force` is invoked
- **THEN** the CLI proceeds without prompting for name confirmation (interactive backup-selection still runs in TTY mode without `--backup-workspaces`)

### Requirement: Destroy Phase Ordering
The system SHALL execute destroy phases in dependency order: hard resources (containers, ACLs, directory) before soft state (IPAM, registry). This ordering SHALL ensure that partial failures leave the instance in a retryable state. The detailed phase order is specified by the "Phase Order Preserves Recoverability Through Backup" requirement.

#### Scenario: Phase execution order
- **WHEN** `sandbox destroy <inst>` executes after confirmation and lock acquisition
- **THEN** phases execute per the "Phase Order Preserves Recoverability Through Backup" sequence: compose down → backup → compose down -v → ACL revoke → rmtree(instance) → rmtree(workspaces) → IPAM release → registry remove

#### Scenario: rmtree failure preserves registry and IPAM
- **WHEN** `shutil.rmtree(instance_dir)` fails with `PermissionError`
- **THEN** IPAM release and registry removal are NOT attempted, and the operator can retry `sandbox destroy <inst>`

#### Scenario: IPAM/registry failure after rmtree
- **WHEN** `shutil.rmtree` succeeds but IPAM release or registry removal fails
- **THEN** the failure is reported as a warning (primary resources — instance dir and workspaces — are already removed)

### Requirement: Compose Environment File on Destroy
The system SHALL pass `--env-file <instance_dir>/.sandbox.env` to the `docker compose down -v` invocation during destroy.

#### Scenario: --env-file on compose down during destroy
- **WHEN** `_compose_down` constructs the compose command during destroy
- **THEN** the command includes `--env-file <instance_dir>/.sandbox.env`

### Requirement: Unconditional Volume Teardown
The system SHALL run `docker compose down -v` regardless of whether containers are currently running. Teardown failure SHALL NOT abort subsequent destroy phases.

#### Scenario: Warm instance torn down completely
- **WHEN** `sandbox destroy` is confirmed and containers are running
- **THEN** `docker compose down -v` removes all containers, networks, and named volumes

#### Scenario: Cold instance volumes removed idempotently
- **WHEN** `sandbox destroy` is confirmed and containers are already stopped
- **THEN** `docker compose down -v` still removes any remaining named volumes and networks without error

#### Scenario: Compose teardown failure continues destroy
- **WHEN** `docker compose down -v` raises `SandboxExecutionError` (e.g., no compose file, Docker daemon unreachable)
- **THEN** the error is reported as a warning and destroy proceeds to ACL revocation

### Requirement: IPAM and Registry Cleanup
The system SHALL free the IPAM `base_index` slot and remove the registry entry as part of destroy, enabling both to be reused by future instances. Each cleanup operation SHALL be independently fault-isolated.

#### Scenario: IPAM slot freed after destroy
- **WHEN** `sandbox destroy` reaches the IPAM release phase
- **THEN** the `instance_id` entry is removed from `ipam.json` and that `base_index` is available for allocation to a new instance

#### Scenario: Registry entry removed after destroy
- **WHEN** `sandbox destroy` reaches the registry removal phase
- **THEN** the `<inst>` entry is removed from `instances.json`

#### Scenario: IPAM release failure reported as warning
- **WHEN** IPAM release raises an exception
- **THEN** the error is reported as a warning and registry removal is still attempted

#### Scenario: Registry removal failure reported as warning
- **WHEN** registry removal raises an exception
- **THEN** the error is reported as a warning

### Requirement: Safe Instance Directory Removal
The system SHALL verify the instance directory path before deletion. The path prefix guard SHALL ensure `instance_dir` starts with `<sandbox_ai_home()>/instances/` before invoking `shutil.rmtree`. Workspace trees and backup trees live under `<sandbox_ai_home()>/workspaces/`; the system SHALL handle idempotent destroy gracefully.

#### Scenario: Path prefix guard prevents runaway rmtree
- **WHEN** `sandbox destroy <inst>` is about to call `shutil.rmtree(instance_dir)`
- **THEN** the CLI first asserts that `instance_dir` starts with `<sandbox_ai_home()>/instances/`; if not, it aborts with an error

#### Scenario: Workspaces always rmtree'd at destroy
- **WHEN** `sandbox destroy <inst>` completes
- **THEN** every workspace tree under `~/.sandbox-ai/workspaces/<inst>/<ws>/` is rmtree'd (regardless of whether a backup was created); backups under `~/.sandbox-ai/workspaces/_backups/<inst>/<ws>/<ts>/` are preserved

#### Scenario: Already-deleted directory handled silently
- **WHEN** `shutil.rmtree(instance_dir)` raises `FileNotFoundError`
- **THEN** the error is suppressed (directory already removed = success condition)

#### Scenario: Permission error propagates
- **WHEN** `shutil.rmtree(instance_dir)` raises `PermissionError`
- **THEN** the error propagates to the caller (defense-in-depth — operator must investigate)

### Requirement: Per-User State Initialization Required
The `sandbox destroy` command SHALL refuse to operate when the per-user state tree is not initialized. Initialization is signaled by the presence of `<sandbox_ai_home()>/state/instances.json`. On absence, the command SHALL exit with a clear error directing the operator to run `sandbox init`.

#### Scenario: Destroy on uninitialized host
- **WHEN** `sandbox destroy` is invoked and `<home>/state/instances.json` does not exist
- **THEN** the CLI exits with: "Error: per-user state not initialized at `<resolved-home>`. Run `sandbox init` first." and exit code 1

#### Scenario: Resolved home in error message
- **WHEN** the destroy command above runs with `SANDBOX_AI_HOME=/tmp/test-home` set
- **THEN** the error message contains `/tmp/test-home`

### Requirement: Destroy Removes Instance Tree With Helper-Recipe State

`sandbox destroy <inst>` SHALL remove `<sandbox_ai_home()>/instances/<inst>/` via `shutil.rmtree`, which transitively removes:
- Cache/log leaves (subuid-owned, with inherited `u:dev:rwx` letting dev's `rmtree` succeed)
- Ro single-files (`<consumer-uid>:<consumer-gid>` owned, removed via parent dir write+x which dev has via existing ACLs)
- Secrets (`<consumer-uid>:<consumer-gid>` mode 0600, same removal path)
- All other instance state

No explicit helper-recipe revocation is invoked; the rmtree handles it. Workspace trees are rmtree'd separately (per the "Phase Order Preserves Recoverability Through Backup" sequence's D8); they live under `<sandbox_ai_home()>/workspaces/<inst>/<ws>/`, not under the instance dir.

#### Scenario: Destroy via rmtree handles subuid-owned files in instance dir
- **WHEN** `sandbox destroy <inst>` runs `shutil.rmtree(<sandbox_ai_home()>/instances/<inst>/)`
- **THEN** the operation succeeds despite cache/log files being subuid-owned; dev's inherited `u:dev:rwx` ACL on agent-created files plus parent-dir write+x permissions are sufficient for unlink/rmdir

#### Scenario: Destroy via rmtree handles consumer-owned files in instance dir
- **WHEN** `sandbox destroy <inst>` runs `shutil.rmtree(<sandbox_ai_home()>/instances/<inst>/)`
- **THEN** the operation succeeds despite ro files being `<consumer-uid>:<consumer-gid>` owned (consumer's host subuid + host subgid pair, per `orchestrator-volumes`'s `Ro single-files on-disk gid matches consumer's host subgid` scenario); dev has write+x on the parent dirs (own dirs in `instances/<inst>/`), which is what unlink requires

#### Scenario: Workspace trees removed separately from instance dir
- **WHEN** `sandbox destroy <inst>` reaches phase D8 (workspace rmtree)
- **THEN** `shutil.rmtree(<sandbox_ai_home()>/workspaces/<inst>/<ws>/)` is invoked for each workspace; these are a separate tree from the instance dir

