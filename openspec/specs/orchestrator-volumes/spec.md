## Purpose

This specification governs the absolute filesystem boundary constraints separating the Human Host repository from the containerized execution environment. It enforces structural mitigations resolving the rootless SubUID paradox via a lifecycle × mechanism taxonomy, establishes topological separation between the immutable tooling plane and mutable per-instance plane, and dictates volume annihilation procedures.
## Requirements
### Requirement: Multi-Workspace Bind Mount Layout

The compose template SHALL emit one read-write bind mount per workspace, sourced from `<workspace.path>` and targeting `/workspaces/<workspace-name>:rw` on each agent service (core and admin). The bind-mount loop iterates `[workspaces]` (sorted by name lexicographically) for render determinism. The legacy single `/workspace` mount SHALL NOT be present.

#### Scenario: One bind mount per workspace per service
- **WHEN** the compose template is rendered for an instance with N workspaces
- **THEN** core and admin services each contain exactly N bind-mount entries of the form `<workspace.path>:/workspaces/<workspace-name>:rw`

#### Scenario: Render determinism via lexicographic sort
- **WHEN** the same instance's compose template is rendered twice
- **THEN** the resulting volume entries appear in identical order (sorted by workspace name)

#### Scenario: Legacy /workspace mount absent
- **WHEN** the rendered compose.yml is inspected
- **THEN** there is NO bind mount of any source to `/workspace` (singular) on any service

### Requirement: _backups Tree Excluded from ACL/Recipe Planning

The `~/.sandbox-ai/workspaces/_backups/` tree SHALL be excluded from any ACL grant plan, helper recipe phase, or shared-group recipe planning. Backup trees are dev-owned plain trees (mode 0700 dev:dev, no ACLs); the orchestrator does not apply runtime recipe state to them.

#### Scenario: _backups path absent from ACL grant plan
- **WHEN** `_acl_grant_plan()` is called for any instance
- **THEN** the returned plan contains NO entries for paths under `~/.sandbox-ai/workspaces/_backups/`

#### Scenario: _backups path absent from shared-group plan
- **WHEN** `_workspace_shared_group_plan()` is called
- **THEN** the returned plan contains NO entries for paths under `~/.sandbox-ai/workspaces/_backups/`

### Requirement: Walker Safety Rules Apply to All Ancestor Planning

The ACL ancestor walker SHALL apply the seven safety rules defined in the `instance-workspace-model` capability's "Walker Safety Rules" requirement (resolve realpath first, boundary stop list, reject targets in boundary, bound walk depth, lstat throughout, fault-isolated grant at execution, per-target walk with execution-side dedup). The walker is invoked once per workspace path (and once for the instance root); deduplication of overlapping ancestor chains happens at execution.

#### Scenario: Per-workspace walk emits independent plans
- **WHEN** the walker is invoked for instance `foo` with workspaces `main` and `scratch`
- **THEN** two walks occur (one per workspace path); the per-walk plans are merged with deduplication at execution

#### Scenario: Boundary path never granted
- **WHEN** any walk's ancestor chain would require a grant on `~/`, `/home`, or any other boundary-list path
- **THEN** the walker emits NO grant for that path; the situation is surfaced as a doctor failure

### Requirement: UID Paradox ACL Default Overrides

The system SHALL govern the `dev`/`<host_unprivileged_user>` filesystem boundary using a two-axis taxonomy: a **lifecycle** axis describing when an operation is applied and reversed, and a **mechanism** axis describing what host operation is performed. Each mount class is assigned a (lifecycle, mechanism) pair (or pairs, when a single mount carries multiple). The mapping is the source of truth for `_acl_grant_plan`, `_acl_revoke_plan`, and the helper-recipe phases.

**Lifecycle axis values:**
- `granted-at-start, revoked-at-stop` — operation applied during `sandbox start` (in `_phase_acl_grant`) and reversed during `sandbox stop` / `sandbox destroy` revocation.
- `granted-once, persistent` — operation applied once (typically at first start, idempotent on subsequent runs); never reversed by orchestrator.
- `applied-on-every-start, idempotent, never-revoked` — re-applied every start (idempotent in the steady state); never reversed by orchestrator; transitively undone only when the containing tree is removed by `sandbox destroy`.

