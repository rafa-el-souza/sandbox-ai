# Architecture

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
make pyright                    # pyright --strict (src/ strict; tests/ scoped)
make check                      # the full gate: lint → typecheck → pyright → coverage
```

`pytest.testpaths = ["tests/unit"]` — integration tests under `tests/integration/` are not collected by default and must be invoked explicitly.

The CLI entrypoint is `sandbox = "cli.main:app"` (typer). Run as `uv run sandbox <cmd>`.

## Overview

The orchestrator's defining property is that everything Docker-related crosses from the dev user into an unprivileged `sandbox` systemd user via `machinectl shell` (see [privilege-boundary.md](privilege-boundary.md)). The CLI itself runs as the operator; every Docker/compose/helper operation routes through the canonical `core.dispatch` crossing into a root-owned dispatcher binary that executes as the unprivileged sandbox user.

## Core modules (`src/core/`)

- `executor.py` — sterile POSIX subprocess execution (the only sanctioned way to shell out).
- `registry.py` — instance registry as fcntl-locked JSON at `<sandbox_ai_home()>/state/instances.json`.
- `ipam.py` — `/24` subnet quintuple allocator (isolated, core_proxy, dns, egress, ipc) over 10.100.0.0–10.255.255.0 with lowest-slot scan and slot reuse (`MAX_SLOTS = 7987`).
- `hydration.py` — `InstanceConfig` Pydantic model → `build_jinja_context` → `render_templates` → `validate_templates`.
  - Templates live in `src/templates/config/` and `src/templates/docker/` (the immutable tooling/config plane).
  - Shipped with the wheel as the top-level `templates` Python package and discovered via `jinja2.PackageLoader("templates", package_path="")` / `importlib.resources`.
- `scaffold.py` — bootstraps `<sandbox_ai_home()>/instances/<inst>/` (dirs, `.sandbox.env`, `sandbox.toml`, default ACLs, sentinel) plus per-workspace trees under `<sandbox_ai_home()>/workspaces/<inst>/`.
  - `mutate_workspaces()` rewrites the `[workspaces]` block on add/remove/rename without disturbing operator hand-edits to other sections.
  - `INSTANCE_SUBDIRS` excludes helper-recipe-owned leaves (the cache/log leaves enumerated in `orchestrator-volumes`'s "Cache/Log Leaf Inventory" requirement); those are created by the helper recipe on first start.
  - The "Scaffold-vs-Helper Boundary" rule prevents the userns-EPERM bug class.
- `crypto.py` — bcrypt htpasswd, SSH keypair, credential generation for the proxy sidecar.
- `host_config.py` — marker-sourced `HostConfig.from_marker(operator)` builder (host provisioning facts come from the root-owned per-operator setup-state marker, not a toml) + `machinectl_cmd()` builder + subuid/subgid resolvers (`host_id_for_in_container`, `in_container_uid_for_host_uid`, `in_container_gid_for_host_gid`, `workspace_bridge_gid`, `autodetect_workspace_bridge_gid_recommendation`).
- `dispatch.py` — the canonical orchestrator→sandbox crossing (see [privilege-boundary.md](privilege-boundary.md)).
  - `Op` enum + per-op validators + per-op target-argv builders.
  - `invoke(op, args, host_config, *, timeout=None)` — raise-on-failure (rejects the streaming `fwd` op).
  - `probe(op, args, host_config, *, timeout=None) -> ProbeOutcome` — branch-on-outcome (rejects the streaming `fwd` op).
  - `proxy_argv(...)` — the streaming-op ProxyCommand-argv constructor; the single sanctioned producer of a `dispatch fwd` payload / `/fwd` docker-exec argv.
  - `_resolve_compose_state(inst)` — the single operator-side compose-state resolver.
  - `compile_dispatcher(...)` — the offline reproducible docker-based Go build.
  - The op surface (12 ops — eleven framed + the streaming `fwd`) and their target-argv shapes are documented in [dispatcher.md](dispatcher.md).
- `helper_container.py` — disposable-helper-container primitives (`helper_chown_files`, `helper_mkdir_chown_dirs`) used by the helper-recipe phases.
  - Pins `IMAGE_REGISTRY["busybox_musl"]`.
  - **Hardening:** runs every helper invocation with the full hardening baseline (runc runtime, network none, read-only rootfs, cap-drop ALL + cap-add CHOWN/DAC_OVERRIDE, no-new-privileges, tmpfs /tmp).
  - Helper API accepts host-absolute uid/gid; the helper internally translates to in-container values via `in_container_uid_for_host_uid` / `in_container_gid_for_host_gid` before issuing `chown` so the daemon's userns map lands the on-disk ownership on the host-absolute target.
  - `--userns=host` is deliberately not used — translation preserves the userns isolation envelope.
- `doctor.py` — host readiness check registry used by `sandbox doctor`.
