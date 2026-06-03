# Architecture

## Project

`sandbox-ai` is a deterministic, zero-trust orchestrator that provisions isolated AI agent sandboxes. The CLI (`sandbox`) wraps Docker Compose lifecycles but executes every Docker call across a privilege boundary into an unprivileged systemd user via `machinectl shell`.

## Architecture

### Core modules (`src/core/`)

- `executor.py` — sterile POSIX subprocess execution (the only sanctioned way to shell out).
- `registry.py` — instance registry as fcntl-locked JSON at `<sandbox_ai_home()>/state/instances.json`.
- `ipam.py` — allocates five consecutive `/24` subnets per instance (isolated, core_proxy, dns, egress, ipc) over 10.100.0.0–10.255.255.0, with a lowest-slot scan and slot reuse (`MAX_SLOTS = 7987`).
- `hydration.py` — `InstanceConfig` Pydantic model → `build_jinja_context` → `render_templates` → `validate_templates`. Templates live in `src/templates/config/` and `src/templates/docker/` (the immutable tooling/config plane), shipped with the wheel as the top-level `templates` Python package and discovered via `jinja2.PackageLoader("templates", package_path="")` / `importlib.resources`.
- `scaffold.py` — bootstraps `<sandbox_ai_home()>/instances/<inst>/` (dirs, `.sandbox.env`, `sandbox.toml`, default ACLs, sentinel) plus per-workspace trees under `<sandbox_ai_home()>/workspaces/<inst>/`. `mutate_workspaces()` rewrites the `[workspaces]` block on add/remove/rename without disturbing operator hand-edits to other sections. `INSTANCE_SUBDIRS` excludes the helper-recipe-owned cache/log leaves: those are created by the helper recipe on first start, because pre-creating them in the scaffold would leave them unmapped in the daemon's userns and EPERM the helper's `chown`.
- `crypto.py` — bcrypt htpasswd, SSH keypair, credential generation for the proxy sidecar.
- `host_config.py` — `sandbox-ai.toml` loader + `machinectl_cmd()` builder + subuid/subgid resolvers (`host_id_for_in_container`, `in_container_uid_for_host_uid`, `in_container_gid_for_host_gid`, `workspace_bridge_gid`, `autodetect_workspace_bridge_gid_recommendation`).
- `dispatch.py` — the canonical orchestrator→sandbox crossing (see "Privilege boundary"). `Op` enum + per-op validators + per-op target-argv builders; `invoke(op, args, host_config, *, timeout=None)` (raise-on-failure) and `probe(op, args, host_config, *, timeout=None) -> ProbeOutcome` (branch-on-outcome); `_resolve_compose_state(inst)` (the single operator-side compose-state resolver); `compile_dispatcher(...)` (the offline reproducible docker-based Go build). The op surface (10 ops) and their target-argv shapes are documented in "Dispatcher op reference" below.
- `helper_container.py` — disposable-helper-container primitives (`helper_chown_files`, `helper_mkdir_chown_dirs`) used by the helper-recipe phases. Pins `IMAGE_REGISTRY["busybox_musl"]`; runs every helper invocation with the hardening baseline (runc runtime, network none, read-only rootfs, cap-drop ALL + cap-add CHOWN/DAC_OVERRIDE, no-new-privileges, tmpfs /tmp). Helper API accepts host-absolute uid/gid; the helper internally translates to in-container values via `in_container_uid_for_host_uid` / `in_container_gid_for_host_gid` before issuing `chown` so the daemon's userns map lands the on-disk ownership on the host-absolute target. `--userns=host` is deliberately not used — translation preserves the userns isolation envelope.
- `doctor.py` — host readiness check registry used by `sandbox doctor`.