**Mechanism axis values:**
- `named-acl` — `setfacl -m u:<user>:<perms>` and its reverse `setfacl -x`.
- `subuid-chown` — chown to the in-container consumer's host subuid (via `helper_mkdir_chown_dirs`).
- `consumer-uid-0-chown` — chown to `<consumer-uid>:<consumer-gid>` mode `<mode>` (via `helper_chown_files`). The mechanism name preserves the historic literal-0 gid reference for continuity; in practice the gid is the consumer's host subgid, paired with the consumer's host subuid that the recipe already produces. In-container root reads the resulting file via `cap_dac_override` (in the helper's cap-add baseline, equally available to a consumer-container entrypoint that starts as root before dropping privileges) — not via group ownership. The literal-0 gid pattern was incompatible with the host-absolute helper API contract (it falls outside any subgid range) and provided no protection that `cap_dac_override` doesn't already grant.
- `shared-group` — `chgrp <bridge-group> + chmod 2770 + setgid + persistent default ACL portion`.

**Per-mount-class recipe table** (the spec's source of truth):

| Mount class | Lifecycle | Mechanism |
| --- | --- | --- |
| Instance root, `docker/` (recursive), `config/` (dir-level traverse), `secrets/` (dir-level traverse), `.sandbox.env` | granted-at-start, revoked-at-stop | named-acl |
| Helper-recipe parents `cache/core`, `cache/admin`, `log/` (per `Helper-Recipe Parent ACL Grants`) | granted-at-start, revoked-at-stop | named-acl (`u:<host_user>:rwx` effective + matching default ACL) |
| Ancestor traverse `--x` chain (above the instance dir AND each workspace path, walking up to the ownership boundary; deduplicated at execution) | granted-once, persistent | named-acl |
| Cache/log dir leaves (per the four-leaf `Cache/Log Leaf Inventory`) | applied-on-every-start, idempotent, never-revoked | subuid-chown + parent default ACL `u:dev:rwx` |
| Ro single-files (Corefile, dnsdist conf, all 5 proxy files, core/admin dotfiles, sshd_config) | applied-on-every-start, idempotent, never-revoked | consumer-uid-0-chown (mode 0640) |
| Secrets (authorized_keys, ipc_*) | applied-on-every-start, idempotent, never-revoked | consumer-uid-0-chown (mode 0600) |
| Executable-script entrypoints (per `Executable-Script File Recipes`; current entry: `docker/core/entrypoint.sh`) | applied-on-every-start, idempotent, never-revoked | consumer-uid-0-chown (mode 0500 owner-only) |
| **Each workspace's** named ACL on its `path` (effective AND default-ACL named entries) | granted-at-start, revoked-at-stop | named-acl |
| **Each workspace's** shared-group state on its `path` (chgrp, chmod 2770+setgid, persistent default ACL portion `u::rwx,g::rwx,o::---,m::rwx,u:dev:rwx`) | granted-once, persistent | shared-group |
| `_backups/` tree (lazy-created on first backup, dev-owned mode 0700) | none | none (excluded from ACL/recipe planning per "_backups Tree Excluded" requirement) |

A single mount may carry multiple (lifecycle, mechanism) pairs. Each workspace is the load-bearing example: its named ACL is granted-at-start/revoked-at-stop, while its group/mode/persistent-default-ACL is granted-once/persistent.

#### Scenario: Lifecycle × mechanism taxonomy is the spec's source of truth
- **WHEN** any new mount class is added to the orchestrator
- **THEN** the spec assigns it one or more (lifecycle, mechanism) pairs from the table; ad-hoc mechanisms outside the taxonomy are NOT introduced

#### Scenario: Named-ACL grants — instance dirs — applied at start
- **WHEN** `sandbox start <inst>` reaches `_phase_acl_grant`
- **THEN** `setfacl -R -m u:<host_unprivileged_user>:rX` is applied to `instances/<inst>/docker/`; `setfacl -m u:<host_unprivileged_user>:rX` to `instances/<inst>/config/` (dir-level — individual files inside are chowned per the consumer-uid-0-chown class); `setfacl -m u:<host_unprivileged_user>:r-x` to `instances/<inst>/`; `setfacl -m u:<host_unprivileged_user>:r` to `instances/<inst>/.sandbox.env`

#### Scenario: Named-ACL grants — instance dirs — revoked at stop
- **WHEN** `sandbox stop <inst>` or `sandbox destroy <inst>` executes ACL revocation
- **THEN** `setfacl -x u:<host_unprivileged_user>` is applied to `instances/<inst>/docker/`, `instances/<inst>/config/`, `instances/<inst>/`, and `instances/<inst>/.sandbox.env`, using fault-isolated revocation

#### Scenario: Named-ACL grants — helper-recipe parents — applied at start
- **WHEN** `sandbox start <inst>` reaches `_phase_acl_grant`
- **THEN** for EACH helper-recipe parent in `("cache/core", "cache/admin", "log")`: `setfacl -m u:<host_user>:rwx <parent>` is applied (effective) AND a matching default ACL `u::rwx,g::rwx,o::---,m::rwx,u:<host_user>:rwx` is applied so children created inside inherit a daemon-rwx named entry

#### Scenario: Cache/log subuid-chown recipe — applied every start
- **WHEN** `sandbox start` reaches the cache/log helper-recipe phase (`_phase_helper_mkdir_chown_cache_log`, after `_phase_acl_grant`)
- **THEN** for each cache/log leaf in the `Cache/Log Leaf Inventory` (the four-leaf set including log/core and log/admin): the parent dir's default ACL is augmented (via `setfacl -d -m u::rwx,g::---,o::---,m::rwx,u:dev:rwx <parent>`) so dev retains rwx on agent-created files (additive over the Phase-`_phase_acl_grant` default ACL set on the same parent — named entries from both calls accumulate; base entries from the helper phase win on overlap); `helper_mkdir_chown_dirs` runs to ensure the leaf exists and is owned by `host_id_for_in_container(1000, host_user):host_gid_for_in_container(1000, host_user)`. Operation is idempotent: re-running on existing-correct state is a no-op.

#### Scenario: Cache/log subuid-chown — never revoked on stop
- **WHEN** `sandbox stop` executes
- **THEN** cache/log leaves remain subuid-owned; the default ACL on the parent is preserved; agent state is preserved across stop/start cycles

#### Scenario: Ro single-files consumer-uid-0-chown recipe — applied every start
- **WHEN** `sandbox start` reaches the ro-files helper-recipe phase (`_phase_helper_cp_chown_ro_files`, after `_phase_acl_grant`)
- **THEN** `helper_chown_files` is invoked once per (consumer-uid, mode) group, batching all files sharing the same target ownership/mode into a single helper container. The phase iterates entries from BOTH `RO_FILE_RECIPES` AND `EXEC_FILE_RECIPES` (per `Executable-Script File Recipes`)

#### Scenario: Ro single-files mapping table
- **WHEN** the consumer-uid-0-chown recipe is executed
- **THEN** files are chowned per this table (consumer in-container uid resolved via `host_id_for_in_container`; consumer in-container gid resolved via `host_gid_for_in_container` against the same in-container value):

  | File group | In-container uid | Mode |
  | --- | --- | --- |
  | `config/coredns/Corefile` | 65532 | 0640 |
  | `config/dnsdist/dnsdist.conf` | 953 | 0640 |
  | `config/proxy/{squid.conf,allowed_domains.txt,read_only_domains.txt,ERR_SANDBOX_403,.htpasswd}` | 13 | 0640 |
  | `config/core/{.bashrc,.npmrc,.gitconfig,CLAUDE.md,sshd_config}` | 1000 | 0640 |
  | `config/admin/{.zshrc,.tmux.conf,.gitconfig,gitmux.conf,starship.toml}` | 1000 | 0640 |
  | `secrets/{authorized_keys,ipc_host_key}` | 1000 | 0600 |
  | `secrets/{ipc_known_hosts,ipc_ssh_key}` | 1000 | 0600 |
  | `docker/core/entrypoint.sh` (executable-script kind, per `Executable-Script File Recipes`) | 1000 | 0500 |

#### Scenario: Ro single-files on-disk gid matches consumer's host subgid
- **WHEN** any file in the consumer-uid-0-chown recipe table has been chowned by `helper_chown_files`
- **THEN** the resulting on-disk gid is `host_gid_for_in_container(<in-container uid from table>, host_user)` — i.e., the consumer's host subgid paired with the consumer's host subuid; it is NOT the daemon user's primary gid (the historic literal-0 pattern was removed because it was incompatible with the host-absolute helper API and provided no protection that `cap_dac_override` doesn't already grant)

#### Scenario: Per-workspace named-ACL — applied at start
- **WHEN** `sandbox start <inst>` reaches `_phase_acl_grant` for an instance with workspaces `main` and `scratch`
- **THEN** for EACH workspace: `setfacl -m u:<host_unprivileged_user>:rwx <ws.path>` is applied (effective ACL); `setfacl -d -m u::rwx,g::rwx,o::---,m::rwx,u:<host_unprivileged_user>:rwx,u:dev:rwx <ws.path>` is applied (default ACL containing the host_user named entry)

#### Scenario: Per-workspace named-ACL revocation includes default ACL host_user entry
- **WHEN** `sandbox stop <inst>` or `sandbox destroy <inst>` executes ACL revocation
- **THEN** for EACH workspace: `setfacl -x u:<host_unprivileged_user> <ws.path>` removes the effective named entry AND `setfacl -d -x u:<host_unprivileged_user> <ws.path>` removes the default-ACL named entry; the persistent portion of each workspace's default ACL (`u::rwx, g::rwx, o::---, m::rwx, u:dev:rwx`) is preserved

#### Scenario: Per-workspace shared-group state — applied once, persistent
- **WHEN** `sandbox start <inst>` reaches `_phase_workspace_shared_group` and a workspace's root does NOT have setgid+correct-group state (drift detection signal — `os.stat(ws.path)` shows missing setgid bit OR group ownership ≠ `workspace_bridge_gid(host)`)
- **THEN** in-process recursive setup runs over `os.walk(<ws.path>)`: for every entry, `os.chown(path, -1, bridge_gid, follow_symlinks=False)` (best-effort; per-file EPERM on non-dev-owned files collected and reported in aggregate — the orchestrator does not escalate via sudo), then `os.chmod(path, 0o2770)` for directories and `os.chmod(path, 0o0660)` for non-symlink regular files. After the walk, the steady-state idempotent root setup runs on `<ws.path>` itself: `os.chown(ws.path, -1, bridge_gid, follow_symlinks=False)`, `os.chmod(ws.path, 0o2770)`, then subprocess `setfacl` (effective + default) installs the persistent default-ACL portion `u::rwx,g::rwx,o::---,m::rwx,u:<host_user>:rwx[,u:dev:rwx]` on `<ws.path>`. The recipe runs BEFORE `_phase_acl_grant` (per `Workspace Shared-Group Phase Ordering`), so `chmod 2770` lands on a non-extended-ACL inode and the workspace root's `group::rwx` entry propagates from the mode bits without a runtime `setfacl -m g::rwx` step. There are NO subprocess `chgrp -R`, `find -exec chmod`, or `setfacl -R` invocations during the recursive walk.

#### Scenario: Per-workspace shared-group state — steady-state idempotency
- **WHEN** `sandbox start <inst>` reaches the shared-group phase and a workspace's root already has setgid+correct-group
- **THEN** the recursive walk is skipped for that workspace; only root-state idempotent operations run (one `os.stat` for drift detection, then `os.chown`+`os.chmod`+`setfacl` on the root only)

#### Scenario: Per-workspace shared-group state — never revoked
- **WHEN** `sandbox stop <inst>` or `sandbox destroy <inst>` executes
- **THEN** chgrp, chmod 2770, setgid bit, and the persistent portion of the default ACL on EACH `<ws.path>` are NOT touched; these state properties are in the `granted-once, persistent` lifecycle

### Requirement: Ancestor Directory Traverse ACLs

The system SHALL grant execute-only (`--x`) ACLs on user-owned ancestor directories so the sandbox user can traverse from `/` to the instance directory and to each workspace directory. Ancestor ACLs are in the `granted-once, persistent` lifecycle: they are NOT revoked on stop or destroy. The walker applies the safety rules from `instance-workspace-model` (resolve realpath first, boundary stop list, depth bound, lstat throughout). Per-workspace walks are deduplicated at execution.

#### Scenario: Ancestor traverse granted at start
- **WHEN** `sandbox start <inst>` reaches Phase 5 (ACL grants)
- **THEN** `setfacl -m u:<host_unprivileged_user>:--x` is applied to each directory in the ancestor chain from the instance directory up to (but not including) the first directory not owned by the orchestrator UID, the first directory in the walker boundary list (per `instance-workspace-model`), or `/`

#### Scenario: Ancestor traverse granted on each workspace path
- **WHEN** `sandbox start <inst>` reaches Phase 5 (ACL grants) for an instance with multiple workspaces
- **THEN** `setfacl -m u:<host_unprivileged_user>:--x` is applied to each directory in the ancestor chain from EACH `workspace.path` up to (but not including) the first non-orchestrator-owned dir, the first boundary-list entry, or `/`. Overlapping chains across workspaces are deduplicated at execution.

#### Scenario: Ownership boundary stops ancestor walk
- **WHEN** the ancestor walk encounters a directory owned by a UID other than the current process UID (e.g., root-owned `/home/`)
- **THEN** the walk stops and no ACL is applied to that directory or any of its ancestors

#### Scenario: Boundary list stops ancestor walk
- **WHEN** the ancestor walk would need to grant on a path in the walker boundary list (`/`, `/etc`, `/usr`, `/var`, `/tmp`, `/proc`, `/sys`, `/dev`, `/boot`, `/run`, `/home`, `/root`, `~/`)
- **THEN** the walk stops and no ACL is applied to that path; the situation surfaces as a doctor failure with operator-resolvable remediation

#### Scenario: Root directory excluded
- **WHEN** the ancestor walk reaches `/`
- **THEN** no ACL is applied to `/`

#### Scenario: Ancestor ACLs not revoked on stop
- **WHEN** `sandbox stop <inst>` executes ACL revocation
- **THEN** ancestor directory `--x` ACLs are NOT included in the revocation set

#### Scenario: Ancestor ACLs not revoked on destroy
- **WHEN** `sandbox destroy <inst>` executes ACL revocation
- **THEN** ancestor directory `--x` ACLs are NOT included in the revocation set; cleanup happens transitively when `rmdir(workspaces/<inst>/)` succeeds at end of destroy

#### Scenario: Concurrent ancestor grants are idempotent
- **WHEN** two instances grant `--x` on the same ancestor directory simultaneously
- **THEN** both `setfacl` calls succeed without error (idempotent — same ACL entry applied twice)

### Requirement: Instance Root Read-Execute ACL
The system SHALL grant read-execute (`r-x`) ACL on the instance root directory so the sandbox user can list its contents for compose file resolution. The instance root is `<sandbox_ai_home()>/instances/<inst>/`.

#### Scenario: Instance root ACL granted at start
- **WHEN** `sandbox start <inst>` reaches Phase 5 (ACL grants)
- **THEN** `setfacl -m u:<host_unprivileged_user>:r-x <sandbox_ai_home()>/instances/<inst>/` is applied

#### Scenario: Instance root ACL revoked at stop
- **WHEN** `sandbox stop <inst>` executes ACL revocation
- **THEN** `setfacl -x u:<host_unprivileged_user> <sandbox_ai_home()>/instances/<inst>/` is applied

### Requirement: Environment File Read ACL

The system SHALL grant a read-only named ACL on `.sandbox.env` so the
sandbox user's `docker compose` process can parse `env_file:`
directives and `--env-file` interpolation. The `.sandbox.env` lives at
`<sandbox_ai_home()>/instances/<inst>/.sandbox.env`.

The named-ACL on `.sandbox.env` is `granted-once, persistent` (cluster
3 — `Acl Revoke Plan Excludes Persistent Grants`): it is applied at
every `sandbox start` (idempotently — re-running `setfacl -m
u:<host_user>:r` on an already-granted file is a no-op), survives
`sandbox stop`, and is removed only when `sandbox destroy` rmtrees the
instance directory.

The lifecycle reclassification eliminates the previous defensive
re-grant rituals at `_container_status` (`docker compose ps` could not
read `--env-file` after a stop) and `destroy`'s preflight (`docker
compose down` could not read `--env-file` after a stop). Those
rituals — implemented temporarily as `_ensure_env_file_readable_by_daemon`
calls before each read-side touchpoint — are deleted; the persistent
classification is the structural fix.

#### Scenario: .sandbox.env ACL granted at start
- **WHEN** `sandbox start <inst>` reaches Phase 5 (ACL grants)
- **THEN** `setfacl -m u:<host_unprivileged_user>:r
  <sandbox_ai_home()>/instances/<inst>/.sandbox.env` is applied

#### Scenario: .sandbox.env ACL is NOT revoked at stop
- **WHEN** `sandbox stop <inst>` executes the teardown sequence
- **THEN** the named ACL on `.sandbox.env` is NOT touched: the entry
  is absent from `_acl_revoke_plan` output and `_revoke_acls` issues no
  setfacl call against `.sandbox.env`

#### Scenario: .sandbox.env ACL survives stop/start cycles
- **WHEN** the lifecycle `start → stop → start → stop` is exercised
- **THEN** at each transition the named ACL on `.sandbox.env` remains
  present; `_container_status`, `_compose_down`, and the
  `docker compose up` invocation can each read `--env-file` without a
  defensive re-grant ritual

#### Scenario: .sandbox.env ACL removed only by destroy rmtree
- **WHEN** `sandbox destroy <inst>` rmtrees the instance directory at
  D7
- **THEN** the named ACL is removed implicitly via the file's deletion;
  no explicit `setfacl -x u:<host>` call against `.sandbox.env` runs
  during destroy

#### Scenario: Missing .sandbox.env fails explicitly at start
- **WHEN** `.sandbox.env` does not exist at Phase 5
- **THEN** `setfacl` fails and the error is surfaced as a
  `SandboxExecutionError` indicating instance corruption (no silent
  skip)

#### Scenario: Defensive env-ACL re-grant helper is removed
- **WHEN** the `cli.main` module is imported
- **THEN** there is no symbol `_ensure_env_file_readable_by_daemon`;
  `_container_status` and `destroy`'s preflight contain no defensive
  setfacl on `.sandbox.env`

### Requirement: ACL Grant Plan as Single Source of Truth

The system SHALL define ACL grant targets in a single function (`_acl_grant_plan`) consumed by both the execution path and the dry-run preview. The grant plan SHALL include named-acl operations only; helper-recipe phases (subuid-chown, consumer-uid-0-chown, shared-group) are separate phases with their own plans (`_helper_mkdir_chown_plan`, `_helper_cp_chown_plan`, `_workspace_shared_group_plan`). Each plan is its own single source of truth for its mechanism. Plans iterate `[workspaces]` (sorted by name) where applicable.

The grant on `config/` is recursive `rX` (read + conditional execute,
NOT write). Daemon host-level write on file CONTENTS is not required
because the helper-cp recipe (per the `Helper Recipe Phases
(unlink+cp+chmod+chown discipline)` requirement) performs unlink+cp
inside the helper container under `cap_dac_override`. The recursive
`u:<host_user>:rwX` widening on `config/` from earlier orchestrator
versions has been retired; the runtime grant MUST NOT carry it.

The grant plan SHALL emit a per-helper-cp-parent dir-level `rwx`
entry for each `config/<subdir>` listed in `RO_FILE_RECIPES`
(`config/coredns`, `config/dnsdist`, `config/proxy`, `config/core`,
`config/admin`). This is BUG-B for config/ — provisioning write on
the parent dir, required so the helper-cp bind mount of
`config/<subdir>` is mountable read-write into the helper container.
Parallel to the existing `docker/core` and `secrets/` dir-level rwx
grants.

#### Scenario: Grant plan consumed by execution
- **WHEN** `_phase_acl_grant` runs
- **THEN** it iterates over the output of `_acl_grant_plan()` to apply each named-acl operation, including per-workspace effective + default-ACL grants

#### Scenario: Grant plan consumed by dry-run preview
- **WHEN** `_dry_run_pipeline` previews the start sequence
- **THEN** it iterates over the output of `_acl_grant_plan()` to display each ACL command, AND iterates over the helper-recipe plans to display each helper invocation, with per-workspace fan-out for shared-group operations

#### Scenario: Helper-recipe plans are separate functions
- **WHEN** the orchestrator code is inspected
- **THEN** there are distinct `_helper_mkdir_chown_plan()`, `_helper_cp_chown_plan()`, and `_workspace_shared_group_plan()` functions; each is the single source of truth for its mechanism; `_workspace_shared_group_plan()` returns per-workspace operations

#### Scenario: Revoke plan excludes ancestors and persistent ops
- **WHEN** `_acl_revoke_plan()` is called
- **THEN** the returned target set includes named-acl entries on instance root, docker/, config/ (dir-level), secrets/ (dir-level), .sandbox.env, AND named-acl entries on EACH workspace.path (effective + default ACL host_user portion); it does NOT include ancestor directories or any chown/chmod operation, NOR any path under `_backups/`

#### Scenario: config/ recursive grant is dir-level rX (not rwX)
- **WHEN** the spec is read end-to-end as the design-clean target
- **THEN** the recursive named-ACL grant on `config/` is `u:<host_user>:rX` (read + conditional execute only); per-file mutability inside is handled exclusively by the `consumer-uid-0-chown` recipe AND the helper-cp recipe's in-helper unlink+cp pair under `cap_dac_override` (no host-level daemon write on file CONTENTS is required); the runtime grant in `cli/main.py` MUST NOT carry a recursive `u:<host_user>:rwX` widening on `config/`

#### Scenario: per-helper-cp-parent dir-level rwx grants present
- **WHEN** `_acl_grant_plan` is queried with a populated instance dir
- **THEN** the plan contains a `setfacl -m u:<host_user>:rwx <parent>` entry for each of `config/coredns`, `config/dnsdist`, `config/proxy`, `config/core`, `config/admin`; each entry is dir-level (NOT recursive) AND is parallel to the existing dir-level rwx entries on `docker/core` and `secrets/`

### Requirement: Fault-Isolated ACL Revocation
The system SHALL execute each ACL revocation independently with `check=False`. Failures SHALL be collected and reported as warnings. All targets SHALL be attempted regardless of individual failures.

#### Scenario: Partial revocation failure does not abort cleanup
- **WHEN** one ACL revocation target fails (e.g., file already deleted, ACL already absent)
- **THEN** remaining targets are still attempted and the failure is reported as a warning

#### Scenario: All revocation failures reported
- **WHEN** multiple ACL revocation targets fail
- **THEN** each failure is reported with the target description and stderr content

#### Scenario: Default-ACL named-entry revocation fault-isolated
- **WHEN** the workspace's default-ACL `u:host_user` removal fails (e.g., default ACL is unexpectedly absent)
- **THEN** the failure is reported as a warning; the effective ACL revocation still attempts independently

### Requirement: Phase Order Contract for Ownership-Sensitive Phases

The system SHALL execute ownership-sensitive phases in a specific order during `sandbox start` such that intermediate filesystem states between phases are mode-restrictive (per Decision 6) and access-controlled. The order SHALL be: `_phase_ipam → _phase_workspace_shared_group → _phase_acl_grant → _phase_credentials → _phase_hydrate → _phase_grant_post_hydrate_daemon_read → _phase_helper_mkdir_chown_cache_log → _phase_helper_cp_chown_ro_files → _phase_compose_up`.

Three ordering invariants are load-bearing:

1. `_phase_workspace_shared_group` runs BEFORE `_phase_acl_grant` so `chmod 2770` on each workspace root lands on a non-extended-ACL inode and the `group::` entry propagates from the mode bits without requiring a separate `setfacl -m g::rwx` call (per the `Workspace Shared-Group Phase Ordering` requirement).
2. `_phase_acl_grant` runs BEFORE `_phase_credentials` so the default ACL on `secrets/` (granting `u:<host_user>:r`) is in place before `generate_ssh_keypair` opens new files inside it (per the `Helper-CP Source Files Daemon-Readable Pre-Recipe` requirement; closes finding 8.D alternative #1, replacing the rejected chgrp-to-daemon-gid alternative #2 which failed EPERM because `dev` is intentionally not a member of the daemon's primary group).
3. `_phase_grant_post_hydrate_daemon_read` runs AFTER both `_phase_credentials` AND `_phase_hydrate` so every helper-cp source file (enumerated by `RO_FILE_RECIPES` + `EXEC_FILE_RECIPES`) AND every daemon-read direct file (enumerated by `DAEMON_READ_DIRECT_FILES`) is on disk by the time the unified setfacl-as-owner pass iterates the inventories, AND BEFORE `_phase_helper_cp_chown_ro_files` so the daemon's bind-mounted view of `/p/<file>` carries the named ACL entry by the time the helper container's `cp /p/<file>` step runs, AND BEFORE `_phase_compose_up` so `docker compose -f <compose.yml>` opens `compose.yml` (and its conditional extras) under the daemon's identity with the named ACL already in place (per the `Helper-CP Source Files Daemon-Readable Pre-Recipe` requirement; closes the empirical breakages where `cp: can't open '/p/Corefile': Permission denied` and `open /…/docker/compose.yml: permission denied` surfaced after the 8.D phase reorder).

Reordering ANY of these phases is a behavioral change requiring a new spec change.

#### Scenario: Phase order matches the contract
- **WHEN** `sandbox start` runs
- **THEN** the phase invocation order is: ipam, workspace_shared_group, acl_grant, credentials, hydrate, grant_post_hydrate_daemon_read, helper_mkdir_chown_cache_log, helper_cp_chown_ro_files, compose_up

#### Scenario: Workspace shared-group precedes named-ACL grant
- **WHEN** `sandbox start` runs the ownership-sensitive phases
- **THEN** `_phase_workspace_shared_group` is invoked for each workspace path BEFORE `_phase_acl_grant`; this ordering is what allows the workspace shared-group recipe to omit the explicit `setfacl -m g::rwx` step (the mode bits set by `chmod 2770` propagate to `group::` because the inode has no extended ACL when chmod runs)

#### Scenario: ACL grant precedes credentials
- **WHEN** `sandbox start` runs the ownership-sensitive phases
- **THEN** `_phase_acl_grant` is invoked BEFORE `_phase_credentials`; this is what allows the default ACL on `secrets/` to be in place when `generate_ssh_keypair` writes new files (per the `Helper-CP Source Files Daemon-Readable Pre-Recipe` requirement)

#### Scenario: Unified setfacl pass between hydrate and helper-cp
- **WHEN** `sandbox start` runs the ownership-sensitive phases
- **THEN** `_phase_grant_post_hydrate_daemon_read` is invoked AFTER `_phase_hydrate` (so every helper-cp source file is on disk) and BEFORE `_phase_helper_cp_chown_ro_files` (so the daemon's view of each source file carries the `u:<host_user>:r` named entry when the helper container's `cp /p/<file>` runs)

#### Scenario: ACL grant precedes helper phases
- **WHEN** the helper-recipe phases run
- **THEN** the named-ACL grants on parent dirs (config/, secrets/) have already been applied, so the daemon (claude-sandbox / gofer) can traverse to the file targets

#### Scenario: Compose up follows all ownership phases
- **WHEN** `_phase_compose_up` is invoked
- **THEN** all helper-recipe phases have completed; in-container services see the ownership/mode state established by those phases

#### Scenario: Workspace failure pre-ACL-grant skips revoke
- **WHEN** `_phase_workspace_shared_group` raises `SandboxExecutionError` for a workspace and `_phase_acl_grant` has not yet been invoked (`acl_granted` flag is False)
- **THEN** the start-command failure handler does NOT invoke `_revoke_acls` because no ACLs were granted; the failure surfaces with the workspace error context only

### Requirement: Hydration Writes Sensitive Files at Restrictive Mode

The hydration pipeline SHALL write sensitive files (secrets, ro config files, env file) at restrictive modes from the moment of file creation, bypassing the orchestrator process's umask. This closes the brief intermediate-state window between hydration and helper-cp+chown that would otherwise expose sensitive content under dev's default umask.

#### Scenario: Secrets written at mode 0600
- **WHEN** hydration writes any file under `secrets/` (authorized_keys, ipc_*)
- **THEN** the file is created via `os.open(path, O_WRONLY | O_CREAT | O_EXCL, 0o600)` so the on-disk mode is 0600 immediately, regardless of the orchestrator process's umask

#### Scenario: Ro config files written at mode 0640
- **WHEN** hydration writes any of the ro config files in the consumer-uid-0-chown table (Corefile, dnsdist conf, dotfiles, sshd_config, all 5 proxy files)
- **THEN** the file is created via `os.open(path, O_WRONLY | O_CREAT | O_EXCL, 0o640)` so the on-disk mode is 0640 immediately

#### Scenario: .sandbox.env unchanged
- **WHEN** scaffold writes `.sandbox.env`
- **THEN** the file is created at mode 0600 via the existing O_CREAT|O_EXCL pattern (no behavior change from today)

#### Scenario: Intermediate state is access-restrictive
- **WHEN** the orchestrator process writes a secret file at 0600 dev:dev and Phase 5's ACL grant gives the daemon `u:host_user:rX` traverse on the parent dir
- **THEN** the daemon (claude-sandbox) cannot read the secret's content during the brief window before helper-cp+chown runs (file mode 0600 dev:dev rejects daemon access; daemon is out of dev's userns and has no DAC override for dev-owned files)

