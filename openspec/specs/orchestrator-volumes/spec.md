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
- `granted-at-start, revoked-at-stop` — operation applied during `sandbox start` Phase 5 and reversed during `sandbox stop` / `sandbox destroy` revocation.
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
| Ancestor traverse `--x` chain (above the instance dir AND each workspace path, walking up to the ownership boundary; deduplicated at execution) | granted-once, persistent | named-acl |
| Cache/log dir leaves (`cache/<svc>/...`, `log/<svc>/...` per the bind-mount inventory) | applied-on-every-start, idempotent, never-revoked | subuid-chown + parent default ACL `u:dev:rwx` |
| Ro single-files (Corefile, dnsdist conf, all 5 proxy files, core/admin dotfiles, sshd_config) | applied-on-every-start, idempotent, never-revoked | consumer-uid-0-chown (mode 0640) |
| Secrets (authorized_keys, ipc_*) | applied-on-every-start, idempotent, never-revoked | consumer-uid-0-chown (mode 0600) |
| **Each workspace's** named ACL on its `path` (effective AND default-ACL named entries) | granted-at-start, revoked-at-stop | named-acl |
| **Each workspace's** shared-group state on its `path` (chgrp, chmod 2770+setgid, persistent default ACL portion `u::rwx,g::rwx,o::---,m::rwx,u:dev:rwx`) | granted-once, persistent | shared-group |
| `_backups/` tree (lazy-created on first backup, dev-owned mode 0700) | none | none (excluded from ACL/recipe planning per "_backups Tree Excluded" requirement) |

A single mount may carry multiple (lifecycle, mechanism) pairs. Each workspace is the load-bearing example: its named ACL is granted-at-start/revoked-at-stop, while its group/mode/persistent-default-ACL is granted-once/persistent.

#### Scenario: Lifecycle × mechanism taxonomy is the spec's source of truth
- **WHEN** any new mount class is added to the orchestrator
- **THEN** the spec assigns it one or more (lifecycle, mechanism) pairs from the table; ad-hoc mechanisms outside the taxonomy are NOT introduced

#### Scenario: Named-ACL grants — instance dirs — applied at start
- **WHEN** `sandbox start <inst>` reaches Phase 5 (ACL grants)
- **THEN** `setfacl -R -m u:<host_unprivileged_user>:rX` is applied to `instances/<inst>/docker/`; `setfacl -m u:<host_unprivileged_user>:rX` to `instances/<inst>/config/` (dir-level — individual files inside are chowned per the consumer-uid-0-chown class); `setfacl -m u:<host_unprivileged_user>:r-x` to `instances/<inst>/`; `setfacl -m u:<host_unprivileged_user>:r` to `instances/<inst>/.sandbox.env`

#### Scenario: Named-ACL grants — instance dirs — revoked at stop
- **WHEN** `sandbox stop <inst>` or `sandbox destroy <inst>` executes ACL revocation
- **THEN** `setfacl -x u:<host_unprivileged_user>` is applied to `instances/<inst>/docker/`, `instances/<inst>/config/`, `instances/<inst>/`, and `instances/<inst>/.sandbox.env`, using fault-isolated revocation

#### Scenario: Cache/log subuid-chown recipe — applied every start
- **WHEN** `sandbox start` reaches the cache/log helper-recipe phase (after Phase 5 ACL grants)
- **THEN** for each cache/log leaf in the bind-mount inventory (`cache/core/.claude`, `cache/admin/tmux_resurrect`, `log/core`, `log/admin`): the parent dir's default ACL is set to `u::rwx,g::---,o::---,m::rwx,u:dev:rwx`; `helper_mkdir_chown_dirs` runs to ensure the leaf exists and is owned by `host_id_for_in_container(1000, host_user):host_gid_for_in_container(1000, host_user)`. Operation is idempotent: re-running on existing-correct state is a no-op.

#### Scenario: Cache/log subuid-chown — never revoked on stop
- **WHEN** `sandbox stop` executes
- **THEN** cache/log leaves remain subuid-owned; the default ACL on the parent is preserved; agent state is preserved across stop/start cycles

