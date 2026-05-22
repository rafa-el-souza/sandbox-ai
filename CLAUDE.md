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

Everything Docker-related crosses from the dev user into an unprivileged `sandbox` systemd user via `machinectl shell <user>@.host`. The prefix is built by `core.host_config.machinectl_cmd(user, auth)` and is either `["sudo", "machinectl", ...]` (SUDO mode) or `["machinectl", ...]` (POLKIT mode). **Never hardcode `sudo machinectl` or `systemd-run`** — always go through `machinectl_cmd()` / `pipe_cmd()` so the auth mode from `sandbox-ai.toml` is respected and the two primitives stay swappable. Recent commits (`f7ac8da`, `1648323`) refactored the codebase to enforce this; preserve it.

**`core.dispatch` is the canonical orchestrator→sandbox crossing path.** Every Docker/compose/helper crossing routes through `core.dispatch.invoke(op, args, host_config, *, timeout=None)` (or `core.dispatch.probe(...)` for probe-style callers — see below), which validates a typed op + args, builds the target argv, and crosses via `machinectl_cmd(...)` to the root-owned dispatcher binary `/usr/local/libexec/sandbox-ai/dispatch`. `machinectl_cmd(...)` itself is consumed by **exactly three allowlisted categories** (the `host-config` capability's documented allowlist — broadening it is a spec change, not a silent edit):

1. `core.host_config` — defines `machinectl_cmd()`.
2. `core.dispatch` — the sanctioned orchestration path (this is where every other caller routes through).
3. The bounded `src/core/setup/*.py` package — populated by sister change `sandbox-setup` (C-002). Setup phases cross the boundary as root *before the dispatcher exists*, so they cannot route through `core.dispatch`; the glob matches nothing until that change lands. `sandbox-setup` does not amend the allowlist — its modules just match the pre-existing `src/core/setup/*.py` category.

This is enforced by the convention meta-test `tests/unit/test_conventions.py::test_machinectl_cmd_callers_restricted`, which `ast.parse`-walks `src/**/*.py` for any import or call of `machinectl_cmd` and fails the gate if the caller is outside those three categories. Adding a new orchestrator→sandbox crossing means adding an op to `core.dispatch` (see "Adding a new orchestrator-to-sandbox operation" below), **not** hand-rolling `machinectl_cmd`.

**Binary-location split (design D6 — load-bearing trust boundary).** sandbox-ai's binaries split across two territories by trust requirement, and contributors MUST NOT move binaries between them without revisiting design D6:

- **Operator territory** — the `sandbox` CLI. Installed by `pip` / `uv` running as the operator, operator-owned, lands wherever Python packaging puts it (`<venv>/bin/sandbox`, `~/.local/bin/sandbox`, `/usr/local/bin/sandbox`), **on the operator's PATH**, intended for direct invocation.
- **Setup territory** — `/usr/local/libexec/sandbox-ai/dispatch` (this dispatcher) and `/usr/local/libexec/sandbox-ai/runsc` (sister change). Installed by `sandbox setup` running **as root**, root-owned mode `0755`, **not on PATH** (FHS § 4.7 `libexec/`: invoked by other binaries, never typed by users), `chattr +i` after install (the cheap compensating control for the F-003-unavailable sudoers `Digest_Spec`).

The split is not cosmetic: `pip` has no root, so a wheel-shipped dispatcher would land operator-writable and defeat the immutable-bit tamper model; a Python-interpreted entry point would expand the trust root to `/usr/bin/python3` + every imported stdlib module instead of one static-binary sha. "Root-owned" refers to *file ownership on disk*, not runtime privilege — the dispatcher still *executes* as the unprivileged `[host].docker_unprivileged_user` (the `machinectl shell` crossing drops privilege before bash execs it). Moving the dispatcher next to the `sandbox` CLI breaks all three of those guarantees; revisit D6 before any such change.

Two boundary primitives, picked by call-site shape:

- `machinectl_cmd(user, auth)` — PTY-allocating crossing. Used wherever the consumer expects a real TTY (today's interactive handoffs, helper-container `exec` paths whose stdin/stdout are TTYs).
- `pipe_cmd(user)` — byte-pipe crossing. Returns `["systemd-run", "-q", "--pipe", f"--uid={user}"]`. Used for programmatic byte transports — most notably the SSH `ProxyCommand` path in `cli-attach`. Auth-mode independent: `systemd-run`'s `manage-units` polkit action is the only authorization layer; the per-host `machinectl_authentication` setting does not apply.

PAM-skip trade-off (`pipe_cmd` only): `systemd-run` does NOT invoke PAM, so policies on `pam_limits.conf` and similar do not apply to processes started via `pipe_cmd`. Acceptable for our use case — programmatic byte-pipe transport over a session-bounded lifetime — where the call site is a fixed audited orchestrator path, not a user-typed command. `machinectl_cmd` retains the full PAM stack and remains the right choice for any path that should respect those policies.

Setuid exception — `sudo_as_operator(operator)` (returns `["sudo", "-u", operator]`): when setup must run a **setuid** binary (notably `sudo machinectl …` in L3a's per-op probe) *as the operator*, it MUST drop via this normal-process `sudo -u` helper, NOT `pipe_cmd`. Execing a setuid-root binary from inside a `systemd-run --uid` transient unit (what `pipe_cmd` builds) fails with systemd `EXIT_EXEC` (203) on a real host — the boundary connects then returns empty output. `pipe_cmd` stays correct for plain-binary operator crossings (L4, L8) and is required for the SSH binary-frame path; `sudo_as_operator` is the setuid-only sibling (finding F-016).

PTY consequence (`machinectl_cmd` only): the allocated PTY's `onlcr` line discipline rewrites every `\n` byte in either direction to `\r\n`. Captured stdout from `machinectl shell` therefore has CRLF line endings, even when the underlying command emits LF. Code that captures output (e.g., `docker inspect ... | head -1`) MUST strip the `\r` (`tr -d '\r'` or read in text mode) before using the value as a filename, IP, hostname, or argv element — passing a `<value>\r` to a downstream command silently fails. This is also the reason `pipe_cmd` exists: paths that carry binary frames (SSH, gRPC, raw TCP) MUST NOT cross the boundary via `machinectl_cmd` because `onlcr` would corrupt every `0x0a` byte in the stream.

### Dispatcher op reference

`core.dispatch` accepts **exactly ten** ops as the dispatcher's `argv[1]`. The surface is byte-faithful to the `machinectl_cmd(...)` callsites it replaced — it is an enumeration of existing behavior, not a new API. Each row gives the typed args callers pass to `invoke()`/`probe()` and the resulting target argv the dispatcher constructs (all are `["/bin/bash", "-c", "<inner>"]` unless noted). This is the operator-friendly "what does the dispatcher do for op X" reference; the authoritative source is the runtime-dispatcher spec's "Typed Op Surface" + "Target Argv Construction Per Op".

| # | Op | Typed args | Target-argv inner string |
|---|---|---|---|
| 1 | `auth-probe` | (none) | `echo ok` |
| 2 | `compose-up` | `[<inst>]` | `TERM=dumb NO_COLOR=1 BUILDKIT_PROGRESS=plain COMPOSE_PROJECT_NAME=<proj> docker compose <compose-files> --ansi never --env-file <env> up -d --build --wait` |
| 3 | `compose-down` | `[<inst>]` (+ `["--volumes"]` for destroy) | `TERM=dumb NO_COLOR=1 BUILDKIT_PROGRESS=plain COMPOSE_PROJECT_NAME=<proj> docker compose <compose-files> --ansi never --env-file <env> down<vol>` where `<vol>` is ` -v` iff `--volumes` was supplied (`sandbox stop` → `down`; `sandbox destroy` → `down -v`) |
| 4 | `compose-ps` | `[<inst>]` | `TERM=dumb NO_COLOR=1 BUILDKIT_PROGRESS=plain COMPOSE_PROJECT_NAME=<proj> docker compose <compose-files> --env-file <env> --ansi never ps --format json` |
| 5 | `compose-ls` | (none) | `docker compose ls --format json --all` |
| 6 | `docker-version` | (none) | `docker version --format '{{.Server.Version}}'` (the `docker version` subcommand — NOT a `docker-info` preset) |
| 7 | `docker-info` | `[<preset>]`, preset ∈ {`security-options`, `runtimes`} | `docker info --format '{{.SecurityOptions}}'` for `security-options`; `docker info --format '{{json .Runtimes}}'` for `runtimes`. There is no `default` preset. |
| 8 | `docker-manifest-inspect` | `[<ref>]` | `docker manifest inspect <ref>` |
| 9 | `helper-chown-files` | `[<parent-path>, <mode-octal>, <uid>, <gid>, <file-name>...]` | the hardened `docker run` invocation from `core.helper_container._hardened_docker_run` (busybox-musl pin, `--cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE`, `--security-opt no-new-privileges:true`, `--tmpfs /tmp`, `--user 0:0`, `-v <parent>:/p`) with the existing `cp→unlink→cp→chmod→chown` inode-stability inner loop |
| 10 | `helper-mkdir-chown-dirs` | `[<parent-path>, <uid>, <gid>, <leaf-name>...]` | same hardened prefix with the existing `mkdir -p && chown` inner loop (no chmod, per the primitive's contract) |

Three contract notes load-bearing for anyone touching the dispatcher:

- **Q6 — compose wire expansion.** Callers pass only `[<inst>]` (plus `["--volumes"]` for a `compose-down` destroy). `invoke()` internally resolves dev-context state via `core.dispatch._resolve_compose_state(inst)` (project name, compose-file list, `.sandbox.env` path — the dispatcher runs in the sandbox-user session and *cannot* re-derive these) and expands the crossed command to the wire form `dispatch <compose-op> <inst> --project <P> --env-file <E> --compose-file <f1> [--compose-file <f2>…] [--volumes]`. The Go binary parses those named flags, applies a **structural + symlink confinement** check (every compose-file/env-file operand must be absolute, `..`-free, live provably under some `…/instances/<inst>/` tree, and have no symlinked component from the `instances/<inst>` boundary downward; `--project` must match `^[a-z0-9][a-z0-9_-]*$` and end with `-<inst>`), and assembles the env prefix around an **op-hardcoded verb** (`up -d --build --wait` / `down` / `down -v` / `ps --format json`) that is NEVER read from the wire.
- **Q7 — `docker-manifest-inspect` membership validation.** The single arg must be a member of the precomputed set `{pin.pinned for pin in IMAGE_REGISTRY.values()} ∪ {pin.tagged for pin in IMAGE_REGISTRY.values()}` — i.e. *either* a registry `.pinned` digest ref *or* a registry `.tagged` tag ref. Validation is by set membership (computed once at module load from `IMAGE_REGISTRY`), NOT by a docker-reference regex. A bare `sha256:…`, an arbitrary non-registry `name@sha256:…`, or any non-registry ref is rejected. (`doctor/checks/supply_chain.py` queries both `.pinned` (stale-digest) and `.tagged` (tag-drift) per registry entry; both route through this one op.)
- **Q8 — `invoke()` raises, `probe()` branches.** `core.dispatch.invoke()` keeps a raise-on-failure contract: it raises `SandboxExecutionError` on non-zero exit or timeout (used by mutating / abort-on-failure callers — helper, compose-up/down). Probe-style callers that must branch on success/failure/timeout (every doctor check, the cli `auth-probe` preflight) call `core.dispatch.probe()` instead, which returns a typed frozen `ProbeOutcome` (`ok: bool`, `timed_out: bool`, `stdout: str`). `probe()` is the single place the `SandboxExecutionError` → `isinstance(__cause__, subprocess.TimeoutExpired)` timeout discrimination lives; `invoke()` and `Executor` are unchanged.
  - **Exit recovery is dispatcher-emitted, not orchestrator-injected (F-018 — load-bearing, do NOT revert to a wrap).** `machinectl shell` masks the inner `/bin/bash -c` exit, so the real exit must be recovered out-of-band. The recovery framing is emitted **by the dispatcher** (`__SANDBOX_BEGIN_<nonce>` before the op, `__SANDBOX_EXIT_<nonce>_$?` after), *after* sudo/polkit has authorized the crossing — so `invoke()` crosses the **bare** `dispatch <op>` payload (`Executor.run(framed=True)`), which is exactly what the per-op `Cmnd_Spec` matches. The earlier `Executor.run(sentinel=True)` *injected* `{ <cmd>; }; echo __SANDBOX_EXIT_…` into the crossed payload, making the authorized command unmatchable by any `Cmnd_Spec` — silently breaking every op for a SUDO-mode password-operator (only NOPASSWD-blanket / POLKIT masked it). `sentinel=True` (orchestrator-injected wrap, token-validated) survives **only** on the root setup-phase crossings (L5/L6/L7) that run as root with no rule to match; the operator-rule crossings (`core.dispatch.invoke`, L3a, L8) MUST use `framed=True`. The recovered exit is bound to the begin nonce so untrusted op output (a malicious image, `docker-manifest-inspect` registry JSON) cannot forge it.

### Adding a new orchestrator-to-sandbox operation

There is no separate developer doc — this is the canonical pipeline. Every new Docker/compose/helper crossing is a new dispatcher op (do NOT hand-roll `machinectl_cmd`; the convention meta-test will fail the gate):

1. **Add the op to the `Op` enum** in `src/core/dispatch.py` and its `OpSpec` (name, min/max args).
2. **Add the typed-arg validator** — reject malformed args with a typed error before the boundary is ever crossed (the dispatcher binary trusts upstream validation and does not re-run validators).
3. **Add the target-argv builder** — pure function from validated args to the `["/bin/bash", "-c", "<inner>"]` (or hardened-`docker run`) argv. Reuse existing primitives (e.g. `core.helper_container._hardened_docker_run`) rather than re-deriving flag lists.
4. **Add the Go-side translation** in `src/templates/dispatch/main.go` and a row to the shared fixture `src/templates/dispatch/fixtures/target_argv_cases.json` (`{op, args, expected_target_argv}`). Both the Python unit tests and the Go `main_test.go` consume this one fixture, so a Python↔Go drift is a fixture mismatch.
5. **Add Python unit tests** in `tests/unit/core/test_dispatch.py`: ≥1 validator-positive, ≥3 validator-negative, and target-argv-builder assertions against the fixture rows. 100% coverage on the new validator/builder paths (no suppressions).
6. **Go fixture-parity is compile-time-enforced, not gate-enforced.** The `make test`/`make coverage` gate covers only the Python side — it does NOT run the Go tests, and a host Go toolchain is not *required*. The authoritative parity run is `go test ./...` inside the pinned `golang:1.23-alpine` image as the first step of `core.dispatch.compile_dispatcher` — a fixture mismatch fails `go test`, which fails the compile, which fails `sandbox-setup`'s install phase, so a drifted dispatcher binary is never produced. (A dev host that *does* have Go installed can run the suite directly for fast iteration: `go -C src/templates/dispatch test ./...`; mind that the host toolchain version may differ from the pinned `1.23-alpine`, so the in-container compile remains the source of truth.)
7. **Integration smoke** — exercise the op end-to-end via `make test-integration` on a real-docker host (the default gate does not collect `tests/integration/`).

If the op carries instance state that the dispatcher cannot re-derive (like the compose ops' project name / compose-file / env-file), resolve it operator-side in `core.dispatch` and cross it as named wire flags with an op-hardcoded verb — see the Q6 note above; do not let the wire dictate the verb.

### Two configuration scopes

- **Per-host** (`<sandbox_ai_home()>/config/sandbox-ai.toml`, default `~/.sandbox-ai/config/sandbox-ai.toml`): parsed by `core.host_config.HostConfig`. Holds `[host].docker_unprivileged_user`, `[host].machinectl_authentication` (`sudo` | `polkit`), and `[host].workspace_bridge_group` (default `sb-ws`, the group used by the workspace shared-group recipe). Seeded by `sandbox init` (TTY prompt or non-TTY fail). `SANDBOX_AI_HOME` env var redirects this path for test isolation only.
- **Per-instance** (`<sandbox_ai_home()>/instances/<inst>/sandbox.toml`): generated during `sandbox init` and **re-hydrated on every `sandbox start`** via the Pydantic→Jinja2 pipeline in `core.hydration`. Drift is eliminated by regenerating compose/sidecar configs from the model on each start. The `[workspaces]` map-of-tables holds one or more workspaces per instance; each workspace tree lives under `<sandbox_ai_home()>/workspaces/<inst>/<ws>/`. Backup snapshots accumulate at `<sandbox_ai_home()>/workspaces/_backups/<inst>/<ws>/<UTC-timestamp>/`.

### Core modules (`src/core/`)

- `executor.py` — sterile POSIX subprocess execution (the only sanctioned way to shell out).
- `registry.py` — instance registry as fcntl-locked JSON at `<sandbox_ai_home()>/state/instances.json`.
- `ipam.py` — `/24` subnet quintuple allocator (isolated, core_proxy, dns, egress, ipc) over 10.100.0.0–10.255.255.0 with lowest-slot scan and slot reuse (`MAX_SLOTS = 7987`).
- `hydration.py` — `InstanceConfig` Pydantic model → `build_jinja_context` → `render_templates` → `validate_templates`. Templates live in `src/templates/config/` and `src/templates/docker/` (the immutable tooling/config plane), shipped with the wheel as the top-level `templates` Python package and discovered via `jinja2.PackageLoader("templates", package_path="")` / `importlib.resources`.
- `scaffold.py` — bootstraps `<sandbox_ai_home()>/instances/<inst>/` (dirs, `.sandbox.env`, `sandbox.toml`, default ACLs, sentinel) plus per-workspace trees under `<sandbox_ai_home()>/workspaces/<inst>/`. `mutate_workspaces()` rewrites the `[workspaces]` block on add/remove/rename without disturbing operator hand-edits to other sections. `INSTANCE_SUBDIRS` excludes helper-recipe-owned leaves (the cache/log leaves enumerated in `orchestrator-volumes`'s "Cache/Log Leaf Inventory" requirement); those are created by the helper recipe on first start, per the "Scaffold-vs-Helper Boundary" rule that prevents the userns-EPERM bug class.
- `crypto.py` — bcrypt htpasswd, SSH keypair, credential generation for the proxy sidecar.
- `host_config.py` — `sandbox-ai.toml` loader + `machinectl_cmd()` builder + subuid/subgid resolvers (`host_id_for_in_container`, `in_container_uid_for_host_uid`, `in_container_gid_for_host_gid`, `workspace_bridge_gid`, `autodetect_workspace_bridge_gid_recommendation`).
- `dispatch.py` — the canonical orchestrator→sandbox crossing (see "Privilege boundary"). `Op` enum + per-op validators + per-op target-argv builders; `invoke(op, args, host_config, *, timeout=None)` (raise-on-failure) and `probe(op, args, host_config, *, timeout=None) -> ProbeOutcome` (branch-on-outcome); `_resolve_compose_state(inst)` (the single operator-side compose-state resolver); `compile_dispatcher(...)` (the offline reproducible docker-based Go build). The op surface (10 ops) and their target-argv shapes are documented in "Dispatcher op reference" below.
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

## Setup

`sudo sandbox setup` is the idempotent host-bootstrap command: it takes a fresh Linux host from "distro + uv-installed wheel" to "every `sandbox doctor` check green," and is safe to re-run any time the host drifts. It runs as root throughout (`os.geteuid() == 0`; refuses non-root with `sandbox setup must be run as root. Re-invoke as: sudo sandbox setup`). The operator is resolved by explicit precedence: `--operator <name>` → `$SUDO_USER`+`$SUDO_UID` consistency → `$PKEXEC_UID` → refuse (no TTY heuristics). Operator-side state crosses via `pipe_cmd(<operator>)`; sandbox-user state crosses via `machinectl_cmd(...)` — setup's `src/core/setup/*.py` modules are the third allowlisted `machinectl_cmd` caller category (see "Privilege boundary").

**Plan/apply two-pass UX.** Plan pass probes every phase (no mutations) with doctor-style markers (`✓ already correct`, `⊙ missing → will mutate`, `⚠ blocked`, `✗ verify-only failure → refuse`) and a `Summary: <A> already correct, <M> will mutate, <B> blocked, <R> refused` line. `--dry-run` runs only the plan pass. Apply pass is gated: zero mutations → no prompt, `Nothing to apply.`, exit 0; ≥1 refusal → no prompt, refusal list, exit non-zero (apply never runs); ≥1 mutation in a TTY without `--yes` → `Proceed with apply? [y/N]: ` (only `y`/`Y`/`yes`/`YES` proceeds); non-TTY without `--yes` → refuse; SIGINT anywhere → `aborted by operator (SIGINT). No mutations applied.`, exit 130. Apply continues past non-rollback failures (dependents marked BLOCKED-BY); L3a failure rolls back the sudoers/polkit drop-in.

**Phased ceremony (named, not counted — never re-introduce a brittle "N-phase" integer; R1).** Phases run in the named order: **L0** identity/env (root + operator resolution; distro tier; required-binary check; `MACHINECTL_PATH` uniqueness assertion on the sudoers `secure_path` basis — inode-deduped so usrmerge symlink-aliases are one, genuinely-distinct binaries still refused per F-005) → **L1** sysctl drop-in + ACL-FS/cgroup-v2 verify (L1 resolves no OS user) → **L2** systemd-machined + sandbox useradd + `/etc/subuid`/`/etc/subgid` append-only + `sb-ws` groupadd + operator `usermod` (L2 does **not** install runsc) → **L2a** `Delegate=yes` drop-in (split out of L1: its `user-<sandbox-uid>.service.d/` path is uid-scoped to the sandbox user L2 creates, so `depends_on=("l2",)`, ordered before L5; F-014) → **L4** operator state (`<sandbox_ai_home()>/{config,state,instances,workspaces}` + `sandbox-ai.toml` seed via `pipe_cmd`) → **L5** linger + rootless dockerd → **L6** `daemon.json` reserved-key merge + restart-cliff → **L6a** runsc install (own phase, R1) → **L6.5** dispatcher compile+install → **L7** helper-image pre-pull → **L3** sudoers/polkit drop-in install + **L3a** per-op probe → **L8** fresh-session re-probe. **L3 is the last base-ceremony mutation phase and the only one that touches the privilege-boundary rule** — there is no permissive bootstrap rule before it, so a crash anywhere in L0..L7 leaves zero sudoers/polkit grant on disk, and an L3 crash is handled by L3a's rollback. Setup-as-root invokes `machinectl` directly (V8); no permissive bootstrap rule is ever installed. L8 is verification, not mutation. The optional fapolicyd/AIDE integration phases run **after L8** with sticky opt-in (see "Production integrity posture") and mutate only their own `/etc/fapolicyd/trust.d/` and `/etc/aide/aide.conf.d/` namespaces — they never touch the L3 rule, so the no-permissive-window property is scoped to the base ceremony's privilege-boundary rule.

**Content-aware probes.** Every phase whose mutation can drift across wheel upgrades renders expected state from current sources (the `core.dispatch.Op` enum, `BINARY_REGISTRY`/`IMAGE_REGISTRY` pins, the dispatcher source bundle derived from `core.dispatch._DISPATCH_SOURCE_ENTRIES`, the toml/daemon.json contents) and compares it to observed on-disk state; the act is skipped only on an exact match. This is why an idempotent re-run on a converged host completes in <5s with every phase `already correct`.

**Reserved-namespace principle.** Setup writes ONLY to namespaces it owns and never edits, appends to, or overwrites any file or key outside the enumerable list (see "Manual uninstall recipe" for the full list). Each drop-in carries a leading `# sandbox-ai managed — do not edit; rerun 'sudo sandbox setup'` comment (`//` for polkit). Hand-edits *outside* the owned namespace are never touched; hand-edits *inside* it are authoritatively overwritten by setup. `/etc/subuid`/`/etc/subgid` are append-only (flat-file shared territory; refuse to shrink an inadequate existing range).

**Binary-location split + F-003 constraint + compensating controls.** The dispatcher (`/usr/local/libexec/sandbox-ai/dispatch`, L6.5) and runsc (`/usr/local/libexec/sandbox-ai/runsc`, L6a) install root-owned mode `0755`, not on PATH (FHS § 4.7 `libexec/`), with `chattr +i` applied after install (cleared with `chattr -i` before any replace, re-applied after — see `--update-runsc`). This is the cheap compensating control for the **F-003** finding: the sudoers `Digest_Spec` (`sha512:<hash>`) crypto pin is **silently no-op on Debian-family hosts**, so the rendered rule deliberately contains no `Digest_Spec`. `chattr +i` does not provide crypto tamper resistance (root can clear the bit) but raises the bar for casual/automated tampering and adds an audit signal. The remaining integrity story is doctor-visibility, not enforcement: `dispatcher_sha_drift` (on-disk binary sha vs. the `<sandbox_ai_home()>/state/dispatcher.manifest.json` `compiled_sha512` + source-bundle sha — WARN on tamper or wheel-upgrade drift), `runsc_pinned_match` (on-disk runsc sha vs. `BINARY_REGISTRY["runsc"].sha512` — WARN on drift), `binary_integrity_posture` (informational dm-verity / IMA / fapolicyd / AIDE state), and `setup_invariants` (owned-path/mode/ownership audit + machinectl-path stability + sudoers-rule content audit + sudo-version floor). All four are WARN-not-FAIL by policy.

**runsc drift/update (shape #3).** L6a installs the pinned runsc if absent; on a re-run with a sha mismatch it does **not** auto-overwrite — it records `runsc version drift: installed sha <X>, pinned sha <Y>. To update: sudo sandbox setup --update-runsc` in the finalization summary. `sudo sandbox setup --update-runsc` re-runs **only** the L6a phase with `force=True` (`chattr -i` → atomic install of the pinned binary → `chattr +i`), bypassing the drift-skip. Setup never silently auto-updates runsc.

**Multi-operator by accumulation.** Each operator runs `sudo sandbox setup` for themselves. Shared host state (sandbox user, `sb-ws` group, rootless dockerd, runsc, dispatcher binary, `/etc/subuid` entries) is **convergent** across operators — idempotent, written once, the same for everyone. Per-operator state (the `/etc/sudoers.d/sandbox-ai-machinectl-<operator>` drop-in, `<sandbox_ai_home()>/`, `sb-ws` group membership) **accumulates additively**: `alice` running setup installs `…-alice`; `bob` running setup later installs `…-bob` without disturbing alice's drop-in or the shared state. Concurrent invocations under the same operator serialize on that operator's per-user `state.lock`; invocations under different operators do not inter-serialize.

The SUDO sudoers rule enumerates each dispatcher op as a `Cmnd_Spec` **inlined directly into the operator's user-spec** (`<operator> <hostname>=(root) NOPASSWD: NOSETENV: <spec>, \ …`) — NOT behind a shared `Cmnd_Alias`. Each spec is the full `MACHINECTL_PATH shell <user>@.host /bin/bash -c <dispatch>\ <op>[\ *]` prefix (backslash-escaped whitespace, zero `"` chars per F-004, no `Digest_Spec` per F-003, rendered from `core.dispatch.Op`). The inline form is load-bearing for multi-operator-by-accumulation: `Cmnd_Alias` names share a single global `/etc/sudoers.d/` namespace, so a per-operator `Cmnd_Alias SANDBOX_OPS` collided with every other operator's drop-in (`duplicate Cmnd_Alias` — F-020); inlining keeps each operator's user-spec independent. POLKIT mode is coarser: `org.freedesktop.machine1.shell` cannot inspect argv, so the polkit rule is an **action-level** grant and per-op narrowing lives only at the application layer (the orchestrator only calls `core.dispatch`, convention-meta-test-enforced) — a deliberate, documented asymmetry, not a defect.

### Production integrity posture

The dispatcher/runsc trust model layers from cheapest to strongest. Setup applies the cheap tier automatically; the stronger tiers are operator-configured and detected (not bootstrapped) by `sandbox doctor`'s `binary_integrity_posture` check:

- **`chattr +i`** (automatic, L6a/L6.5): defense-in-depth against casual tampering + an audit signal. Not crypto tamper resistance.
- **fapolicyd** (opt-in): `sudo sandbox setup --enable-fapolicyd-integration` writes `/etc/fapolicyd/trust.d/sandbox-ai.trust` (one `<path> <size> <sha256>` line per managed binary, `# sandbox-ai managed` header, mode `0644` root:root) and runs `fapolicyd-cli --update`. Refuses with a distro-conditional install hint if `fapolicyd` or `/etc/fapolicyd/trust.d/` is absent; warns (does not fail) if fapolicyd is installed but not running.
- **AIDE** (opt-in): `sudo sandbox setup --enable-aide-integration` writes `/etc/aide/aide.conf.d/sandbox-ai.conf` (the two-line `… dispatch NORMAL` / `… runsc NORMAL` snippet + `# sandbox-ai managed` header, mode `0644` root:root). Setup never runs `aide --init` (a 10+ minute filesystem walk); on first install with `/var/lib/aide/aide.db` absent it appends an `aide --init` prompt to the finalization summary.
- **dm-verity / IMA-appraise** (operator-configured, out of setup's reach): require kernel cmdline + boot configuration. Setup does not bootstrap these; `binary_integrity_posture` probes `/proc/cmdline` + `dmsetup status` (dm-verity) and `/sys/kernel/security/ima/policy` (IMA) and reports structured state.

**Sticky opt-in** (design D11): an integration phase auto-includes itself on any subsequent `sudo sandbox setup` when **either** its flag is passed **or** its owned drop-in already exists on disk. So `--update-runsc` (or any dispatcher re-compile) on a host with sticky fapolicyd/AIDE integration cascades a trust-file refresh with the new sha — no window of inconsistency. An integration-phase failure does not roll back the L0..L8 base ceremony.

`binary_integrity_posture` always PASSes (informational); it reports state so operators in production-sensitive contexts know which tiers to enable. It does not enforce or bootstrap.

### Manual uninstall recipe

There is no `sandbox setup --uninstall` yet (a future change will automate this). To remove sandbox-ai from a host, an operator walks the enumerable owned-namespace list **per the spec's "Reserved Namespace File Ownership" requirement** — setup writes ONLY these paths/keys, so removing exactly them fully uninstalls:

1. `rm /etc/sudoers.d/sandbox-ai-machinectl-<operator>` — sudoers drop-in (SUDO mode), one per operator.
2. `rm /etc/polkit-1/rules.d/49-sandbox-ai-machinectl.rules` — polkit drop-in (POLKIT mode).
3. `rm /etc/sysctl.d/49-sandbox-ai.conf` — kernel sysctl drop-in.
4. `rm /etc/systemd/system/user-<sandbox-uid>.service.d/sandbox-ai-delegate.conf` — systemd `Delegate=yes` drop-in (then `systemctl daemon-reload`).
5. `chattr -i /usr/local/libexec/sandbox-ai/dispatch /usr/local/libexec/sandbox-ai/runsc` then `rm -rf /usr/local/libexec/sandbox-ai/` — the immutable binary directory (dispatcher + runsc).
6. Remove the `runtimes["sandbox-ai-runsc"]` key from `~<sandbox-user>/.config/docker/daemon.json` — reserved key only; leave the operator's other `runtimes["..."]` keys and the rest of the file intact.
7. `rm -rf <sandbox_ai_home()>/` — per-operator state (`{config,state,instances,workspaces}` + `state/dispatcher.manifest.json`), one tree per operator.
8. Remove the append-only `<sandbox-user>` entries from `/etc/subuid` + `/etc/subgid` if no longer needed (these are shared flat-file territory — only the sandbox-user lines, hand-edit-removed).
9. `rm /etc/fapolicyd/trust.d/sandbox-ai.trust` then `fapolicyd-cli --update` — **only if** fapolicyd integration was enabled (optional owned path).
10. `rm /etc/aide/aide.conf.d/sandbox-ai.conf` — **only if** AIDE integration was enabled (optional owned path); the operator's next `aide --check`/`aide --update` reflects the removal.
11. Optionally `userdel <sandbox-user>` and `groupdel sb-ws` (plus `usermod -G '' <operator>` to drop the `sb-ws` membership) if the sandbox user / bridge group are no longer wanted — these are shared host state, not removed automatically since other operators may still depend on them.

The list above is faithful to and complete against the spec's "Reserved Namespace File Ownership" enumerable list (including the optional fapolicyd/AIDE drop-ins and the dispatcher manifest); setup writes nothing outside it, so this recipe is exhaustive. Operator docs for the full walkthrough live in `docs/setup-guide.md`.

## OpenSpec workflow

This repo uses an OpenSpec-driven change workflow under `openspec/`:

- `openspec/specs/<capability>/` — current source-of-truth specs (one dir per capability, e.g. `cli-init`, `hydration-pipeline`, `instance-registry`).
- `openspec/changes/<change-id>/` — in-flight change proposals with delta specs and tasks.
- `openspec/changes/archive/` — completed changes.

Use the `openspec-*` skills (`openspec-new-change`, `openspec-apply-change`, `openspec-verify-change`, `openspec-archive-change`, etc.) rather than editing artifacts by hand — or the `internal-tooling` skill, which wraps them with the full change-development lifecycle (explore → scope → draft → implement → archive → integrate) and owns `openspec/explorations/` (the exploration backlog, ongoing explorations, validation records, findings). When the user defers work during an OpenSpec / `internal-tooling` flow, log it via `flow-backlog add deferred …` (the `internal-tooling` skill — it lands in `openspec/explorations/backlog/deferred.md`) per the global rule.

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
- **No suppression directives.** `# noqa`, `# type: ignore`, `# pragma: no cover` are forbidden in `src/` and `tests/`. Restructure the code (use `cast(...)`, the `setattr(obj, name, value)` runtime path, string-form `monkeypatch.setattr("module.name", ...)`, etc.) instead of silencing the linter. Pre-existing exceptions are tracked explicitly in `tests/unit/test_conventions.py::_GRANDFATHERED_SUPPRESSIONS` with a corresponding entry in `openspec/explorations/backlog/deferred.md` documenting how to remove them.
- **Pre-fix verification protocol.** When an OpenSpec change ships a regression test, the test must demonstrably FAIL against the pre-fix tree. The standard recipe (logged inline in the change's `tasks.md`): `git stash -u && git checkout main -- <changed-files> && make test-file FILE=<new-test>` → record observed failure → `git stash pop && git checkout HEAD -- <changed-files>`. The recorded failure is a load-bearing artifact: it proves the test catches the bug class, not just the absence of the symptom.