### Requirement: Workspace Recursive Setup via Drift Detection

The workspace shared-group recipe SHALL detect whether the workspace tree needs recursive setup (first-time application or operator-induced drift) by inspecting the workspace root's setgid bit and group ownership. Recursive operations SHALL run only on first-time-or-drift; subsequent starts SHALL run only the cheap root-state idempotent assertions.

#### Scenario: Drift detection signal is workspace root state
- **WHEN** the recipe checks whether recursive setup is needed
- **THEN** the check is `(workspace_root has setgid bit) AND (workspace_root group == workspace_bridge_gid)` — a single `os.stat` call

#### Scenario: First-run triggers recursive setup
- **WHEN** the workspace root does not have setgid bit OR has the wrong group
- **THEN** recursive `chgrp` (best-effort), recursive `chmod 2770` for dirs, recursive `chmod 0660` for files, and `setfacl` (effective + default) are applied

#### Scenario: Steady-state skips recursion
- **WHEN** the workspace root has setgid bit AND correct group
- **THEN** recursive operations are skipped; only the root-state idempotent assertions run (chmod 2770, chgrp, setfacl on the root only)

#### Scenario: Best-effort recursive chgrp on non-dev-owned files
- **WHEN** recursive `chgrp` encounters a file the orchestrator does not own (e.g., system-installed asset, file from a previous user)
- **THEN** the chgrp for that specific file fails with EPERM; the failure is logged and counted; the rest of the recursive operation continues; the doctor's post-run report includes the count of skipped files

