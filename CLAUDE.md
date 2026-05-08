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
- `scaffold.py` — bootstraps `<sandbox_ai_home()>/instances/<inst>/` (dirs, `.sandbox.env`, `sandbox.toml`, default ACLs, sentinel) plus per-workspace trees under `<sandbox_ai_home()>/workspaces/<inst>/`. `mutate_workspaces()` rewrites the `[workspaces]` block on add/remove/rename without disturbing operator hand-edits to other sections. `INSTANCE_SUBDIRS` excludes helper-recipe-owned leaves (the cache/log leaves enumerated in `orchestrator-volumes`'s "Cache/Log Leaf Inventory" requirement); those are created by the helper recipe on first start, per the "Scaffold-vs-Helper Boundary" rule that prevents the userns-EPERM bug class.
- `crypto.py` — bcrypt htpasswd, SSH keypair, credential generation for the proxy sidecar.
- `host_config.py` — `sandbox-ai.toml` loader + `machinectl_cmd()` builder + subuid/subgid resolvers (`host_id_for_in_container`, `in_container_uid_for_host_uid`, `in_container_gid_for_host_gid`, `workspace_bridge_gid`, `autodetect_workspace_bridge_gid_recommendation`).
- `helper_container.py` — disposable-helper-container primitives (`helper_chown_files`, `helper_mkdir_chown_dirs`) used by the helper-recipe phases. Pins `IMAGE_REGISTRY["busybox_musl"]`; runs every helper invocation with the full hardening baseline (runc runtime, network none, read-only rootfs, cap-drop ALL + cap-add CHOWN/DAC_OVERRIDE, no-new-privileges, tmpfs /tmp). Helper API accepts host-absolute uid/gid; the helper internally translates to in-container values via `in_container_uid_for_host_uid` / `in_container_gid_for_host_gid` before issuing `chown` so the daemon's userns map lands the on-disk ownership on the host-absolute target. `--userns=host` is deliberately not used — translation preserves the userns isolation envelope.
- `doctor.py` — host readiness check registry used by `sandbox doctor`.

### State and locking

- Mutable orchestrator state lives under `<sandbox_ai_home()>/state/` (default `~/.sandbox-ai/state/`): `instances.json`, `ipam.json`, `state.lock`, `ipam.json.lock`, `instances.json.lock`, and lazy per-instance `<inst>.backup.lock` files. The directory is created with mode `0700` by `sandbox init`.
- **Per-user lock topology** (four lock files; the topology is normative, source of truth is the `instance-registry` capability's "Registry Lock Safety" requirement):
  - `state.lock` — provisioning sequence atomicity. Held by lifecycle commands (`start`, `stop`, `destroy`, `workspace add/remove/rename/restore`) for the duration of their provisioning sequence. Per-user (not per-CWD): all `sandbox` invocations under the same user serialize on this lock regardless of working directory.
  - `ipam.json.lock` — IPAM ledger mutation lock (`core.host_config.ipam_lock_path()`). Held only inside `IPAMLedger.allocate` / `IPAMLedger.release`; the ledger never touches `state.lock`.
  - `instances.json.lock` — instance registry mutation lock (`core.host_config.registry_lock_path()`). Held only inside `InstanceRegistry.register` / `InstanceRegistry.remove`; the registry never touches `state.lock`.
  - `<inst>.backup.lock` — per-instance backup mutex (lazy per instance). Held during a backup operation's long phase; coordinates with `state.lock` via release-during-rsync.
- **Lock acquisition ordering (load-bearing).** `state.lock` is OUTER. Resource locks (`ipam.json.lock`, `instances.json.lock`) are SIBLINGS of each other — they never nest with each other; they are inner to `state.lock` when called from a state.lock-holding caller. Per-instance backup locks are also siblings of the resource locks. **No code path may acquire `state.lock` while holding any inner lock.** Violations of this ordering produce the AB/BA self-deadlock class that surfaced as bug-tracker findings 3.C (IPAM) and 4.A (registry); both fixes followed the "dedicate a lock file per resource" template.
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
- `subuid-chown` — chown to the consumer's host subuid via the disposable helper container. Used for cache/log leaves with the parent's default ACL granting `u:dev:rwx` so dev can read agent-created files. Mechanism-owned directories are NOT pre-created by `sandbox init`; the helper recipe creates them on first start (per `orchestrator-volumes`'s "Scaffold-vs-Helper Boundary" — a scaffold-pre-created leaf would be unmapped in the daemon's userns and would EPERM the helper's chown).
- `consumer-uid-0-chown` — chown to `<consumer-uid>:<consumer-gid>` mode `0640` (ro config) or `0600` (secrets) via the helper container. Mode mapping in `cli.main.RO_FILE_RECIPES`. Group ownership matches the consumer's host subgid; in-container root reads via `cap_dac_override` (in the helper's cap-add baseline), not via group permissions. The literal-0 gid pattern was removed because it was incompatible with the host-absolute helper API and provided no protection that `cap_dac_override` doesn't already grant. Mechanism-owned files are NOT pre-created by `sandbox init`; the helper recipe creates them via cp-then-chown on first start (same Scaffold-vs-Helper Boundary as `subuid-chown`).
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

### Testing

The following test conventions are enforced by meta-tests at `tests/unit/test_layout.py` and `tests/unit/test_conventions.py`. A new contribution that violates them fails `make coverage`.

- **Layout mirrors source.** Every `src/<pkg>/<module>.py` requires a test at `tests/unit/<pkg>/test_<module>.py`. Exceptions go in `_LAYOUT_ALLOWLIST` with a one-line reason (visible in diffs). The reverse direction is also checked: orphan test files (no matching src module) fail unless allowlisted (e.g., tests for `scripts/` developer tools).
- **Fixtures over plain helpers.** Cross-file shared setup uses pytest fixtures defined in the relevant `conftest.py`. Importing helper *functions* from a `conftest.py` at runtime is forbidden (`from tests.X.conftest import _helper`). Type-only imports under `if TYPE_CHECKING:` are fine — those are protocols/aliases, not behavior. Discover available fixtures via `uv run pytest --fixtures <test-file>`.
- **Markers must be registered.** Every custom `@pytest.mark.X` is declared in `pyproject.toml`'s `[tool.pytest.ini_options].markers` list with a one-line description. Pytest builtins (`parametrize`, `usefixtures`, `skip`, `skipif`, `xfail`, `filterwarnings`) are exempt. Existing custom markers — `integration`, `no_warm_mock`, `no_host_config_mock`, `no_seed_registry` — follow the opt-out-of-autouse pattern: tests that need to bypass an autouse fixture (e.g., to exercise the real function) mark themselves accordingly.
- **No suppression directives.** `# noqa`, `# type: ignore`, `# pragma: no cover` are forbidden in `src/` and `tests/`. Restructure the code (use `cast(...)`, the `setattr(obj, name, value)` runtime path, string-form `monkeypatch.setattr("module.name", ...)`, etc.) instead of silencing the linter. Pre-existing exceptions are tracked explicitly in `tests/unit/test_conventions.py::_GRANDFATHERED_SUPPRESSIONS` with a corresponding entry in `openspec/deferred.md` documenting how to remove them.
- **Pre-fix verification protocol.** When an OpenSpec change ships a regression test, the test must demonstrably FAIL against the pre-fix tree. The standard recipe (logged inline in the change's `tasks.md`): `git stash -u && git checkout main -- <changed-files> && make test-file FILE=<new-test>` → record observed failure → `git stash pop && git checkout HEAD -- <changed-files>`. The recorded failure is a load-bearing artifact: it proves the test catches the bug class, not just the absence of the symptom.