#### Scenario: Ro single-files consumer-uid-0-chown recipe — applied every start
- **WHEN** `sandbox start` reaches the ro-files helper-recipe phase (after Phase 5 ACL grants)
- **THEN** `helper_chown_files` is invoked once per (consumer-uid, mode) group, batching all files sharing the same target ownership/mode into a single helper container

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

#### Scenario: Ro single-files on-disk gid matches consumer's host subgid
- **WHEN** any file in the consumer-uid-0-chown recipe table has been chowned by `helper_chown_files`
- **THEN** the resulting on-disk gid is `host_gid_for_in_container(<in-container uid from table>, host_user)` — i.e., the consumer's host subgid paired with the consumer's host subuid; it is NOT the daemon user's primary gid (the historic literal-0 pattern was removed because it was incompatible with the host-absolute helper API and provided no protection that `cap_dac_override` doesn't already grant)

#### Scenario: Per-workspace named-ACL — applied at start
- **WHEN** `sandbox start <inst>` reaches Phase 5 (ACL grants) for an instance with workspaces `main` and `scratch`
- **THEN** for EACH workspace: `setfacl -m u:<host_unprivileged_user>:rwx <ws.path>` is applied (effective ACL); `setfacl -d -m u::rwx,g::rwx,o::---,m::rwx,u:<host_unprivileged_user>:rwx,u:dev:rwx <ws.path>` is applied (default ACL containing the host_user named entry)

#### Scenario: Per-workspace named-ACL revocation includes default ACL host_user entry
- **WHEN** `sandbox stop <inst>` or `sandbox destroy <inst>` executes ACL revocation
- **THEN** for EACH workspace: `setfacl -x u:<host_unprivileged_user> <ws.path>` removes the effective named entry AND `setfacl -d -x u:<host_unprivileged_user> <ws.path>` removes the default-ACL named entry (symmetric revocation per change 4 Decision 4); the persistent portion of each workspace's default ACL (`u::rwx, g::rwx, o::---, m::rwx, u:dev:rwx`) is preserved

#### Scenario: Per-workspace shared-group state — applied once, persistent
- **WHEN** `sandbox start <inst>` reaches `_phase_workspace_shared_group` and a workspace's root does NOT have setgid+correct-group state (drift detection per change 4 Decision 17)
- **THEN** `chgrp -R <bridge-group> <ws.path>` (best-effort, dev-owned files only); `find <ws.path> -type d -exec chmod 2770 {} +`; `find <ws.path> -type f -exec chmod 0660 {} +`; the persistent portion of the default ACL is set on `<ws.path>`

#### Scenario: Per-workspace shared-group state — steady-state idempotency
- **WHEN** `sandbox start <inst>` reaches the shared-group phase and a workspace's root already has setgid+correct-group
- **THEN** the recursive operation is skipped for that workspace; only root-state idempotent assertions run (one stat call cost)

#### Scenario: Per-workspace shared-group state — never revoked
- **WHEN** `sandbox stop <inst>` or `sandbox destroy <inst>` executes
- **THEN** chgrp, chmod 2770, setgid bit, and the persistent portion of the default ACL on EACH `<ws.path>` are NOT touched (per change 4 Decision 4: persistent identity properties)

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
The system SHALL grant read-only ACL on `.sandbox.env` so the sandbox user's `docker compose` process can parse `env_file:` directives and `--env-file` interpolation. The `.sandbox.env` lives at `<sandbox_ai_home()>/instances/<inst>/.sandbox.env`.

#### Scenario: .sandbox.env ACL granted at start
- **WHEN** `sandbox start <inst>` reaches Phase 5 (ACL grants)
- **THEN** `setfacl -m u:<host_unprivileged_user>:r <sandbox_ai_home()>/instances/<inst>/.sandbox.env` is applied

#### Scenario: .sandbox.env ACL revoked at stop
- **WHEN** `sandbox stop <inst>` executes ACL revocation
- **THEN** `setfacl -x u:<host_unprivileged_user> <sandbox_ai_home()>/instances/<inst>/.sandbox.env` is applied