### Requirement: Topographical File Isolation Boundaries
The system SHALL enforce separation between the immutable tooling plane (the packaged `templates` Python module containing `templates/docker/` and `templates/config/`) and the mutable per-instance plane (`<sandbox_ai_home()>/instances/<inst>/`).

#### Scenario: The Immutable Tooling Plane (`templates/docker/` and `templates/config/`)
- **WHEN** the orchestrator configures infrastructure for an instance
- **THEN** template sources under `templates/docker/` and `templates/config/` (read via `importlib.resources.files("templates")`) are read-only inputs to the hydration pipeline; they are never written to at runtime

#### Scenario: The Mutable Instance Plane
- **WHEN** the hydration pipeline runs
- **THEN** all rendered artifacts are written exclusively under `<sandbox_ai_home()>/instances/<inst>/`, which is owned by `dev` and scoped to the instance

#### Scenario: Workspace Plane Separate From Instance Plane
- **WHEN** the orchestrator scaffolds a workspace
- **THEN** the workspace tree is written exclusively under `<sandbox_ai_home()>/workspaces/<inst>/<ws>/`, which is dev-owned at scaffold and transitions to `<bridge-group>`-grouped on first start; this plane is distinct from the instance plane

#### Scenario: Backup Plane Separate From Instance and Workspace Planes
- **WHEN** the backup mechanism creates a backup tree
- **THEN** the backup tree is written exclusively under `<sandbox_ai_home()>/workspaces/_backups/<inst>/<ws>/<ts>/`, which is dev-owned with mode 0700 and no ACL state; this plane is distinct from both the instance plane and the live workspace plane

#### Scenario: Shell History Isolation via Directory Mount
- **WHEN** the admin and core containers are started
- **THEN** the bind mounts for shell history are at the **directory** level (`<sandbox_ai_home()>/instances/<inst>/log/admin/` and `<sandbox_ai_home()>/instances/<inst>/log/core/`), and the `HISTFILE` environment variable inside each container points to a specific path within that mounted directory

#### Scenario: SSH Credentials in Secrets Directory
- **WHEN** the hydration pipeline runs
- **THEN** SSH keypair files are written exclusively under `<sandbox_ai_home()>/instances/<inst>/secrets/`

### Requirement: Deep VFS Annihilation
The system SHALL support volume removal on explicit operator request, scoped strictly to Docker named volumes owned by the instance.

#### Scenario: The `--clean` Flag Termination Sequence
- **WHEN** the human operator executes `sandbox stop <inst> --clean`
- **THEN** the orchestrator executes `docker compose down -v`, removing all named Docker volumes for the instance (e.g., Postgres data), while leaving `<sandbox_ai_home()>/instances/<inst>/log/`, `<sandbox_ai_home()>/instances/<inst>/cache/`, and all `<sandbox_ai_home()>/workspaces/<inst>/<ws>/` trees on the host filesystem intact

