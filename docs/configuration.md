# Two configuration scopes

**Per-host**

- **Path**: `<sandbox_ai_home()>/config/sandbox-ai.toml`, default `~/.sandbox-ai/config/sandbox-ai.toml`.
- **Parsed by**: `core.host_config.HostConfig`.
- **Keys** (`[host]` table):

  | Key | Meaning | Default |
  | --- | --- | --- |
  | `docker_unprivileged_user` | | |
  | `machinectl_authentication` | | `sudo` |
  | `workspace_bridge_group` | the group used by the workspace shared-group recipe | `sb-ws` |

- **Behavior**: seeded by `sandbox init` (TTY prompt or non-TTY fail).

> **Note**: The `SANDBOX_AI_HOME` env var redirects this path for test isolation only. It is an environment override, not a config key.

**Per-instance**

- **Path**: `<sandbox_ai_home()>/instances/<inst>/sandbox.toml`.
- **Contents**: the `[workspaces]` map-of-tables holds one or more workspaces per instance. Related filesystem paths:

  - `<sandbox_ai_home()>/instances/<inst>/sandbox.toml` — the per-instance config file.
  - `<sandbox_ai_home()>/workspaces/<inst>/<ws>/` — each workspace tree.
  - `<sandbox_ai_home()>/workspaces/_backups/<inst>/<ws>/<UTC-timestamp>/` — accumulated backup snapshots.

- **Behavior**: generated during `sandbox init` and **re-hydrated on every `sandbox start`** via the Pydantic→Jinja2 pipeline in `core.hydration`. Drift is eliminated by regenerating compose/sidecar configs from the model on each start.
