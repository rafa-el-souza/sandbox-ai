# ACL/ownership model (lifecycle × mechanism)

The model (defined by the `orchestrator-volumes` spec) is a two-axis taxonomy: lifecycle (when the operation applies / when it is reversed) × mechanism (what host operation is performed).

**Lifecycles**:
- `granted-at-start, revoked-at-stop` — applied during `sandbox start`, reversed during `sandbox stop`/`destroy`.
- `granted-once, persistent` — applied during the first start, never revoked (e.g. ancestor traverse, workspace shared-group).
- `applied-on-every-start, idempotent, never-revoked` — re-applied every start; preserved across stop/start cycles (e.g. cache/log subuid chowns).

**Mechanisms**:
- `named-acl` — `setfacl -m u:<user>:<perms>` and its reverse. Used for instance dir, `docker/`, `config/`, `.sandbox.env`, `secrets/` traverse, and the workspace effective + named-default-entry portion.
- `subuid-chown` — chown to the consumer's host subuid via the disposable helper container. Used for cache/log leaves with the parent's default ACL granting `u:dev:rwx` so dev can read agent-created files. Mechanism-owned directories are NOT pre-created by `sandbox init`; the helper recipe creates them on first start (the Scaffold-vs-Helper Boundary: a scaffold-pre-created leaf would be unmapped in the daemon's userns and would EPERM the helper's chown).
- `consumer-uid-0-chown` — chown to `<consumer-uid>:<consumer-gid>` mode `0640` (ro config) or `0600` (secrets) via the helper container. Mode mapping in `cli.main.RO_FILE_RECIPES`. Group ownership matches the consumer's host subgid; in-container root reads via `cap_dac_override` (in the helper's cap-add baseline), not via group permissions. Mechanism-owned files are NOT pre-created by `sandbox init`; the helper recipe creates them via cp-then-chown on first start (same Scaffold-vs-Helper Boundary as `subuid-chown`).
- `shared-group` — `chgrp <bridge-gid> + chmod 2770 + setfacl` on the workspace tree, with the agent's container picking up the bridge gid via `--group-add` (computed at hydration time as `in_container_gid_for_host_gid(host_bridge_gid, <docker_unprivileged_user>)`).

The bridge group is resolved via `[host].workspace_bridge_group` (default `sb-ws`); the orchestrator never invokes `sudo`, so operators set up the group manually via `groupadd -g <gid-in-subgid-range> sb-ws` + `usermod -aG sb-ws $USER` (then re-login). `sandbox doctor` autodetects a recommended gid and prints copy-pasteable commands when the group is missing.

When touching filesystem permissions, identify which (lifecycle, mechanism) pair applies before changing anything. Plans live in `cli.main._acl_grant_plan`, `_acl_revoke_plan`, `_helper_mkdir_chown_plan`, `_helper_cp_chown_plan`, `_workspace_shared_group_plan` — single source of truth shared with the dry-run preview.
