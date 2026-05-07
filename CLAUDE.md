# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`sandbox-ai` is a deterministic, zero-trust orchestrator that provisions isolated AI agent sandboxes. The CLI (`sandbox`) wraps Docker Compose lifecycles but executes every Docker call across a privilege boundary into an unprivileged systemd user via `machinectl shell`.

## Commands

The project pins Python 3.14 (`.python-version`) and depends on [`uv`](https://docs.astral.sh/uv/). Never call `python` directly — always go through `uv run` or `make`.

```bash
make test                       # unit tests (tests/unit)
make test-integration           # subprocess-level integration tests (tests/integration)
make test-file FILE=tests/unit/core/test_ipam.py   # single file
uv run pytest tests/unit/core/test_ipam.py::test_name   # single test
make coverage                   # enforces 100% on core/ + cli/
make lint                       # ruff
make typecheck                  # mypy --strict
make format                     # ruff format + --fix
```

`pytest.testpaths = ["tests/unit"]` — integration tests under `tests/integration/` are not collected by default and must be invoked explicitly.

The CLI entrypoint is `sandbox = "cli.main:app"` (typer). Run as `uv run sandbox <cmd>`.

## Architecture

### Privilege boundary (load-bearing)

Everything Docker-related crosses from the dev user into an unprivileged `sandbox` systemd user via `machinectl shell <user>@.host`. The prefix is built by `core.host_config.machinectl_cmd(user, auth)` and is either `["sudo", "machinectl", ...]` (SUDO mode) or `["machinectl", ...]` (POLKIT mode). **Never hardcode `sudo machinectl`** — always go through `machinectl_cmd()` so the auth mode from `sandbox-ai.toml` is respected. Recent commits (`f7ac8da`, `1648323`) refactored the codebase to enforce this; preserve it.

### Two configuration scopes

- **Per-host** (`<sandbox_ai_home()>/config/sandbox-ai.toml`, default `~/.sandbox-ai/config/sandbox-ai.toml`): parsed by `core.host_config.HostConfig`. Holds `[host].docker_unprivileged_user`, `[host].machinectl_authentication` (`sudo` | `polkit`), and `[host].workspace_bridge_group` (default `sb-ws`, the group used by the workspace shared-group recipe). Seeded by `sandbox init` (TTY prompt or non-TTY fail). `SANDBOX_AI_HOME` env var redirects this path for test isolation only.
- **Per-instance** (`<sandbox_ai_home()>/instances/<inst>/sandbox.toml`): generated during `sandbox init` and **re-hydrated on every `sandbox start`** via the Pydantic→Jinja2 pipeline in `core.hydration`. Drift is eliminated by regenerating compose/sidecar configs from the model on each start. The `[workspaces]` map-of-tables holds one or more workspaces per instance; each workspace tree lives under `<sandbox_ai_home()>/workspaces/<inst>/<ws>/`. Backup snapshots accumulate at `<sandbox_ai_home()>/workspaces/_backups/<inst>/<ws>/<UTC-timestamp>/`.

### Core modules (`src/core/`)

- `executor.py` — sterile POSIX subprocess execution (the only sanctioned way to shell out).
- `registry.py` — instance registry as fcntl-locked JSON at `<sandbox_ai_home()>/state/instances.json`.
- `ipam.py` — `/24` subnet septuple allocator (isolated, core_proxy, dns, admin, admin_proxy, egress, ipc) over 10.100.0.0–10.255.255.0 with lowest-slot scan and slot reuse (`MAX_SLOTS = 5705`).
- `hydration.py` — `InstanceConfig` Pydantic model → `build_jinja_context` → `render_templates` → `validate_templates`. Templates live in `src/templates/config/` and `src/templates/docker/` (the immutable tooling/config plane), shipped with the wheel as the top-level `templates` Python package and discovered via `jinja2.PackageLoader("templates", package_path="")` / `importlib.resources`.
- `scaffold.py` — bootstraps `<sandbox_ai_home()>/instances/<inst>/` (dirs, `.sandbox.env`, `sandbox.toml`, default ACLs, sentinel) plus per-workspace trees under `<sandbox_ai_home()>/workspaces/<inst>/`. `mutate_workspaces()` rewrites the `[workspaces]` block on add/remove/rename without disturbing operator hand-edits to other sections.
- `crypto.py` — bcrypt htpasswd, SSH keypair, credential generation for the proxy sidecar.
- `host_config.py` — `sandbox-ai.toml` loader + `machinectl_cmd()` builder + subuid/subgid resolvers (`host_id_for_in_container`, `in_container_gid_for_host_gid`, `workspace_bridge_gid`, `autodetect_workspace_bridge_gid_recommendation`).
- `helper_container.py` — disposable-helper-container primitives (`helper_chown_files`, `helper_mkdir_chown_dirs`) used by the helper-recipe phases. Pins `IMAGE_REGISTRY["busybox_musl"]`; runs every helper invocation with the full hardening baseline (runc runtime, network none, read-only rootfs, cap-drop ALL + cap-add CHOWN/DAC_OVERRIDE, no-new-privileges, tmpfs /tmp).
- `doctor.py` — host readiness check registry used by `sandbox doctor`.

### State and locking

- Mutable orchestrator state lives under `<sandbox_ai_home()>/state/` (default `~/.sandbox-ai/state/`): `instances.json`, `ipam.json`, `state.lock`, `ipam.json.lock`, and lazy per-instance `<inst>.backup.lock` files. The directory is created with mode `0700` by `sandbox init`.
- `state.lock` is now **per-user** (not per-CWD): all `sandbox` invocations under the same user serialize on the same lock during provisioning, regardless of which working directory they were launched from.
- `ipam.json.lock` is the dedicated mutation lock for the IPAM ledger (`core.host_config.ipam_lock_path()`), distinct from `state.lock`. `IPAMLedger.allocate` / `IPAMLedger.release` acquire only `ipam.json.lock`; they never touch `state.lock`. **Lock acquisition order: `state.lock` outer, `ipam.json.lock` inner — never the reverse.** No code path may acquire `state.lock` while already holding `ipam.json.lock`.
- `state.lock` is **transient** — held only during provisioning, released for the runtime lifetime of a sandbox. Don't add long-lived locks.
- Lifecycle commands (`start`, `stop`, `destroy`, `status`, `attach`) hard-fail with a "run sandbox init first" error when `<home>/state/instances.json` is absent.

### ACL/ownership model (lifecycle × mechanism)

The orchestrator-volumes capability uses an orthogonal taxonomy: lifecycle (when does the operation apply / when is it reversed) × mechanism (what host operation is performed).

**Lifecycles**:
- `granted-at-start, revoked-at-stop` — applied during `sandbox start`, reversed during `sandbox stop`/`destroy`.
- `granted-once, persistent` — applied during the first start, never revoked (e.g. ancestor traverse, workspace shared-group).
- `applied-on-every-start, idempotent, never-revoked` — re-applied every start; preserved across stop/start cycles (e.g. cache/log subuid chowns).

**Mechanisms**:
- `named-acl` — `setfacl -m u:<user>:<perms>` and its reverse. Used for instance dir, `docker/`, `config/`, `.sandbox.env`, `secrets/` traverse, and the workspace effective + named-default-entry portion.
- `subuid-chown` — chown to the consumer's host subuid via the disposable helper container. Used for cache/log leaves with the parent's default ACL granting `u:dev:rwx` so dev can read agent-created files.
- `consumer-uid-0-chown` — chown to `<consumer-uid>:0` mode `0640` (ro config) or `0600` (secrets) via the helper container. Mode mapping in `cli.main.RO_FILE_RECIPES`.
- `shared-group` — `chgrp <bridge-gid> + chmod 2770 + setfacl` on the workspace tree, with the agent's container picking up the bridge gid via `--group-add` (computed at hydration time as `in_container_gid_for_host_gid(host_bridge_gid, claude-sandbox)`).

The bridge group is resolved via `[host].workspace_bridge_group` (default `sb-ws`); the orchestrator never invokes `sudo`, so operators set up the group manually via `groupadd -g <gid-in-subgid-range> sb-ws` + `usermod -aG sb-ws $USER` (then re-login). `sandbox doctor` autodetects a recommended gid and prints copy-pasteable commands when the group is missing.

When touching filesystem permissions, identify which (lifecycle, mechanism) pair applies before changing anything. Plans live in `cli.main._acl_grant_plan`, `_acl_revoke_plan`, `_helper_mkdir_chown_plan`, `_helper_cp_chown_plan`, `_workspace_shared_group_plan` — single source of truth shared with the dry-run preview.

## OpenSpec workflow

This repo uses an OpenSpec-driven change workflow under `openspec/`:

- `openspec/specs/<capability>/` — current source-of-truth specs (one dir per capability, e.g. `cli-init`, `hydration-pipeline`, `instance-registry`).
- `openspec/changes/<change-id>/` — in-flight change proposals with delta specs and tasks.
- `openspec/changes/archive/` — completed changes.

Use the `openspec-*` skills (`openspec-new-change`, `openspec-apply-change`, `openspec-verify-change`, `openspec-archive-change`, etc.) rather than editing artifacts by hand. When the user defers work during an openspec flow, log it to `openspec/deferred.md` per the global rule.

## Conventions

- 100% coverage gate on `src/core/` and `src/cli/` — new code without tests will fail `make coverage`.
- mypy is strict, ruff line-length 120, target `py314`. Selected rules: `E,F,W,I,UP,B,SIM,TCH,RUF`.
- `src/cli/__main__.py` is excluded from coverage (`tool.coverage.run.omit`).
- Tests live in `tests/unit/` mirroring the `src/core/` and `src/cli/` layout.
