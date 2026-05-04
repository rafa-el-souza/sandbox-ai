# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`sandbox-ai` is a deterministic, zero-trust orchestrator that provisions isolated AI agent sandboxes. The CLI (`sandbox`) wraps Docker Compose lifecycles but executes every Docker call across a privilege boundary into an unprivileged systemd user via `machinectl shell`.

## Commands

The project pins Python 3.14 (`.python-version`) and depends on [`uv`](https://docs.astral.sh/uv/). Never call `python` directly — always go through `uv run` or `make`.

```bash
make test                       # unit tests (tests/unit)
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

- **Per-host** (`sandbox-ai.toml` at project root): parsed by `core.host_config.HostConfig`. Holds `[host].docker_unprivileged_user` and `[host].machinectl_authentication` (`sudo` | `polkit`).
- **Per-instance** (`sandboxes/<id>/sandbox.toml`): generated during `sandbox init` and **re-hydrated on every `sandbox start`** via the Pydantic→Jinja2 pipeline in `core.hydration`. Drift is eliminated by regenerating compose/sidecar configs from the model on each start.

### Core modules (`core/`)

- `executor.py` — sterile POSIX subprocess execution (the only sanctioned way to shell out).
- `registry.py` — instance registry as fcntl-locked JSON at `.state/instances.json`.
- `ipam.py` — `/24` subnet septuple allocator (isolated, core_proxy, dns, admin, admin_proxy, egress, ipc) over 10.100.0.0–10.255.255.0 with lowest-slot scan and slot reuse (`MAX_SLOTS = 5705`).
- `hydration.py` — `InstanceConfig` Pydantic model → `build_jinja_context` → `render_templates` → `validate_templates`. Templates live in `.config/` and `.docker/` (immutable tooling/config plane).
- `scaffold.py` — bootstraps `sandboxes/<id>/` (dirs, `.sandbox.env`, `sandbox.toml`, default ACLs, sentinel).
- `crypto.py` — bcrypt htpasswd, SSH keypair, credential generation for the proxy sidecar.
- `host_config.py` — `sandbox-ai.toml` loader + `machinectl_cmd()` builder.
- `doctor.py` — host readiness check registry used by `sandbox doctor`.

### State and locking

- `.state/instances.json`, `.state/ipam.json`, `.state/state.lock` are the only mutable orchestrator state.
- `state.lock` is **transient** — held only during provisioning, released for the runtime lifetime of a sandbox. Don't add long-lived locks.

### ACL model (two patterns)

- **Pattern A**: sandbox user gets read access granted on `start`, revoked on `stop`.
- **Pattern B**: dev user has persistent access via default ACLs, never revoked.

When touching filesystem permissions, identify which pattern applies before changing anything.

## OpenSpec workflow

This repo uses an OpenSpec-driven change workflow under `openspec/`:

- `openspec/specs/<capability>/` — current source-of-truth specs (one dir per capability, e.g. `cli-init`, `hydration-pipeline`, `instance-registry`).
- `openspec/changes/<change-id>/` — in-flight change proposals with delta specs and tasks.
- `openspec/changes/archive/` — completed changes.

Use the `openspec-*` skills (`openspec-new-change`, `openspec-apply-change`, `openspec-verify-change`, `openspec-archive-change`, etc.) rather than editing artifacts by hand. When the user defers work during an openspec flow, log it to `openspec/deferred.md` per the global rule.

## Conventions

- 100% coverage gate on `core/` and `cli/` — new code without tests will fail `make coverage`.
- mypy is strict, ruff line-length 120, target `py314`. Selected rules: `E,F,W,I,UP,B,SIM,TCH,RUF`.
- `cli/__main__.py` is excluded from coverage (`tool.coverage.run.omit`).
- Tests live in `tests/unit/` mirroring the `core/` and `cli/` layout.
