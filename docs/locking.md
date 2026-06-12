# State and locking

This page covers the orchestrator's on-disk state files and the per-user lock topology — including the acquisition-ordering rule that prevents the AB/BA self-deadlock class.

## State layout

Mutable orchestrator state lives under `<sandbox_ai_home()>/state/` (default `~/.sandbox-ai/state/`). The directory is created with mode `0700` by `sandbox init`. It holds:

| File | Purpose | Created by / mode |
| --- | --- | --- |
| `instances.json` | instance registry | `sandbox init` (dir mode `0700`) |
| `ipam.json` | IPAM ledger | `sandbox init` (dir mode `0700`) |
| `state.lock` | provisioning sequence atomicity (outer lock) | `sandbox init` (dir mode `0700`) |
| `ipam.json.lock` | IPAM ledger mutation lock | `sandbox init` (dir mode `0700`) |
| `instances.json.lock` | instance registry mutation lock | `sandbox init` (dir mode `0700`) |
| `<inst>.backup.lock` | per-instance backup mutex | lazy, per instance |

## Lock topology

- **Per-user lock topology** (four lock files; the topology is normative, source of truth is the `instance-registry` capability's "Registry Lock Safety" requirement):
  - `state.lock` — provisioning sequence atomicity. Held by lifecycle commands (`start`, `stop`, `destroy`, `workspace add/remove/rename/restore`) for the duration of their provisioning sequence. Per-user (not per-CWD): all `sandbox` invocations under the same user serialize on this lock regardless of working directory.
  - `ipam.json.lock` — IPAM ledger mutation lock (`core.host_config.ipam_lock_path()`). Held only inside `IPAMLedger.allocate` / `IPAMLedger.release`; the ledger never touches `state.lock`.
  - `instances.json.lock` — instance registry mutation lock (`core.host_config.registry_lock_path()`). Held only inside `InstanceRegistry.register` / `InstanceRegistry.remove`; the registry never touches `state.lock`.
  - `<inst>.backup.lock` — per-instance backup mutex (lazy per instance). Held during a backup operation's long phase; coordinates with `state.lock` via release-during-rsync.

## Acquisition ordering

**Lock acquisition ordering (load-bearing).** `state.lock` is OUTER. Resource locks (`ipam.json.lock`, `instances.json.lock`) are SIBLINGS of each other — they never nest with each other; they are inner to `state.lock` when called from a state.lock-holding caller. Per-instance backup locks are also siblings of the resource locks.

> **No code path may acquire `state.lock` while holding any inner lock.**

**Deadlock history.** Violations of this ordering produce the AB/BA self-deadlock class that surfaced historically in both the IPAM and registry paths; both fixes followed the "dedicate a lock file per resource" template.

## Lifecycle notes

- `state.lock` is **transient** — held only during provisioning, released for the runtime lifetime of a sandbox. Don't add long-lived locks.
- Lifecycle commands (`start`, `stop`, `destroy`, `status`, `attach`) hard-fail with a "run sandbox init first" error when `<home>/state/instances.json` is absent.
