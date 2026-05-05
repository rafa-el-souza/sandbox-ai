# sandbox-ai

Deterministic, zero-trust orchestrator for isolated AI agent sandboxes.

## CLI Commands

```bash
sandbox init      # Initialize a new sandbox instance for the current project
sandbox start     # Launch a previously initialized sandbox
sandbox status    # Show instance status, container health, and diagnostics
sandbox attach    # Reconnect to a running sandbox (no re-provisioning)
sandbox stop      # Gracefully stop a running sandbox
sandbox stop --clean  # Stop and destroy named volumes (data unrecoverable)
sandbox destroy   # Permanently remove a sandbox instance (interactive confirmation)
sandbox destroy --force  # Bypass confirmation
sandbox doctor    # Run host readiness diagnostics
```

## Directory Layout

```
SANDBOX_AI_HOME/                  # Git clone root
├── cli/                          # CLI entrypoint (typer)
├── core/                         # Core modules
│   ├── executor.py               # Sterile POSIX subprocess execution
│   ├── registry.py               # Instance registry (fcntl-locked JSON)
│   ├── ipam.py                   # Subnet allocator (lowest-slot scan)
│   ├── hydration.py              # Pydantic → Jinja2 template pipeline
│   ├── scaffold.py               # Instance directory bootstrapper
│   └── crypto.py                 # Proxy credential generation (bcrypt)
├── .docker/                      # Immutable tooling plane (Dockerfiles)
├── .config/                      # Immutable config templates (Jinja2)
├── sandboxes/<id>/               # Per-instance mutable state
│   ├── sandbox.toml              # Instance configuration
│   ├── .sandbox.env              # Secrets (never committed)
│   ├── docker/                   # Hydrated compose files
│   ├── config/                   # Hydrated sidecar configs
│   └── log/                      # Orchestrator and container logs
└── tests/unit/                   # TDD test suite (226 tests)
```

## Configuration

Per-host orchestrator config lives at `~/.sandbox-ai/config/sandbox-ai.toml`
(seeded by `sandbox init` via interactive prompt or pre-created for non-TTY
runs). Orchestrator state (`instances.json`, `ipam.json`, `state.lock`)
lives under `~/.sandbox-ai/state/`. Both directories are created with mode
`0700` and are per-user — they are shared across all working directories of
the same user. The `SANDBOX_AI_USER_HOME` env var redirects the path for
test isolation only.

Each sandbox instance stores its per-instance configuration at
`sandboxes/<id>/sandbox.toml`. The file is generated during `sandbox init`
via the scaffolding pipeline and re-hydrated on every subsequent
`sandbox start` to ensure infrastructure drift is eliminated.

## Development

All commands require [`uv`](https://docs.astral.sh/uv/). The project pins Python 3.14 via `.python-version`;
the host system Python may differ. Always use `uv run` or `make` targets — never invoke `python` directly.

```bash
make test        # Run unit tests
make coverage    # Coverage report (target: 100% on core/ and cli/)
make lint        # Ruff linting
make typecheck   # Mypy strict mode
```

## Architecture

- **Privilege boundary**: All Docker operations execute via `machinectl shell sandbox@.host` to inherit the unprivileged user's systemd environment.
- **Transient locking**: `state.lock` is held only during provisioning, not for the runtime duration of the sandbox.
- **Two-pattern ACL model**: Pattern A (sandbox user read access, granted on start, revoked on stop) and Pattern B (dev user persistent access via default ACLs, never revoked).
- **IPAM**: Supports up to 13,312 concurrent instances via `/24` subnet triplet allocation with slot reuse.
