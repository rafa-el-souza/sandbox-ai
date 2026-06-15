# Two configuration scopes

**Per-host** — not user-editable; setup-determined.

Host provisioning facts are **not** a user-editable file. They are determined by `sudo sandbox setup` and recorded in a root-owned per-operator marker (`/usr/local/libexec/sandbox-ai/setup-state.json`); the runtime reads them back via `core.host_config.HostConfig.from_marker(operator)`. There is no per-host `sandbox-ai.toml` — it was retired (its only section was `[host]`, and every field is now setup-determined). A leftover `sandbox-ai.toml` from an older install is obsolete; `sandbox doctor` flags it as such and directs the operator to delete it and re-run `sudo sandbox setup`.

- **Built by**: `core.host_config.HostConfig.from_marker(operator)` — built-in defaults overlaid with the per-operator marker entry.
- **Marker facts** (per-operator, mode-conditional):

  | Fact | Meaning | Notes |
  | --- | --- | --- |
  | `mode` | `operator-rootless` (default) or `separate-user` | always present |
  | `docker_unprivileged_user` | the dedicated daemon-owner account | **separate-user only**; absent in operator-rootless, where the daemon runs as the invoking operator's own user |
  | `workspace_bridge_group` | the group used by the workspace shared-group recipe | setup-derived: `sb-ws-<operator>` (operator-rootless, per-operator) / `sb-ws` (separate-user, shared) |
  | `workspace_bridge_gid` | the gid of that group | per-operator, allocated in the operator's own subgid range (operator-rootless) / the single shared range (separate-user) |

- **Behavior**: written by `sudo sandbox setup`, never hand-edited. The "init has run" gate is `instances.json`.

> **Note**: The `SANDBOX_AI_HOME` env var redirects the per-instance state path for test isolation only. It is an environment override, not a config key.

**Per-instance**

- **Path**: `<sandbox_ai_home()>/instances/<inst>/sandbox.toml`.
- **Contents**: the `[workspaces]` map-of-tables holds one or more workspaces per instance. Related filesystem paths:

  - `<sandbox_ai_home()>/instances/<inst>/sandbox.toml` — the per-instance config file.
  - `<sandbox_ai_home()>/workspaces/<inst>/<ws>/` — each workspace tree.
  - `<sandbox_ai_home()>/workspaces/_backups/<inst>/<ws>/<UTC-timestamp>/` — accumulated backup snapshots.

- **Behavior**: generated during `sandbox init` and **re-hydrated on every `sandbox start`** via the Pydantic→Jinja2 pipeline in `core.hydration`. Drift is eliminated by regenerating compose/sidecar configs from the model on each start.
