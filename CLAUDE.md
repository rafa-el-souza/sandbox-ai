# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

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
```

`pytest.testpaths = ["tests/unit"]` — integration tests under `tests/integration/` are not collected by default and must be invoked explicitly.

The CLI entrypoint is `sandbox = "cli.main:app"` (typer). Run as `uv run sandbox <cmd>`.

## Architecture

The architecture documentation is fanned out across `docs/`. Read the relevant doc before touching code in its area:

- [docs/architecture.md](docs/architecture.md) — project overview, commands, and the `src/core/` module map.
- [docs/privilege-boundary.md](docs/privilege-boundary.md) — the load-bearing orchestrator→sandbox crossing: `machinectl_cmd`/`pipe_cmd`/`sudo_pipe_cmd` primitives, the binary-location split, and the PAM-skip + PTY/`onlcr` consequences.
- [docs/dispatcher.md](docs/dispatcher.md) — the 12-op dispatcher reference (typed args + target argv), the four contract notes, and the pipeline for adding a new orchestrator-to-sandbox operation.
- [docs/configuration.md](docs/configuration.md) — the per-host and per-instance configuration scopes.
- [docs/acl-model.md](docs/acl-model.md) — the ACL/ownership model as a lifecycle × mechanism taxonomy.
- [docs/locking.md](docs/locking.md) — orchestrator state layout and the per-user lock topology + acquisition ordering.
- [docs/setup.md](docs/setup.md) — `sudo sandbox setup`: phased ceremony, reserved-namespace principle, production integrity posture, and the manual uninstall recipe. ([docs/setup-guide.md](docs/setup-guide.md) is the operator-facing walkthrough.)
- [docs/testing.md](docs/testing.md) — coverage gate, lint/type conventions, and the meta-test-enforced testing rules.
- [docs/security-model.md](docs/security-model.md) — how the orchestrator isolates an untrusted agent from the host.

## OpenSpec workflow

This repo uses an OpenSpec-driven change workflow under `openspec/`:

- `openspec/specs/<capability>/` — current source-of-truth specs (one dir per capability, e.g. `cli-init`, `hydration-pipeline`, `instance-registry`).
- `openspec/changes/<change-id>/` — in-flight change proposals with delta specs and tasks.
- `openspec/changes/archive/` — completed changes.

Use the `openspec-*` skills (`openspec-new-change`, `openspec-apply-change`, `openspec-verify-change`, `openspec-archive-change`, etc.) rather than editing artifacts by hand — or the `internal-tooling` skill, which wraps them with the full change-development lifecycle (explore → scope → draft → implement → archive → integrate) and owns `openspec/explorations/` (the exploration backlog, ongoing explorations, validation records, findings). When the user defers work during an OpenSpec / `internal-tooling` flow, log it via `flow-backlog add deferred …` (the `internal-tooling` skill — it lands in `openspec/explorations/backlog/deferred.md`) per the global rule.
