# ACL/ownership model (lifecycle × mechanism)

The orchestrator-volumes capability uses an orthogonal taxonomy: lifecycle (when does the operation apply / when is it reversed) × mechanism (what host operation is performed).

## Lifecycles

- `granted-at-start, revoked-at-stop` — applied during `sandbox start`, reversed during `sandbox stop`/`destroy`.
- `granted-once, persistent` — applied during the first start, never revoked (e.g. ancestor traverse, workspace shared-group).
- `applied-on-every-start, idempotent, never-revoked` — re-applied every start; preserved across stop/start cycles (e.g. cache/log subuid chowns).

## Mechanisms

| Mechanism | Host operation | Used for | Not pre-created by `sandbox init`? |
| --- | --- | --- | --- |
| `named-acl` | `setfacl -m u:<user>:<perms>` and its reverse. | Instance dir, `docker/`, `config/`, `.sandbox.env`, `secrets/` traverse, and the workspace effective + named-default-entry portion. | |
| `subuid-chown` | chown to the consumer's host subuid via the disposable helper container. | Cache/log leaves with the parent's default ACL granting `u:dev:rwx` so dev can read agent-created files. | Yes — the helper recipe creates them on first start (per `orchestrator-volumes`'s "Scaffold-vs-Helper Boundary" — a scaffold-pre-created leaf would be unmapped in the daemon's userns and would EPERM the helper's chown). |
| `consumer-uid-0-chown` | chown to `<consumer-uid>:<consumer-gid>` mode `0640` (ro config) or `0600` (secrets) via the helper container. Mode mapping in `cli.main.RO_FILE_RECIPES`. Group ownership matches the consumer's host subgid; in-container root reads via `cap_dac_override` (in the helper's cap-add baseline), not via group permissions. | ro config and secrets files. | Yes — the helper recipe creates them via cp-then-chown on first start (same Scaffold-vs-Helper Boundary as `subuid-chown`). |
| `shared-group` | `chgrp <bridge-gid> + chmod 2770 + setfacl` on the workspace tree, with the agent's container picking up the bridge gid via `--group-add` (computed at hydration time as `in_container_gid_for_host_gid(host_bridge_gid, claude-sandbox)`). | The workspace tree. | |

> **Note / history.** The literal-0 gid pattern (under `consumer-uid-0-chown`) was removed because it was incompatible with the host-absolute helper API and provided no protection that `cap_dac_override` doesn't already grant.

**Bridge group setup.** The bridge group name is a setup-determined fact read from `HostSettings.workspace_bridge_group` (marker-sourced via `HostConfig.from_marker`), not a user-editable config key. It is setup-derived: `sb-ws-<operator>` in operator-rootless (per-operator, with its gid allocated in that operator's own subgid range) and the shared `sb-ws` in separate-user. `sudo sandbox setup` creates the group at the in-range gid and adds the operator to it; the operator re-logins to pick up the new group membership. `sandbox doctor` verifies the group/gid and the operator's membership.

When touching filesystem permissions, identify which (lifecycle, mechanism) pair applies before changing anything.

**Where the plans live.** Plans are the single source of truth shared with the dry-run preview:

- `cli.main._acl_grant_plan`
- `cli.main._acl_revoke_plan`
- `cli.main._helper_mkdir_chown_plan`
- `cli.main._helper_cp_chown_plan`
- `cli.main._workspace_shared_group_plan`
