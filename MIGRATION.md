# MIGRATION — change-4 → change-5 (`instance-workspace-model`)

## Scope

Pre-release scope: no in-place migration. Operators destroy every existing
change-4 instance and re-init under the new layout. Doctor surfaces stale
state with warn-level checks (`legacy_sandboxes_dir_detected`,
`legacy_workspace_in_user_project_root`, `legacy_registry_shape`) plus
copy-pasteable remediation.

## Steps

### 1. Detect legacy state

```bash
sandbox doctor
```

The relevant warnings will fire:
- `legacy sandboxes dir detected` — `<cwd>/sandboxes/` from pre-change-2
- `legacy user_project_root field` — `[instance].user_project_root` in any
  registered instance's `sandbox.toml`
- `legacy registry shape` — path-keyed entries in
  `~/.sandbox-ai/state/instances.json`

### 2. Rename env var if you have shell rc / scripts

`SANDBOX_AI_USER_HOME` → `SANDBOX_AI_HOME`. Atomic rename — there is no
transition alias. Update any `~/.bashrc`, `~/.zshrc`, CI scripts, and
direnv `.envrc` references.

### 3. Destroy every existing instance

```bash
sandbox destroy <inst>
# repeat per registered instance
```

### 4. Re-init under the new layout

```bash
# Default — single empty workspace named `main`
sandbox init <inst>

# Or import a host-local checkout
sandbox init <inst> --copy <ws-name>=/path/to/checkout

# Or scaffold multiple workspaces in one call
sandbox init backend-stack \
  --copy api=/home/dev/repos/api \
  --copy web=/home/dev/repos/web \
  --empty scratch
```

Workspace trees now live under
`~/.sandbox-ai/workspaces/<inst>/<workspace-name>/`. Inside the agent
container, each workspace is bind-mounted at `/workspaces/<workspace-name>`
(plural, no longer `/workspace`).

### 5. Per-stack rebuild after `--copy`

The copy excludes build artifacts and dependency caches by default
(`node_modules/`, `.venv/`, `target/`, etc.). After `sandbox start <inst>`
the agent should rebuild the stack inside the container:

| Stack | Command |
|---|---|
| Node (npm) | `npm install` |
| Node (pnpm) | `pnpm install` |
| Node (yarn) | `yarn install` |
| Python (uv) | `uv sync` |
| Python (pip) | `python -m venv .venv && .venv/bin/pip install -r requirements.txt` |
| Python (poetry) | `poetry install` |
| Rust | `cargo build` |
| Go | `go build ./...` |
| Java (gradle) | `./gradlew build` |
| Ruby | `bundle install` |
| PHP | `composer install` |

### 6. Workspace bridge group

Manual setup unchanged from change 4 — until `sandbox setup` ships as a
separate change. Doctor's `workspace_bridge_group_exists` failure path
prints copy-pasteable `groupadd` / `usermod` commands; re-login (or
`newgrp sb-ws`) after `usermod` so the new supplementary group reaches the
shell process.

## Behavior changes worth a release note

- Doctor checks `secrets_hydrated_restrictively` and
  `pre_existing_instance_layout` start working in wheel installs (closes
  change-4's deferred behavior — pre-change-5 they `SKIP`ped because
  `__file__`-based scanning didn't reach a `sandboxes/` tree under
  `site-packages/`).
- `sandbox destroy` flag grammar: `--remove-workspaces` is gone, replaced
  by `--backup-workspaces=all|none|<csv>`. Default destroy purges; backup
  is opt-in. Non-TTY destroy refuses without the flag.
- Lifecycle commands (`start`, `stop`, `attach`, `status`, `destroy`,
  `workspace ...`) require an explicit `<inst>` argument. CWD-based
  discovery is gone.
- Instance name length cap tightened from 32 to 30 chars to keep
  `<dev>-<inst>-<service>-<idx>` under docker's 64-char container-name
  limit.

## Rollback

`git revert` the change. Pre-change-5 instances that were destroyed during
migration are gone; if you want them back, re-init under the reverted code
the same way you would forward (no in-place mutation happened in either
direction).

## Out-of-band full-fidelity backup (operator-managed)

`sandbox`'s built-in backups (`workspace remove --backup`,
`destroy --backup-workspaces=...`) are **recovery aids** that intentionally
strip ACL/group/setgid/xattr state for portability across hosts and
filesystems. The recipe is `rsync -aHXS --no-owner --no-group
--group=<dev-primary> --chmod=Du+rwx,Dg-s,Dgo-rwx,Fu+rw,Fgo-rwx,Fa-st …`
plus default-excludes (build artifacts, caches). The resulting tree is
plain dev:dev mode 0700/0600.

For compliance, archival, or forensic needs that require full-fidelity
preservation (POSIX ACLs, named-user/group entries, setgid bits, security
xattrs / capabilities, SELinux contexts), use `tar` directly:

```bash
sudo tar --acls --xattrs --selinux \
         -cpf <name>.tar \
         -C ~/.sandbox-ai/workspaces/<inst>/ \
         <ws>/
```

To restore:

```bash
mkdir -p /tmp/restore-<ws>
sudo tar --acls --xattrs --selinux \
         -xpf <name>.tar \
         -C /tmp/restore-<ws>/

# Then fold the extracted tree into a stopped instance:
sandbox stop <dest-inst>
sandbox workspace add <dest-inst> --copy <ws>=/tmp/restore-<ws>/<ws>
```

This workflow is **outside** the orchestrator — it requires `sudo` (the
orchestrator never invokes sudo) and produces archives that
`sandbox-ai`'s built-in restore commands cannot consume. It is an
operator-managed escape hatch, deliberately distinct from the
recovery-aid backups.