#### Scenario: The `destroy` Full Annihilation
- **WHEN** the human operator confirms `sandbox destroy <inst>` (per `cli-destroy`'s phase order)
- **THEN** `docker compose down -v` removes all named Docker volumes; `shutil.rmtree(<sandbox_ai_home()>/instances/<inst>/)` removes the entire instance directory; AND `shutil.rmtree(<sandbox_ai_home()>/workspaces/<inst>/<ws>/)` is invoked for each workspace; `_backups/` trees are preserved (separate plane)

#### Scenario: admin-ipc_vol absent from volumes
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** there is NO volume definition for `admin-ipc_vol`

#### Scenario: mcp-ipc_vol absent from volumes
- **WHEN** the rendered `compose.yml` and `mcp-firecrawl.yml` are inspected
- **THEN** there is NO volume definition for `mcp-ipc_vol`

#### Scenario: /sock mount absent from all services
- **WHEN** the rendered `compose.yml` is inspected
- **THEN** no service contains a volume mount referencing `/sock`

### Requirement: Custom Config Override Bind Mounts
The rendered `compose.yml` SHALL include read-only bind mounts provisioning the custom config override directories inside each container. Without these mounts, the override hooks in `.bashrc`, `.gitconfig`, `.zshrc`, and `.tmux.conf` would silently no-op.

#### Scenario: Core custom config mount
- **WHEN** the rendered `compose.yml` is inspected for the core service
- **THEN** it contains a volume entry `{{ instance_dir }}/custom/config/core:{{ custom_config_core }}:ro`

#### Scenario: Admin custom config mount
- **WHEN** the rendered `compose.yml` is inspected for the admin service
- **THEN** it contains a volume entry `{{ instance_dir }}/custom/config/admin:{{ custom_config_admin }}:ro`

### Requirement: Tmux Resurrect State Bind Mount

The rendered `compose.yml` SHALL include a read-write bind mount for the tmux resurrect session state directory in the admin container, sourced from the `cache/admin/tmux_resurrect` instance subdirectory. This relocates the plugin state from the agent-writable workspace.

The bind-mount source path (`{{ instance_dir }}/cache/admin/tmux_resurrect`) is the **host-side** location. The corresponding **in-container** target path is provided by `hydration-pipeline`'s Jinja context value `tmux_resurrect_dir` (typically `/home/human/.sandbox/tmux_resurrect`); the two paths are different sides of the same mount and are deliberately decoupled — host-side layout is governed by this spec, in-container layout by the consumer's home-directory convention.

#### Scenario: Tmux resurrect mount in admin service
- **WHEN** the rendered `compose.yml` is inspected for the admin service
- **THEN** it contains a volume entry `{{ instance_dir }}/cache/admin/tmux_resurrect:{{ tmux_resurrect_dir }}:rw`

### Requirement: Container-Namespaced Cache Directory

The `cache/` subtree under each instance dir SHALL follow a container-namespaced convention. The Claude Code cache directory SHALL be at `cache/core/.claude` (not `cache/.claude`). The tmux resurrect cache directory SHALL be at `cache/admin/tmux_resurrect` (not `cache/tmux_resurrect`). Paths are relative to the instance dir at `<sandbox_ai_home()>/instances/<inst>/`.

The cache subtree splits between scaffold-created parents and helper-recipe-created leaves per the "Scaffold-vs-Helper Boundary" requirement: `core.scaffold.INSTANCE_SUBDIRS` includes `cache/core` and `cache/admin` (parent dirs) but NOT the leaves `cache/core/.claude` and `cache/admin/tmux_resurrect`. The leaves are created by `_phase_helper_mkdir_chown_cache_log` on first start (per the "Cache/Log Leaf Inventory" requirement).

#### Scenario: Scaffold creates namespaced cache parents only
- **WHEN** `sandbox init` runs `core.scaffold.create_instance_dirs()`
- **THEN** `<sandbox_ai_home()>/instances/<inst>/cache/core/` and `<sandbox_ai_home()>/instances/<inst>/cache/admin/` are created (mode `0775` dev:dev) but the leaves `cache/core/.claude` and `cache/admin/tmux_resurrect` are NOT created at scaffold time

#### Scenario: Helper recipe creates namespaced cache leaves on first start
- **WHEN** `_phase_helper_mkdir_chown_cache_log` runs for the first time on a freshly-init'd instance
- **THEN** the helper container creates `cache/core/.claude` and `cache/admin/tmux_resurrect` as in-container root and chowns each to the consumer's host subuid; the on-disk paths exist post-phase

#### Scenario: Compose mount references namespaced cache path
- **WHEN** the rendered `compose.yml` is inspected for the core service
- **THEN** the `.claude` directory mount references `{{ instance_dir }}/cache/core/.claude` (not `{{ instance_dir }}/cache/.claude`)

#### Scenario: Compose mount references namespaced tmux path
- **WHEN** the rendered `compose.yml` is inspected for the admin service
- **THEN** the tmux resurrect mount references `{{ instance_dir }}/cache/admin/tmux_resurrect` (not `{{ instance_dir }}/cache/tmux_resurrect`)

### Requirement: Cache/Log Leaf Inventory

The cache/log helper-recipe phase operates on a fixed inventory of bind-mount source leaves. The inventory SHALL be enumerated in this requirement as the single source of truth; other specs (`cli-start`, `cli-stop`, scenario bodies elsewhere in this spec) reference it by name rather than re-enumerating.

The inventory:

| Leaf path (relative to `<sandbox_ai_home()>/instances/<inst>/`) | Container service consuming the mount |
| --- | --- |
| `cache/core/.claude` | core (Claude Code agent cache) |
| `cache/admin/tmux_resurrect` | admin (tmux session resurrect state) |
| `log/core` | core (agent log output) |
| `log/admin` | admin (admin shell log output) |

All four leaves are owned end-to-end by the helper-recipe (per the
`Scaffold-vs-Helper Boundary` requirement); none of them appear in
`core.scaffold.INSTANCE_SUBDIRS`. The log leaves were added to the
helper-recipe-owned set in cluster
`orchestrator-volumes-scaffold-helper-acl-completeness` after the
empirical descent (Finding 8.A) demonstrated the same userns-EPERM
trap that motivated the original cache-leaf exclusion.

The inventory's authoritative *runtime* source is the bind-mount inventory rendered in `compose.yml`; this spec enumeration is documentary. If the runtime inventory ever diverges from this enumeration, the spec is updated in the same change that adds or removes a leaf in `compose.yml` / its templates.

#### Scenario: Inventory enumerates exactly the four cache/log leaves
- **WHEN** the inventory is consulted by `_phase_helper_mkdir_chown_cache_log`, `_acl_grant_plan`, `_acl_revoke_plan`, or any doctor check
- **THEN** the consulted set is exactly `{cache/core/.claude, cache/admin/tmux_resurrect, log/core, log/admin}`; no consumer constructs a different list inline

#### Scenario: Other specs reference inventory by name
- **WHEN** `cli-start` or `cli-stop` or any cross-referencing spec describes behavior over the cache/log leaves
- **THEN** the spec text refers to "the cache/log leaves per `orchestrator-volumes`'s 'Cache/Log Leaf Inventory' requirement" rather than re-enumerating the four paths inline

#### Scenario: All four leaves are helper-recipe-owned end-to-end
- **WHEN** `core.scaffold.INSTANCE_SUBDIRS` is inspected
- **THEN** none of the four leaves (`cache/core/.claude`, `cache/admin/tmux_resurrect`, `log/core`, `log/admin`) appears in the list; their parents (`cache/core`, `cache/admin`, `log`) are present so the helper recipe creates the leaves on first start as in-container root and chowns them to the consumer subuid

### Requirement: Scaffold-vs-Helper Boundary

Directories subject to a helper-recipe `subuid-chown` or `consumer-uid-0-chown` mechanism (per the lifecycle×mechanism table in "UID Paradox ACL Default Overrides") SHALL NOT be created by `core.scaffold.create_instance_dirs()` (i.e., they MUST NOT appear in `INSTANCE_SUBDIRS`). The helper recipe creates them on first start as in-container root (= host claude-sandbox, mapped) and chowns them to the consumer's host subuid; the scaffolded *parent* directory carries the orchestrator's `dev`-owned `u:dev:rwx` default ACL so the agent's child files inherit dev-readability.

The rationale is enforceable by kernel rule: `CAP_CHOWN` in a user namespace authorizes `chown` only when the file's current owner uid is mapped in the userns. `sandbox init` runs as the dev user (host uid `1000`), which is unmapped in the helper container's userns; a scaffold-pre-created leaf would be permanently unreachable to the helper recipe.

The boundary applies to ALL four leaves in the `Cache/Log Leaf
Inventory` (the original two cache leaves and the two log leaves added
by cluster `orchestrator-volumes-scaffold-helper-acl-completeness`).
Any future helper-recipe leaf added to the inventory is similarly
excluded from `INSTANCE_SUBDIRS`.

#### Scenario: INSTANCE_SUBDIRS excludes helper-recipe-owned leaves
- **WHEN** `core.scaffold.INSTANCE_SUBDIRS` is inspected
- **THEN** the list contains `cache/core` and `cache/admin` (parents) but NOT `cache/core/.claude` or `cache/admin/tmux_resurrect` (helper-owned cache leaves); the list also EXCLUDES `log/core` and `log/admin` (helper-owned log leaves added per the extended `Cache/Log Leaf Inventory`)

#### Scenario: Helper recipe creates the leaf on first start
- **WHEN** `_phase_helper_mkdir_chown_cache_log` runs against a freshly-init'd instance whose cache/log leaves do not yet exist on disk
- **THEN** `helper_mkdir_chown_dirs` performs `mkdir -p /p/<leaf>` followed by `chown <consumer-subuid>:<consumer-subgid> /p/<leaf>`, both as in-container root; both operations succeed because the leaf is created by claude-sandbox (mapped) and the chown target is in the subuid range (mapped)

#### Scenario: Helper recipe is idempotent on re-start
- **WHEN** `_phase_helper_mkdir_chown_cache_log` runs against an instance whose cache/log leaves already exist with the correct consumer-subuid ownership
- **THEN** the `mkdir -p` is a no-op and the `chown` is idempotent; no change in on-disk state

#### Scenario: Pre-Change-D leftover leaves trigger doctor warning
- **WHEN** an operator who init'd their instance before the scaffold-vs-helper boundary was enforced runs `sandbox doctor` (their `cache/core/.claude` exists as `dev:dev`)
- **THEN** the `pre_existing_instance_layout` doctor check (per `cli-doctor`) emits the dev-owned-leaf warning with a `rm -rf` remediation; running the remediation lets the next `sandbox start` succeed because the helper recipe creates the leaf fresh as claude-sandbox-owned

### Requirement: Stale Proxy Seed File Removal
The following files in `templates/config/proxy/` SHALL be absent from the tooling plane: `allowed_domains.txt`, `trusted_clients.acl`, `.htpasswd`. These files are overridden by programmatic generation in `render_templates()` or by `core/crypto.py` during scaffold. Their presence in the tooling plane is misleading — edits to them have no effect.

#### Scenario: No stale proxy seed files in tooling plane
- **WHEN** the `templates/config/proxy/` directory in the tooling plane is inspected
- **THEN** it contains only `squid.conf` and `ERR_SANDBOX_403` (the Jinja2 template and the static error page)

### Requirement: Helper-Recipe Parent ACL Grants

The `_acl_grant_plan` function SHALL emit a named-ACL grant `setfacl -m u:<host_unprivileged_user>:rwx` plus a matching default ACL on each helper-recipe parent directory under the instance dir. The helper-recipe parents are `cache/core`, `cache/admin`, and `log/` (the parents of the four leaves enumerated in the `Cache/Log Leaf Inventory` requirement). Without these grants the helper-mkdir+chown phase cannot create leaves inside the parents (the daemon's userns has the host dev uid unmapped, so DAC for files owned by host dev resolves only to "other" perms `r-x`, blocking `mkdir`).

#### Scenario: Helper-recipe parent receives effective named-ACL grant
- **WHEN** `_acl_grant_plan(instance_dir, host_user)` is called
- **THEN** the returned plan contains an entry whose command is `setfacl -m u:<host_user>:rwx <instance_dir>/<parent>` for EACH parent in `("cache/core", "cache/admin", "log")`, with description `"helper-recipe parent: <abs-path>"`

#### Scenario: Helper-recipe parent receives matching default ACL
- **WHEN** `_acl_grant_plan(instance_dir, host_user)` is called
- **THEN** for EACH parent in `("cache/core", "cache/admin", "log")` the plan contains a default-ACL grant `setfacl -d -m u::rwx,g::rwx,o::---,m::rwx,u:<host_user>:rwx <instance_dir>/<parent>` with description `"helper-recipe parent default ACL: <abs-path>"`

### Requirement: Executable-Script File Recipes

The `cli.main` module SHALL define `EXEC_FILE_RECIPES` as a sibling-table constant alongside `RO_FILE_RECIPES`. Each entry is `(parent_relative_to_instance, files, in_container_consumer_uid, mode)` with mode `0o500` (owner-only read+exec). The executable-script kind is structurally distinct from `RO_FILE_RECIPES` (mode `0o640` for ro configs and mode `0o600` for secrets): the consumer is the sole reader/exec; nobody else has any access. `_helper_cp_chown_plan` SHALL iterate both `RO_FILE_RECIPES` and `EXEC_FILE_RECIPES` in a single pass so the helper-cp+chown phase processes both kinds uniformly. The current entry is `("docker/core", ("entrypoint.sh",), 1000, 0o500)`.

#### Scenario: EXEC_FILE_RECIPES contains the core entrypoint at mode 0500
- **WHEN** `cli.main.EXEC_FILE_RECIPES` is inspected
- **THEN** it contains `("docker/core", ("entrypoint.sh",), 1000, 0o500)`

#### Scenario: Helper-cp plan iterates both ro and exec tables
- **WHEN** `_helper_cp_chown_plan(instance_dir, host_user)` is called
- **THEN** the returned plan length is `len(RO_FILE_RECIPES) + len(EXEC_FILE_RECIPES)`; entries from both tables appear, each with the consumer-mapped owner uid/gid and the table's stated mode

#### Scenario: Entrypoint absent from RO_FILE_RECIPES
- **WHEN** `cli.main.RO_FILE_RECIPES` is inspected
- **THEN** no entry has parent `docker/core` and files containing `entrypoint.sh`; the entrypoint kind belongs to `EXEC_FILE_RECIPES` exclusively

#### Scenario: Admin Dockerfile uses owner-only mode
- **WHEN** `templates/docker/admin/Dockerfile.admin.debian` is inspected
- **THEN** the entrypoint is installed with `COPY --chown=human:human entrypoint.sh /usr/local/bin/entrypoint.sh` followed by `chmod 0500 /usr/local/bin/entrypoint.sh` (owner-only r-x)

### Requirement: Workspace Shared-Group Phase Ordering

The workspace shared-group recipe (`_phase_workspace_shared_group`, `_workspace_shared_group_plan`) SHALL run BEFORE the named-ACL grant phase (`_phase_acl_grant`) so `chmod 2770` lands on a non-extended-ACL inode and the `group::` entry propagates from the mode bits without requiring a separate `setfacl -m g::rwx` call. The plan and phase MUST NOT contain an explicit `setfacl -m g::rwx` step on the workspace root — its presence indicates the prior workaround (which set the entry explicitly because chmod ran after named-ACL grants had already extended the inode's ACL) has resurfaced and the phase ordering has regressed.

#### Scenario: Plan omits explicit owning-group setfacl step
- **WHEN** `_workspace_shared_group_plan(workspace, bridge_gid, dev_user, host_user)` is called
- **THEN** the returned plan does NOT contain any operation labeled `"setfacl -m g::rwx"` on the workspace root; the chmod 2770 step alone establishes `group::rwx` because the inode has no extended ACL when chmod runs

#### Scenario: Phase invocation order — workspace before named-ACL
- **WHEN** `sandbox start <inst>` runs the ownership-sensitive phases
- **THEN** `_phase_workspace_shared_group` is invoked for each workspace path BEFORE `_phase_acl_grant`; the call order is verifiable via mocked phase recording in unit tests and is enforced by the new sequence in the start command

### Requirement: Helper-CP Source Files Daemon-Readable Pre-Recipe

The system SHALL grant the daemon partitioned permission bits across two distinct categories of dev-created files, replacing the prior recursive `setfacl -R -m u:<host_user>:rwX <secrets>` widening on `secrets/` (which was discarded for granting daemon write on every secret's contents):

- **BUG-A — daemon RUNTIME READ on file CONTENTS.** Every file enumerated by `RO_FILE_RECIPES`, `EXEC_FILE_RECIPES`, AND `RW_FILE_RECIPES` (the authoritative helper-cp source-file inventory under `cli.main`) AND every file enumerated by `DAEMON_READ_DIRECT_FILES` (the authoritative inventory of dev-created files the daemon reads in place forever — `docker/compose.yml` plus the conditional compose extras `docker/extras/db-postgres.yml` and `docker/extras/mcp-firecrawl.yml`) SHALL receive a `u:<host_user>:r` named POSIX ACL entry on its inode BEFORE the daemon reads it. The mechanism is a unified post-hydrate setfacl-as-owner pass — `cli.main._phase_grant_post_hydrate_daemon_read(instance_dir, host_user)` — that iterates ALL FOUR inventories (RO_FILE_RECIPES + EXEC_FILE_RECIPES + RW_FILE_RECIPES + DAEMON_READ_DIRECT_FILES), runs `setfacl -m u:<host_user>:r <path>` against each existing file, and skips files that do not yet exist on disk (defensive; covers the conditional compose extras whose presence depends on `InstanceConfig` component flags).
- **BUG-B — daemon PROVISIONING WRITE on the parent dir.** `_acl_grant_plan` SHALL emit a dir-level `setfacl -m u:<host_user>:rwx <secrets_dir>` grant on `secrets/`. The dir-level write bit is required by the helper-cp recipe's in-helper `unlink /p/<f>` step (followed by `cp /tmp/<f> /p/<f>`) — see `helper-container`'s `helper_chown_files Primitive Contract` for the full `cp + unlink + cp + chmod + chown` recipe. Without write on the parent the `unlink` returns EPERM. The grant is dir-level only — it does NOT widen write to file contents (those remain at per-file `r` per BUG-A). This partition is strictly narrower than the prior recursive `setfacl -R -m u:<host_user>:rwX <secrets>` widening it replaces.

The two grants together replace the prior recursive `rwX` widening on `secrets/`, which MUST NOT appear in `_acl_grant_plan`.

The BUG-A inventory is divided into two structurally distinct categories:

1. **Helper-cp source files** (`RO_FILE_RECIPES` + `EXEC_FILE_RECIPES` + `RW_FILE_RECIPES`): the helper-cp recipe transfers ownership of these to a consumer subuid via a `cp /p/<f> /tmp/<f> + unlink /p/<f> + cp /tmp/<f> /p/<f> + chmod /p/<f> + chown /p/<f>` sequence inside a daemon-managed bind mount (per `helper-container`'s `helper_chown_files Primitive Contract`). The daemon reads the source path through the bind mount during the first `cp` step. The three sub-tables differ only in mode and bind-mount direction (RO/EXEC are `:ro` mounts; RW is `:rw` mounts) — the BUG-A mechanism is identical for all three.
2. **Daemon-read direct files** (`DAEMON_READ_DIRECT_FILES`): NEVER transferred via helper-cp. The daemon reads these in place forever in two distinct sub-categories:
   - **Compose YAML inputs**: `compose.yml` plus the conditional compose extras (`db-postgres.yml`, `mcp-firecrawl.yml`), consumed via `docker compose -f <path>` invocations whose canonical path-set is built by `_build_compose_files` (used by `_phase_compose_up`, `_compose_down`, and the `docker compose ps` callsites in `_container_status` / `_render_status_detailed`).
   - **Build context**: every Dockerfile rendered or copied by `core.hydration.render_templates` (`Dockerfile.core`, `Dockerfile.admin`, `Dockerfile.coredns`, conditional `Dockerfile.mcp-firecrawl`) plus every local-COPY source those Dockerfiles reference (currently only `admin/entrypoint.sh` — `core/entrypoint.sh` is covered by `EXEC_FILE_RECIPES` because it is bind-mounted at runtime, distinct from the admin entrypoint which is COPY'd into the image during build). Read by buildkit (running as the daemon) during `docker compose up --build`.

   If a new compose `--file` path is added to `_build_compose_files`, OR a new local-COPY source is added to any Dockerfile, OR a new Dockerfile is rendered by hydrate, `DAEMON_READ_DIRECT_FILES` MUST be extended in lockstep so the post-hydrate setfacl pass covers it. The empirical symptom of missing any build-context entry is `target <svc>: failed to solve: the Dockerfile cannot be empty` (buildkit treats an unreadable Dockerfile as empty).

`_phase_grant_post_hydrate_daemon_read` SHALL run AFTER both `_phase_credentials` and `_phase_hydrate` so every file in both categories is already on disk.

The BUG-A `setfacl` runs as the file owner (`dev`); the privilege boundary is preserved (no chgrp-to-foreign-group, no escalated privilege). `setfacl -m` recomputes the ACL `mask::` to cover the new named entry, defeating `core.hydration.write_restricted`'s `os.fchmod(mode)` step which would otherwise zero `mask::` to match the new mode's group bits and mask out any inherited named entry.

A failure of the per-file BUG-A `setfacl` MUST raise `SandboxExecutionError` mentioning the offending path and the phrase "grant daemon read on post-hydrate target"; the failure is not silently swallowed.

Why this is necessary even with the helper container's `--cap-add DAC_OVERRIDE`: in rootless docker the daemon runs as `host_user`; `cap_dac_override` held inside a user namespace bypasses DAC only for files whose owner uid/gid is mapped INSIDE that namespace. Files written by `dev` (host uid 1000, NOT mapped in the daemon's userns) appear as the overflow uid to in-container kernel checks; `cap_dac_override` does NOT apply. The same kernel rule applies to the compose-up case: `docker compose` running as the daemon user reads `compose.yml` from the host filesystem under the daemon's identity, not via cap_dac_override, so the daemon needs an explicit DAC grant on `compose.yml` (and any extras) — provided here by the named ACL entry. The same kernel rule applies to BUG-B: the helper container's `unlink /p/<f>` step (followed by `cp /tmp/<f> /p/<f>`) is gated by host-level write on `secrets/` and `cap_dac_override` does NOT bypass it; the dir-level `rwx` named entry granted to `host_user` is what makes the `unlink` + replacement-`cp` pair succeed. Post-helper-cp the helper-cp destination files are owned by the consumer's subuid (mapped in the userns), so `cap_dac_override` handles runtime reads on those files without further per-file ACL.

The recursive `rwX` widening on `config/` (introduced as a temporary fix in earlier orchestrator versions) has been retired: `_acl_grant_plan` now emits dir-level `rX` on `config/` plus per-helper-cp-parent dir-level `rwx` on each `config/<subdir>` (BUG-B parallel to `secrets/`), per the `ACL Grant Plan as Single Source of Truth` requirement. The same BUG-A/BUG-B partition therefore applies uniformly across all helper-cp source-file parents (`config/<subdir>`, `secrets/`, `docker/core`).

The `secrets/` directory additionally carries a default ACL `setfacl -d -m u::rw-,g::---,o::---,m::r--,u:<host_user>:r <secrets_dir>` — belt-and-suspenders for any future write path that does NOT chmod-after-create; the load-bearing mechanism for BUG-A on existing helper-cp source files is the unified setfacl-as-owner pass above. The default ACL revocation entry `setfacl -d -x u:<host_user> <secrets_dir>` SHALL appear in `_acl_revoke_plan` (symmetric with the grant).

#### Scenario: secrets/ ACL grant is dir-level rwx (not recursive rwX) — BUG-B

- **WHEN** `_acl_grant_plan(instance_dir, host_user)` is called
- **THEN** the entry whose description is `"secrets dir provisioning write: <abs-path>"` is `setfacl -m u:<host_user>:rwx <secrets-dir>` — NOT recursive (`-R`) and NOT `rwX`. A recursive `setfacl -R -m u:<host_user>:rwX <secrets>` widening MUST NOT appear in the plan. The dir-level `w` is the load-bearing bit for the helper-cp in-helper `unlink /p/<f>` step (which precedes the replacement `cp /tmp/<f> /p/<f>`); the dir-level grant does NOT widen write to file contents (per BUG-A files retain per-file `r`-only).

#### Scenario: secrets/ default ACL grants daemon read on inherited entries

- **WHEN** `_acl_grant_plan(instance_dir, host_user)` is called
- **THEN** the plan contains an entry whose description is `"secrets default ACL: <abs-path>"` whose command begins `setfacl -d -m` and includes a `u:<host_user>:r` named entry on the `secrets/` directory

#### Scenario: secrets/ default ACL revocation is symmetric

- **WHEN** `_acl_revoke_plan(instance_dir, host_user)` is called
- **THEN** the plan contains a `setfacl -d -x u:<host_user> <secrets_dir>` entry alongside the existing `setfacl -x u:<host_user> <secrets_dir>` traverse-revocation entry

#### Scenario: Phase order — unified setfacl pass runs after hydrate, before helper recipes

- **WHEN** `sandbox start` runs the ownership-sensitive phases
- **THEN** the invocation order is `_phase_acl_grant -> _phase_credentials -> _phase_hydrate -> _phase_grant_post_hydrate_daemon_read -> _phase_helper_mkdir_chown_cache_log -> _phase_helper_cp_chown_ro_files -> _phase_compose_up`; the unified setfacl pass runs after every helper-cp source file (RO + EXEC + RW) AND every daemon-read direct file is on disk, BEFORE the helper-cp recipe reads from `/p/` through the bind mount, AND BEFORE `docker compose -f <compose.yml>` opens the file under the daemon's identity

#### Scenario: Unified setfacl pass touches every helper-cp source file AND every daemon-read direct file

- **WHEN** `_grant_post_hydrate_daemon_read(instance_dir, host_user)` is called against an instance where every file in `RO_FILE_RECIPES`, `EXEC_FILE_RECIPES`, `RW_FILE_RECIPES`, and `DAEMON_READ_DIRECT_FILES` exists on disk
- **THEN** for EACH `(parent, files, _consumer_uid, _mode)` tuple in `(*RO_FILE_RECIPES, *EXEC_FILE_RECIPES, *RW_FILE_RECIPES)` AND EACH `(parent, files)` tuple in `DAEMON_READ_DIRECT_FILES`, and for EACH `fname` in `files`, `setfacl -m u:<host_user>:r <instance_dir>/<parent>/<fname>` is executed against the file's path

#### Scenario: DAEMON_READ_DIRECT_FILES inventory covers compose inputs AND build context

- **WHEN** `cli.main.DAEMON_READ_DIRECT_FILES` is inspected
- **THEN** it contains TWO sub-categories:
  1. **Compose YAML inputs** matching `_build_compose_files`'s enumeration: `("docker", ("compose.yml",))` (always present) AND `("docker/extras", ("db-postgres.yml", "mcp-firecrawl.yml"))` (conditional extras).
  2. **Build context** matching the Dockerfiles + their local-COPY sources rendered by `core.hydration.render_templates`: `("docker/core", ("Dockerfile.core",))`, `("docker/admin", ("Dockerfile.admin", "entrypoint.sh"))`, `("docker/coredns", ("Dockerfile.coredns",))`, AND `("docker/extras", ("Dockerfile.mcp-firecrawl",))` (conditional). The `admin/entrypoint.sh` entry is in this category — NOT `EXEC_FILE_RECIPES` — because it is COPY'd into the admin image during build (baked into the image, never bind-mounted at runtime). The `core/entrypoint.sh` is covered by the helper-cp branch via `EXEC_FILE_RECIPES` (bind-mounted at runtime). Empirical symptom of missing any build-context entry: `target <svc>: failed to solve: the Dockerfile cannot be empty` (buildkit treats an unreadable Dockerfile as empty).

#### Scenario: RW recipe sources are covered by the post-hydrate setfacl pass

- **WHEN** `_post_hydrate_daemon_read_targets(instance_dir)` is called against an instance where `config/core/.claude.json` exists on disk
- **THEN** the returned target list includes the absolute path to `.claude.json`; the unified setfacl pass therefore grants the daemon `u:<host_user>:r` on the RW recipe source so the helper-cp recipe's `cp /p/.claude.json /tmp/.claude.json` step succeeds (the daemon process backing the bind mount has DAC read on the dev-owned source)

#### Scenario: Missing files are skipped defensively

- **WHEN** `_grant_post_hydrate_daemon_read` encounters a recipe-table entry OR a `DAEMON_READ_DIRECT_FILES` entry whose file does not exist on disk
- **THEN** the helper skips that entry without raising; `setfacl` is not invoked for the missing path. (Covers the conditional compose extras when the matching `InstanceConfig` component flag is disabled.)

#### Scenario: setfacl failure surfaces as SandboxExecutionError

- **WHEN** the per-file `setfacl -m u:<host_user>:r <path>` fails (e.g., EPERM, ENOENT) for any post-hydrate target
- **THEN** `_grant_post_hydrate_daemon_read` raises `SandboxExecutionError` mentioning the path and "grant daemon read on post-hydrate target"; the failure is not silently swallowed

### Requirement: Helper Recipe Phases (unlink+cp+chmod+chown discipline)

The system SHALL implement the helper-cp file ownership transfer as an
in-helper-container shell sequence with the explicit ordering
``cp /p/$f /tmp/$f && unlink /p/$f && cp /tmp/$f /p/$f && chmod
<mode> /p/$f && chown <uid>:<gid> /p/$f``. Cross-filesystem ``mv`` MUST
NOT be used (it strips extended ACLs from the destination). ``chmod``
MUST precede ``chown`` (post-userns translation, in-container root
cannot chmod a foreign-owned file even with ``cap_dac_override``).

#### Scenario: helper_chown_files inner shell ordering
- **WHEN** ``core.helper_container.helper_chown_files`` is invoked
- **THEN** the inner shell command run inside the helper container
  contains, in order, ``cp /p/"$f" /tmp/"$f"``, ``unlink /p/"$f"``,
  ``cp /tmp/"$f" /p/"$f"``, ``chmod <mode> /p/"$f"``, ``chown
  <uid>:<gid> /p/"$f"``, AND does NOT contain ``mv`` for any per-file
  step

#### Scenario: chmod precedes chown
- **WHEN** the helper-cp recipe runs against a file destined for a
  consumer subuid
- **THEN** the ``chmod`` step appears in the inner shell BEFORE the
  ``chown`` step, so the chmod runs while the file is still owned by
  in-container root and does not EPERM under ``cap_dac_override``

#### Scenario: unlink preserves parent default ACL
- **WHEN** the helper recipe re-creates a file inside a parent dir that
  carries a default ACL ``u:<host_user>:r``
- **THEN** the new inode inherits the parent's default ACL because the
  pair (``unlink`` + ``cp``) creates a fresh inode within the same
  filesystem (no cross-fs copy that would strip extended ACLs)

### Requirement: Helper-Cp Default ACL Inheritance

The system SHALL grant a default ACL ``u:<host_user>:r`` on each
helper-cp parent directory listed in ``RO_FILE_RECIPES`` (``config/coredns``,
``config/dnsdist``, ``config/proxy``, ``config/core``, ``config/admin``,
``secrets``). The default ACL is defense-in-depth for write paths that
do not trigger ``write_restricted``'s ``fchmod`` mask reset; it
complements (does not replace) the post-hydrate setfacl pass owned by
the ``Helper-CP Source Files Daemon-Readable Pre-Recipe`` requirement.

#### Scenario: default-ACL grant present per helper-cp parent
- **WHEN** ``_acl_grant_plan`` is queried with a populated instance dir
- **THEN** for each helper-cp parent dir listed above, the plan
  contains a ``setfacl -d -m u:<host_user>:r <parent>`` entry

#### Scenario: default-ACL is defense-in-depth, not load-bearing
- **WHEN** a file is created inside a helper-cp parent via the
  ``write_restricted`` path (which fchmods immediately after open)
- **THEN** the inherited default-ACL named entry is masked out by
  ``mask::---`` AND the post-hydrate setfacl pass (from sibling
  requirement ``Helper-CP Source Files Daemon-Readable Pre-Recipe``)
  is required to restore effective daemon read; the default-ACL alone
  is sufficient ONLY for write paths that preserve inherited ACLs

### Requirement: Consumer-Uid-Only Sidecar Mechanism

The system SHALL declare ``user: "13:13"`` and pin ``entrypoint:
["/usr/sbin/squid"]`` for the proxy service in ``compose.yml`` so the
container starts directly as the in-container ``squid`` uid (= host
subuid 13 post-userns translation) and bypasses the OCI image's stock
entrypoint. Required because under gVisor + ``read_only: true`` the
stock entrypoint's start-as-root + drop-to-worker pattern EPERMs
reading the helper-cp-transferred ``squid.conf`` (mode ``0640`` owned
by the consumer uid).

#### Scenario: proxy compose declares uid 13:13 directly
- **WHEN** the rendered ``compose.yml`` template is read
- **THEN** the ``proxy`` service block contains ``user: "13:13"``

#### Scenario: proxy compose pins direct squid entrypoint
- **WHEN** the rendered ``compose.yml`` template is read
- **THEN** the ``proxy`` service block contains ``entrypoint:
  ["/usr/sbin/squid"]`` (the OCI image's stock entrypoint is NOT
  invoked)

### Requirement: Read-Only Rootfs Sidecar Configuration

The system SHALL pin the squid template directives ``pid_filename
none``, ``access_log stdio:/dev/stderr``, and ``cache_log stderr``.
Required because the proxy container runs ``read_only: true`` plus a
fixed tmpfs set; squid's default pid file (``/var/run/squid/squid.pid``)
and log paths under ``/var/log/squid/`` are not writable by the
post-uid-pivot worker. Logs are captured by docker's logging driver
from the container's stderr stream.

#### Scenario: squid template disables on-disk pid recording
- **WHEN** the squid template is read
- **THEN** the directive ``pid_filename none`` is present

#### Scenario: squid template redirects logs to stderr
- **WHEN** the squid template is read
- **THEN** the directives ``access_log stdio:/dev/stderr`` and
  ``cache_log stderr`` are both present

### Requirement: Teardown Sequence

The system SHALL implement a single shared teardown sequence
`_phase_stop_teardown(instance_dir, project_name, host_user, config,
workspace_paths, *, volumes, auth) -> list[str]` invoked by both
`sandbox stop` and `sandbox destroy` (D5+D6 phase). The sequence runs
exactly three phases in this order:

1. `_compose_down(..., volumes=<arg>, ...)` — `docker compose down [-v]`
   via `machinectl shell`. May raise `SandboxExecutionError`.
2. `_phase_stop_unlink_consumer_files(instance_dir, host_user)` —
   unlinks every helper-cp-managed file enumerated in
   `_helper_cp_chown_plan`. Fault-isolated; returns warning strings.
3. `_revoke_acls(instance_dir, host_user, workspace_paths)` — runs
   `_acl_revoke_plan` entries with `check=False`. Fault-isolated;
   returns warning strings.

Steps 2 and 3 SHALL run in this order so the named-ACL revoke (step 3)
operates on dev-owned parents only. Step 1 is the caller-controlled
phase: `stop` propagates a failed compose-down (the lifecycle is
recoverable); `destroy` demotes it to a warning AND still runs steps 2
and 3 inline (the destroy path is irreversible by design and MUST
proceed to rmtree regardless).

#### Scenario: Helper enforces compose-down → unlink → revoke ordering
- **WHEN** `_phase_stop_teardown` is invoked
- **THEN** `_compose_down` is called first, then
  `_phase_stop_unlink_consumer_files`, then `_revoke_acls`, in that
  exact order; the call-sequence is verifiable via mocked side-effects
  appending to a list

#### Scenario: stop delegates to the shared helper
- **WHEN** `sandbox stop <inst>` runs (instance is warm)
- **THEN** the inline compose-down + unlink + revoke is replaced by a
  single `_phase_stop_teardown(..., volumes=<clean>, ...)` call;
  `volumes=False` for default stop, `volumes=True` for `--clean`

#### Scenario: destroy D5 delegates to the shared helper
- **WHEN** `sandbox destroy <inst> --force` reaches D5 (compose down -v)
- **THEN** D5+D6 are implemented via
  `_phase_stop_teardown(..., volumes=True, ...)`; aggregated warnings
  are emitted via the existing `⚠ <warning>` channel

#### Scenario: destroy proceeds when compose-down fails
- **WHEN** `_compose_down` inside `_phase_stop_teardown` raises
  `SandboxExecutionError` during destroy
- **THEN** destroy catches the exception, demotes it to a warning, AND
  still invokes `_phase_stop_unlink_consumer_files` and `_revoke_acls`
  inline so the on-disk cleanup proceeds; the subsequent rmtree
  (irreversible D7) is unaffected

### Requirement: Acl Revoke Plan Excludes Persistent Grants

The `_acl_revoke_plan` function SHALL be a strict subset of the
`_acl_grant_plan` output by lifecycle: only grants whose lifecycle is
`granted-at-start, revoked-at-stop` are revoked. Grants with lifecycle
`granted-once, persistent` or `applied-on-every-start, idempotent,
never-revoked` SHALL NOT appear in the revoke plan.

Concretely, the revoke plan SHALL NOT contain:

- Ancestor traverse entries (`u:<host_user>:--x` on `_compute_ancestors`).
- Recursive walks (`-R` flag) on `docker/` or `config/`. Both are
  dir-level only because the recursive walk would EPERM on
  consumer-uid-owned files inside (dev lacks `CAP_FOWNER`).
- Workspace shared-group `chgrp`/`chmod 2770`/`setgid` operations or
  default-mask resets (`m::`, `g::`, `o::`). Only the named per-user
  `setfacl -x u:<host>` portion of the workspace ACL is revoked.
- Cache/log entries (mechanism is `subuid-chown`, never `named-acl`;
  there is nothing to setfacl-revoke).
- The `.sandbox.env` named-ACL entry (now persistent — see
  `Environment File Read ACL`).

The revoke plan SHALL contain dir-level `setfacl -x u:<host>` entries
for: instance root, `docker/` (dir-level), `config/` (dir-level),
`secrets/` (dir-level traverse), and per-workspace named-ACL +
default named-entry.

#### Scenario: Revoke plan contains no recursive walks
- **WHEN** `_acl_revoke_plan` is called for any instance dir
- **THEN** no entry's argv contains the `-R` flag

#### Scenario: Revoke plan does not touch helper-cp parents
- **WHEN** `_acl_revoke_plan` is called and the helper-cp parent set
  `{config/coredns, config/dnsdist, config/proxy, config/core,
  config/admin, secrets}` is enumerated
- **THEN** no recursive entry's target equals any of those paths

#### Scenario: Revoke plan excludes ancestor traverse
- **WHEN** `_acl_revoke_plan` is called
- **THEN** no entry description contains the substring "ancestor"

#### Scenario: Revoke plan excludes workspace shared-group persistent ops
- **WHEN** `_acl_revoke_plan` is called with workspace_paths
- **THEN** no entry description contains "chgrp" or "setgid"; the only
  `-d` entries on workspace paths revoke a single named user entry
  (argv contains `-x u:<host_user>`), never a default-mask reset

#### Scenario: Revoke plan excludes the env file
- **WHEN** `_acl_revoke_plan` is called
- **THEN** no entry description contains `.sandbox.env` and no entry's
  argv mentions `.sandbox.env`

#### Scenario: Revoke plan is a strict subset of grant plan paths
- **WHEN** both `_acl_grant_plan` and `_acl_revoke_plan` are called
  with the same arguments
- **THEN** every revoke target path appears as a grant target path
  (revoke ⊆ grant by path); the converse asymmetry is the load-bearing
  direction (grant has persistent entries that revoke does not)

### Requirement: Consumer-Uid-0-Chown Revoke Spec

The system SHALL revoke the `consumer-uid-0-chown` mechanism (used by
helper-cp-managed files in `RO_FILE_RECIPES` and `EXEC_FILE_RECIPES`)
via `unlink at stop + recreate-via-hydrate at next start`, NOT via
setfacl. `_phase_stop_unlink_consumer_files` is the recipe-symmetry
partner of `_phase_helper_cp_chown_ro_files`. On the next `sandbox
start`, hydration's `O_CREAT|O_TRUNC` creates fresh dev-owned files;
the helper-cp+chown phase then re-transfers ownership to the consumer
subuid. The consumer-uid-0-chown lifecycle is therefore:

- **Lifecycle**: `granted-at-start, revoked-at-stop`.
- **Mechanism**: `unlink + rehydrate` (NOT `setfacl -x`; consumer files
  are owned by an unmapped subuid, so dev lacks `CAP_FOWNER` to setfacl
  them).
- **Symmetry partner**: every file that
  `_phase_helper_cp_chown_ro_files` writes at start is unlinked by
  `_phase_stop_unlink_consumer_files` at stop.

#### Scenario: helper-cp file inventory has a 1:1 stop counterpart
- **WHEN** `_helper_cp_chown_plan` is the source of truth at start
- **THEN** `_phase_stop_unlink_consumer_files` iterates the same plan
  output and unlinks every `(parent_abs, file)` pair, fault-isolated;
  per-file FileNotFoundError is silent, per-file OSError aggregates
  into a returned warning string

#### Scenario: stop unlink runs before named-ACL revoke
- **WHEN** `_phase_stop_teardown` runs the teardown sequence
- **THEN** `_phase_stop_unlink_consumer_files` runs before
  `_revoke_acls` so the named-ACL revoke walks dev-owned parents only
  (consumer-owned files have already been removed)

#### Scenario: hydration recreates the files on next start
- **WHEN** `sandbox start <inst>` runs after a `sandbox stop` cycle
- **THEN** the hydrate phase's `O_CREAT|O_TRUNC` re-creates each
  helper-cp-managed file as dev-owned; the helper-cp+chown phase then
  re-transfers ownership to the consumer subuid; the lifecycle is
  fully recoverable across stop/start

### Requirement: RW Config File Recipes

The `cli.main` module SHALL define `RW_FILE_RECIPES` as a sibling-table constant alongside `RO_FILE_RECIPES` and `EXEC_FILE_RECIPES`. Each entry is `(parent_relative_to_instance, files, in_container_consumer_uid, mode)` with mode `0o660` (consumer user + primary group rw, "other" `---`). The RW kind is structurally distinct from `RO_FILE_RECIPES` (mode `0o640` ro configs / `0o600` secrets — bind-mounted `:ro`) and `EXEC_FILE_RECIPES` (mode `0o500` owner-only — bind-mounted `:ro`): RW recipe files are bind-mounted `:rw` into the consumer container and the consumer MUST be able to write them.

`_helper_cp_chown_plan` SHALL iterate `RO_FILE_RECIPES`, `EXEC_FILE_RECIPES`, AND `RW_FILE_RECIPES` in a single pass so the helper-cp+chown phase processes all three kinds uniformly. The stop-time symmetry partner (`_phase_stop_unlink_consumer_files`) inherits the new entries automatically because it iterates the same plan singleton; RW files are unlinked at stop and recreated by hydration on the next start.

The current entry is `("config/core", (".claude.json",), 1000, 0o660)`. `.claude.json` is dev-created by `core.hydration` (programmatic generation, not a static copy, because the firecrawl MCP endpoint is dynamic) and bind-mounted RW into the agent (core) container at `/home/agent/.claude.json`. Without consumer-uid ownership transfer the file presents as `nobody:nobody ---` from inside the agent's userns and the agent's write fails with EACCES even though the compose mount is `:rw` — the empirical symptom this requirement closes.

If a future bind mount adds a new dev-created RW single-file mount, `RW_FILE_RECIPES` MUST be extended in lockstep with the compose template change. If the new entry's parent is NOT already a helper-cp parent (i.e., not present in the per-helper-cp-parent dir-level `rwx` grants under `ACL Grant Plan as Single Source of Truth`), `_acl_grant_plan` MUST also gain a dir-level `u:<host_user>:rwx` + helper-cp parent default ACL `u:<host_user>:r` pair for that parent, parallel to the existing helper-cp parents. Today's only entry shares `config/core` with the existing dotfile RO recipes, so no `_acl_grant_plan` change is required by this requirement.

#### Scenario: RW_FILE_RECIPES contains .claude.json at mode 0660

- **WHEN** `cli.main.RW_FILE_RECIPES` is inspected
- **THEN** it contains `("config/core", (".claude.json",), 1000, 0o660)`; the consumer uid is 1000 (the in-container `agent` user) and the mode is `0o660` (consumer user + primary group rw)

#### Scenario: RW recipes do not collide with RO or EXEC tables

- **WHEN** `cli.main.RO_FILE_RECIPES` and `cli.main.EXEC_FILE_RECIPES` are inspected
- **THEN** no entry has parent `config/core` and files containing `.claude.json`; the RW config kind belongs to `RW_FILE_RECIPES` exclusively (the bind-mount mode `:ro` vs `:rw` is the schema-level invariant separating the categories)

#### Scenario: Helper-cp plan iterates the RW table alongside RO + EXEC

- **WHEN** `_helper_cp_chown_plan(instance_dir, host_user)` is called
- **THEN** the returned plan length is `len(RO_FILE_RECIPES) + len(EXEC_FILE_RECIPES) + len(RW_FILE_RECIPES)`; entries from all three tables appear, each with the consumer-mapped owner uid/gid and the table's stated mode; the RW entry for `config/core/.claude.json` is present at mode `0o660`

#### Scenario: Stop-time unlink covers RW recipe files

- **WHEN** `_phase_stop_unlink_consumer_files(instance_dir, host_user)` is invoked at stop or destroy
- **THEN** it unlinks every file enumerated by `_helper_cp_chown_plan`, including the RW recipe entries; on the next `sandbox start`, hydration writes a fresh dev-owned restrictive-mode replacement and the helper-cp recipe re-installs consumer ownership at the RW recipe's mode

#### Scenario: Compose-mount intent corresponds to recipe table

- **WHEN** the compose template is inspected for dev-created single-file bind mounts
- **THEN** every file mounted `:ro` from a dev-created path appears in `RO_FILE_RECIPES` or `EXEC_FILE_RECIPES`; every file mounted `:rw` from a dev-created path appears in `RW_FILE_RECIPES`. (Workspace `:rw` mounts and cache/log `:rw` directory mounts are NOT in scope for any of the file-recipe tables — they are handled by the workspace shared-group recipe and the helper-mkdir-chown recipe respectively.)

### Requirement: Plan Items Are Typed Action Objects

Each `_*_plan` function (`_acl_grant_plan`, `_acl_revoke_plan`, `_helper_mkdir_chown_plan`, `_helper_cp_chown_plan`, `_workspace_shared_group_plan`, `_compose_up_cmd_plan`) SHALL return a list of typed `Action` objects (subclasses of `core.actions.base.Action`) rather than raw tuples or strings. Each `Action` SHALL expose `describe(self) -> str` (the line rendered by the dry-run preview) and `execute(self, ctx: ActionContext) -> None` (the live execution path). The dry-run preview and the live phases SHALL consume the same `Action` instances — the dry-run path calls `.describe()`, the live path calls `.execute(ctx)`. No code path is permitted to bypass an `Action` by reconstructing the underlying argv from the plan's input data; the `Action` is the single carrier of both semantics.

This requirement makes the Command pattern (anti-hack rule 7: "Plan and execute share data") structural rather than conventional. Every spec elsewhere in this capability that constrains the *content* of a plan (command strings, descriptions, target paths, owner uid/gid, mode) continues to apply unchanged — those constraints are now satisfied via the `Action`'s `.command` / `.description` / `.target` / typed fields rather than positional tuple indexing.

This requirement is ADDED rather than MODIFIED on an existing requirement (such as "ACL Grant Plan as Single Source of Truth") because the existing single-source-of-truth requirements constrain the *content* of plans (which functions exist, what target paths and command strings they produce, what uid/gid/mode each entry carries) but are silent on the *encoding* of plan items — they would be satisfied by tuples, dataclasses, namedtuples, or dicts equally. This new requirement adds an orthogonal constraint on the encoding (must be a typed `Action` subclass with `.describe()` / `.execute()`); keeping it independently auditable preserves a clean separation of concerns and avoids conflating two independent dimensions of the contract.

#### Scenario: ACL grant plan returns typed grant Actions

- **GIVEN** an instance directory with a populated `[workspaces]` section
- **WHEN** `_acl_grant_plan(instance_dir, host_user)` is called
- **THEN** every item in the returned list is an instance of `core.actions.acl.NamedAclGrantAction`
- **AND** each item exposes `.describe()` returning the same human-readable line the pre-refactor code would have printed for the equivalent tuple
- **AND** each item exposes `.execute(ctx)` which, when called with a valid `ActionContext`, issues the corresponding `setfacl` invocation via `ctx.executor` using the auth mode from `ctx.auth`
- **AND** no item in the returned list is a `tuple` or any other non-`Action` type

#### Scenario: helper-cp plan returns typed helper-cp Actions

- **GIVEN** an instance directory and a host user
- **WHEN** `_helper_cp_chown_plan(instance_dir, host_user)` is called
- **THEN** every item in the returned list is an instance of `core.actions.helper_cp.HelperCpChownAction`
- **AND** each item carries `.parent`, `.files`, `.owner_uid`, `.owner_gid`, `.mode` as typed attributes (not positional indices)
- **AND** the per-entry `(owner_uid, owner_gid, mode)` values match the corresponding entries in `RO_FILE_RECIPES + EXEC_FILE_RECIPES + RW_FILE_RECIPES`, satisfying the `Helper-CP Recipe Iterates Both Tables` requirement via attribute access rather than tuple unpacking

#### Scenario: dry-run and live execution share the same Action instance

- **GIVEN** any plan returned by an `_*_plan()` function
- **WHEN** the dry-run preview iterates the plan and calls `.describe()` on each item
- **AND** the corresponding `_phase_*` function iterates the same plan (or a re-invocation that returns equivalent items) and calls `.execute(ctx)` on each item
- **THEN** the inputs read by `.describe()` (target path, command argv, description string) are the same fields read by `.execute(ctx)` — there is no parallel reconstruction of the argv inside `.execute()`
- **AND** the `cli-start` capability's "Live and dry-run derive compose up from a shared plan helper" requirement is satisfied structurally: the `ComposeUpAction.inner_command` field is the single carrier consumed by both code paths