#### Scenario: Missing .sandbox.env fails explicitly
- **WHEN** `.sandbox.env` does not exist at Phase 5
- **THEN** `setfacl` fails and the error is surfaced as a `SandboxExecutionError` indicating instance corruption (no silent skip)

### Requirement: ACL Grant Plan as Single Source of Truth
The system SHALL define ACL grant targets in a single function (`_acl_grant_plan`) consumed by both the execution path and the dry-run preview. The grant plan SHALL include named-acl operations only; helper-recipe phases (subuid-chown, consumer-uid-0-chown, shared-group) are separate phases with their own plans (`_helper_mkdir_chown_plan`, `_helper_cp_chown_plan`, `_workspace_shared_group_plan`). Each plan is its own single source of truth for its mechanism. Plans iterate `[workspaces]` (sorted by name) where applicable.

#### Scenario: Grant plan consumed by execution
- **WHEN** `_phase_acl_grant` executes Phase 5
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

The system SHALL execute ownership-sensitive phases in a specific order during `sandbox start` such that intermediate filesystem states between phases are mode-restrictive (per Decision 6) and access-controlled. The order SHALL be: `_phase_credentials → _phase_hydrate → _phase_acl_grant → _phase_helper_mkdir_chown_cache_log → _phase_helper_cp_chown_ro_files → _phase_workspace_shared_group → _phase_compose_up`. Reordering ANY of these phases is a behavioral change requiring a new spec change.

#### Scenario: Phase order matches the contract
- **WHEN** `sandbox start` runs
- **THEN** the phase invocation order is: ipam, credentials, hydrate, acl_grant, helper_mkdir_chown_cache_log, helper_cp_chown_ro_files, workspace_shared_group, compose_up

#### Scenario: ACL grant precedes helper phases
- **WHEN** the helper-recipe phases run
- **THEN** the named-ACL grants on parent dirs (config/, secrets/) have already been applied, so the daemon (claude-sandbox / gofer) can traverse to the file targets

#### Scenario: Compose up follows all ownership phases
- **WHEN** `_phase_compose_up` is invoked
- **THEN** all helper-recipe phases have completed; in-container services see the ownership/mode state established by those phases

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

#### Scenario: Tmux resurrect mount in admin service
- **WHEN** the rendered `compose.yml` is inspected for the admin service
- **THEN** it contains a volume entry `{{ instance_dir }}/cache/admin/tmux_resurrect:{{ tmux_resurrect_dir }}:rw`

### Requirement: Container-Namespaced Cache Directory
The `cache/` subtree under each instance dir SHALL follow a container-namespaced convention. The Claude Code cache directory SHALL be at `cache/core/.claude` (not `cache/.claude`). Paths are relative to the instance dir at `<sandbox_ai_home()>/instances/<inst>/`.

#### Scenario: Scaffold creates namespaced cache directory
- **WHEN** scaffold creates a new instance via `INSTANCE_SUBDIRS`
- **THEN** `<sandbox_ai_home()>/instances/<inst>/cache/core/.claude` is created (not `cache/.claude`)

#### Scenario: Scaffold creates tmux resurrect cache directory
- **WHEN** scaffold creates a new instance via `INSTANCE_SUBDIRS`
- **THEN** `<sandbox_ai_home()>/instances/<inst>/cache/admin/tmux_resurrect` is created

#### Scenario: Compose mount references namespaced cache path
- **WHEN** the rendered `compose.yml` is inspected for the core service
- **THEN** the `.claude` directory mount references `{{ instance_dir }}/cache/core/.claude` (not `{{ instance_dir }}/cache/.claude`)

### Requirement: Stale Proxy Seed File Removal
The following files in `templates/config/proxy/` SHALL be absent from the tooling plane: `allowed_domains.txt`, `trusted_clients.acl`, `.htpasswd`. These files are overridden by programmatic generation in `render_templates()` or by `core/crypto.py` during scaffold. Their presence in the tooling plane is misleading — edits to them have no effect.

#### Scenario: No stale proxy seed files in tooling plane
- **WHEN** the `templates/config/proxy/` directory in the tooling plane is inspected
- **THEN** it contains only `squid.conf` and `ERR_SANDBOX_403` (the Jinja2 template and the static error page)
