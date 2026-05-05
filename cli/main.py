"""Sandbox CLI orchestrator — full lifecycle implementation.

Commands: init, start, stop, attach, destroy, doctor, status.
All Docker operations cross the dev/sandbox privilege boundary via machinectl.
"""

from __future__ import annotations

import fcntl
import json as _json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from pathlib import Path
from core.crypto import generate_credential, generate_ssh_keypair, hash_proxy_password, write_htpasswd
from core.doctor import (
    build_check_registry,
    detect_distro,
    render_results,
    run_check_subset,
    run_checks,
)
from core.exceptions import SandboxExecutionError
from core.executor import Executor
from core.host_config import HostConfig, MachinectlAuth, machinectl_cmd, sandbox_ai_user_home, state_lock_path
from core.hydration import (
    InstanceConfig,
    build_jinja_context,
    render_templates,
    validate_templates,
)
from core.ipam import IPAMExhaustedError, IPAMLedger, derive_static_ips, derive_subnets
from core.registry import InstanceRegistry, generate_instance_id
from core.scaffold import (
    _detect_git_config,
    apply_default_acls,
    create_env_file,
    create_instance_dirs,
    ensure_per_user_tree,
    ensure_registry_seed,
    prompt_secrets,
    write_initialized_sentinel,
    write_sandbox_toml,
)
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

app = typer.Typer()
console = Console()


# ─── Data Types ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ContainerInfo:
    """Parsed container status from `docker compose ps --format json`."""

    name: str
    service: str
    state: str
    health: str | None
    status: str


# ─── Resolution helpers ─────────────────────────────────────────────────────


def _resolve_sandbox_ai_home() -> str:
    """Resolve SANDBOX_AI_HOME from the orchestrator source location."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_project_dir() -> str:
    """Resolve the project directory from CWD."""
    return os.path.abspath(os.getcwd())


def _resolve_instance(sandbox_ai_home: str, project_dir: str) -> tuple[str | None, str | None]:
    """Look up instance from registry. Returns (instance_dir, instance_id) or (None, None)."""
    registry = InstanceRegistry()
    instance_id = registry.lookup(project_dir)
    if instance_id is None:
        return None, None
    instance_dir = os.path.join(sandbox_ai_home, "sandboxes", instance_id)
    return instance_dir, instance_id


def _load_config(instance_dir: str) -> InstanceConfig:
    """Parse sandbox.toml from instance directory."""
    toml_path = os.path.join(instance_dir, "sandbox.toml")
    return InstanceConfig.from_toml(toml_path)


# ─── Warm state check ───────────────────────────────────────────────────────


def _container_status(
    instance_dir: str,
    name: str,
    host_user: str,
    config: InstanceConfig,
    auth: MachinectlAuth = MachinectlAuth.SUDO,
) -> list[ContainerInfo]:
    """Query container statuses via `docker compose ps --format json`.

    Returns a list of ContainerInfo parsed from NDJSON output.
    Returns an empty list if the instance is stopped or the executor errors.
    """
    compose_file = os.path.join(instance_dir, "docker", "compose.yml")
    if not os.path.exists(compose_file):
        return []

    compose_files = _build_compose_files(instance_dir, config)
    files_str = " ".join(compose_files)

    executor = Executor()
    try:
        result = executor.run(
            [
                *machinectl_cmd(host_user, auth),
                "/bin/bash",
                "-c",
                (
                    f"TERM=dumb NO_COLOR=1 BUILDKIT_PROGRESS=plain COMPOSE_PROJECT_NAME={name} "
                    f"docker compose {files_str} "
                    f"--env-file {os.path.join(instance_dir, '.sandbox.env')} "
                    f"--ansi never ps --format json"
                ),
            ],
            sentinel=True,
        )
    except SandboxExecutionError:
        return []

    containers: list[ContainerInfo] = []
    if result.stdout:
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = _json.loads(line)
                containers.append(
                    ContainerInfo(
                        name=data.get("Name", ""),
                        service=data.get("Service", ""),
                        state=data.get("State", ""),
                        health=data.get("Health", None) or None,
                        status=data.get("Status", ""),
                    )
                )
            except _json.JSONDecodeError:
                continue
    return containers


def _warm_check(instance_dir: str, name: str, host_user: str, auth: MachinectlAuth = MachinectlAuth.SUDO) -> bool:
    """Check if containers are already running. Returns True if warm.

    Delegates to _container_status (D-3) — returns True if any containers exist.
    """
    compose_file = os.path.join(instance_dir, "docker", "compose.yml")
    if not os.path.exists(compose_file):
        return False

    config = _load_config(instance_dir)
    return bool(_container_status(instance_dir, name, host_user, config, auth))


# ─── Locking ─────────────────────────────────────────────────────────────────


def _acquire_state_lock(instance_dir: str) -> int:
    """Acquire the per-user state lock. Returns fd. Raises BlockingIOError on contention."""
    del instance_dir
    lock_path = str(state_lock_path())
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        raise


def _release_lock(fd: int) -> None:
    """Release and close a lock fd."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


# ─── Phase implementations ──────────────────────────────────────────────────


def _phase_ipam(sandbox_ai_home: str, instance_id: str) -> int:
    """Phase 2: IPAM allocation. Returns base_index."""
    del sandbox_ai_home
    ledger = IPAMLedger()
    return ledger.allocate(instance_id)


def _phase_credentials(
    instance_dir: str,
    core_ipc_ip: str,
) -> str:
    """Phase 3: Generate proxy credentials and SSH keypairs.

    Returns proxy password. Credential ownership matching is handled
    separately by _phase_credential_ownership() after ACL grants.
    """
    password = generate_credential()
    hashed = hash_proxy_password(password)
    htpasswd_line = f"proxyuser:{hashed}"
    config_proxy_dir = os.path.join(instance_dir, "config", "proxy")
    write_htpasswd(config_proxy_dir, htpasswd_line)

    # SSH keypairs for IPC transport
    generate_ssh_keypair(instance_dir, "auth")
    generate_ssh_keypair(instance_dir, "host", core_ipc_ip=core_ipc_ip)

    return password


def _phase_credential_ownership(
    instance_dir: str,
    host_user: str,
    secrets_dir: str | None = None,
    auth: MachinectlAuth = MachinectlAuth.SUDO,
) -> None:
    """Phase 5b: Credential ownership matching via disposable helper container.

    Runs chown(2) on all four IPC secret files to converge ownership to
    uid 1000 inside the rootless Docker namespace. Must execute after ACL
    grants (Phase 5) so the rootless Docker daemon can traverse the
    instance directory tree.
    """
    resolved_secrets_dir = secrets_dir or os.path.join(instance_dir, "secrets")
    secret_files = ("ipc_host_key", "authorized_keys", "ipc_ssh_key", "ipc_known_hosts")
    filenames = " ".join(secret_files)
    volume_args = f"-v {resolved_secrets_dir}:/secrets"
    docker_cmd = (
        f"docker run --rm --runtime=runc {volume_args} busybox sh -c '"
        f"for f in {filenames}; do "
        f"cp /secrets/$f /tmp/$f && chown 1000:1000 /tmp/$f && chmod 0600 /tmp/$f && mv /tmp/$f /secrets/$f; "
        f"done'"
    )

    try:
        subprocess.run(["setfacl", "-m", f"u:{host_user}:rwX", resolved_secrets_dir], check=True)
    except subprocess.CalledProcessError as e:
        raise SandboxExecutionError(f"Failed to escalate ACLs for secrets directory: {e}") from e

    executor = Executor()
    try:
        executor.run(
            [
                *machinectl_cmd(host_user, auth),
                "/bin/bash",
                "-c",
                docker_cmd,
            ],
            sentinel=True,
        )
    except SandboxExecutionError as e:
        raise SandboxExecutionError(f"Credential ownership matching failed: {e}") from e
    finally:
        try:
            subprocess.run(["setfacl", "-m", f"u:{host_user}:rX", resolved_secrets_dir], check=True)
        except subprocess.CalledProcessError as e:
            raise SandboxExecutionError(
                f"Failed to downgrade ACLs for secrets directory. "
                f"Fix: sudo setfacl -m u:{host_user}:rX {resolved_secrets_dir}"
            ) from e


def _phase_hydrate(
    config: InstanceConfig,
    base_index: int,
    proxy_password: str,
    sandbox_ai_home: str,
    instance_dir: str,
) -> None:
    """Phase 4: Pydantic + Jinja2 hydration pipeline."""
    context = build_jinja_context(config, base_index, proxy_password, instance_dir)
    render_templates(
        context,
        sandbox_ai_home,
        instance_dir,
        db_postgres=config.components_db_postgres.enabled,
        mcp_firecrawl=config.components.mcp_firecrawl,
    )


def _compute_ancestors(instance_dir: str) -> list[str]:
    """Walk ancestor directories from instance_dir up to the ownership boundary.

    Returns directories owned by the current UID, excluding root (/) and
    the instance_dir itself. Stops at the first directory not owned by
    the current process UID.

    Returns:
        List of ancestor paths in top-down order (shallowest first).
    """
    current_uid = os.getuid()
    abs_path = os.path.abspath(instance_dir)
    ancestors: list[str] = []

    parent = os.path.dirname(abs_path)
    while parent != "/":
        try:
            stat = os.stat(parent)
        except OSError:
            break
        if stat.st_uid != current_uid:
            break
        ancestors.append(parent)
        parent = os.path.dirname(parent)

    # Return in top-down order (shallowest ancestor first)
    ancestors.reverse()
    return ancestors


def _acl_grant_plan(instance_dir: str, host_user: str) -> list[tuple[list[str], str]]:
    """Build the ACL grant plan — single source of truth for Phase 5 and dry-run (D4).

    Returns a list of (setfacl_args, description) tuples:
    - Ancestors: ``--x`` (traverse only)
    - Instance root: ``r-x``
    - docker/: ``rX`` recursive
    - config/: ``rX`` recursive
    - .sandbox.env: ``r``
    """
    plan: list[tuple[list[str], str]] = []

    # Ancestors — execute-only traverse
    for ancestor in _compute_ancestors(instance_dir):
        plan.append(
            (
                ["setfacl", "-m", f"u:{host_user}:--x", ancestor],
                f"ancestor traverse: {ancestor}",
            )
        )

    # Instance root — read + execute
    plan.append(
        (
            ["setfacl", "-m", f"u:{host_user}:r-x", instance_dir],
            f"instance root: {instance_dir}",
        )
    )

    # docker/ — recursive read + conditional execute
    docker_dir = os.path.join(instance_dir, "docker/")
    plan.append(
        (
            ["setfacl", "-R", "-m", f"u:{host_user}:rX", docker_dir],
            f"docker config: {docker_dir}",
        )
    )

    # config/ — recursive read + conditional execute
    config_dir = os.path.join(instance_dir, "config/")
    plan.append(
        (
            ["setfacl", "-R", "-m", f"u:{host_user}:rX", config_dir],
            f"config files: {config_dir}",
        )
    )

    # .sandbox.env — read only
    env_file = os.path.join(instance_dir, ".sandbox.env")
    plan.append(
        (
            ["setfacl", "-m", f"u:{host_user}:r", env_file],
            f"env file: {env_file}",
        )
    )

    # secrets/ — recursive read + conditional execute
    secrets_dir = os.path.join(instance_dir, "secrets/")
    plan.append(
        (
            ["setfacl", "-R", "-m", f"u:{host_user}:rX", secrets_dir],
            f"secrets dir: {secrets_dir}",
        )
    )

    # ── rw bind-mount sources for rootless Docker ──────────────────────
    # Rootless Docker's namespace-root maps to host_user. Every rw bind-mount
    # source needs write access from that user for mountpoint creation.
    # Grant rwX on specific subdirectories, not the entire cache/ or log/ tree.

    rw_mount_sources = [
        "cache/core/.claude",
        "cache/admin/tmux_resurrect",
        "log/core",
        "log/admin",
    ]
    for subdir in rw_mount_sources:
        target = os.path.join(instance_dir, subdir)
        # Effective ACL: rwX for existing files/dirs
        plan.append(
            (
                ["setfacl", "-R", "-m", f"u:{host_user}:rwX", target],
                f"rw mount source: {target}",
            )
        )
        # Default ACL: rwX for future files/dirs created by containers
        plan.append(
            (
                ["setfacl", "-R", "-d", "-m", f"u:{host_user}:rwX", target],
                f"rw mount default: {target}",
            )
        )

    return plan


def _acl_revoke_plan(instance_dir: str, host_user: str) -> list[tuple[list[str], str]]:
    """Build the ACL revoke plan — intentionally asymmetric with grant plan (D4).

    Ancestors are NOT revoked (D3 — grant-only model). Returns a list of
    (setfacl_args, description) tuples for: instance root, docker/ (recursive),
    config/ (recursive), .sandbox.env, and rw bind-mount sources
    (cache/core/.claude, cache/admin/tmux_resurrect, log/core, log/admin)
    with both effective and default ACL entries.
    """
    plan: list[tuple[list[str], str]] = []

    # Instance root
    plan.append(
        (
            ["setfacl", "-x", f"u:{host_user}", instance_dir],
            f"instance root: {instance_dir}",
        )
    )

    # docker/ — recursive
    docker_dir = os.path.join(instance_dir, "docker/")
    plan.append(
        (
            ["setfacl", "-R", "-x", f"u:{host_user}", docker_dir],
            f"docker config: {docker_dir}",
        )
    )

    # config/ — recursive
    config_dir = os.path.join(instance_dir, "config/")
    plan.append(
        (
            ["setfacl", "-R", "-x", f"u:{host_user}", config_dir],
            f"config files: {config_dir}",
        )
    )

    # .sandbox.env
    env_file = os.path.join(instance_dir, ".sandbox.env")
    plan.append(
        (
            ["setfacl", "-x", f"u:{host_user}", env_file],
            f"env file: {env_file}",
        )
    )

    # rw bind-mount sources — effective + default ACL removal
    rw_mount_sources = [
        "cache/core/.claude",
        "cache/admin/tmux_resurrect",
        "log/core",
        "log/admin",
    ]
    for subdir in rw_mount_sources:
        target = os.path.join(instance_dir, subdir)
        plan.append(
            (
                ["setfacl", "-R", "-x", f"u:{host_user}", target],
                f"rw mount source: {target}",
            )
        )
        plan.append(
            (
                ["setfacl", "-R", "-d", "-x", f"u:{host_user}", target],
                f"rw mount default: {target}",
            )
        )

    return plan


def _diagnose_traverse_failure(instance_dir: str, host_user: str) -> str:
    """Diagnose which ancestor directory lacks execute permission for the sandbox user.

    Walks the ancestor chain from instance_dir upward, checking ``--x`` via
    ``os.stat()`` mode bits. Reports the first failure point with a fix command.

    Only runs on the failure path — lightweight (D10).

    Returns:
        Diagnostic message string, or empty string if no failure found.
    """
    import pwd
    import stat

    try:
        pw = pwd.getpwnam(host_user)
        target_uid = pw.pw_uid
        target_gid = pw.pw_gid
    except KeyError:
        return f"Diagnosis: user '{host_user}' does not exist on this host."

    abs_path = os.path.abspath(instance_dir)
    components: list[str] = []
    current = abs_path
    while current != "/":
        components.append(current)
        current = os.path.dirname(current)
    components.append("/")
    components.reverse()

    for directory in components:
        try:
            st = os.stat(directory)
        except OSError:
            return f"Diagnosis: cannot stat '{directory}'.\nFix: verify directory exists and is accessible."

        mode = st.st_mode
        # Check execute permission for the target user
        has_exec = False
        if st.st_uid == target_uid:
            has_exec = bool(mode & stat.S_IXUSR)
        elif st.st_gid == target_gid:
            has_exec = bool(mode & stat.S_IXGRP)
        else:
            has_exec = bool(mode & stat.S_IXOTH)

        if not has_exec:
            return (
                f"Diagnosis: sandbox user '{host_user}' lacks execute permission"
                f" on {directory}\n"
                f"Fix: setfacl -m u:{host_user}:--x {directory}\n"
                f"Run 'sandbox doctor' for full host readiness diagnostics."
            )

    return ""


def _phase_acl_grant(instance_dir: str, host_user: str) -> None:
    """Phase 5: Grant sandbox user ACLs via _acl_grant_plan() (Pattern A).

    Each setfacl call runs as direct subprocess.run (NOT via Executor.run —
    sentinel injection would corrupt the setfacl command, per I-1).
    CalledProcessError is wrapped in SandboxExecutionError (D6).
    """
    for acl_cmd, description in _acl_grant_plan(instance_dir, host_user):
        try:
            subprocess.run(acl_cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            diag = _diagnose_traverse_failure(instance_dir, host_user)
            error_msg = f"ACL grant failed for {description}"
            if e.stderr:
                error_msg += f": {e.stderr.strip()}"
            if diag:
                error_msg += f"\n{diag}"
            raise SandboxExecutionError(error_msg) from e


def _build_compose_files(instance_dir: str, config: InstanceConfig) -> list[str]:
    """Build the compose file list including component-conditional extras."""
    files = ["-f", os.path.join(instance_dir, "docker", "compose.yml")]
    if config.components_db_postgres.enabled:
        extras = os.path.join(instance_dir, "docker", "extras", "db-postgres.yml")
        files.extend(["-f", extras])
    if config.components.mcp_firecrawl:
        extras = os.path.join(instance_dir, "docker", "extras", "mcp-firecrawl.yml")
        files.extend(["-f", extras])
    return files


def _phase_compose_up(
    instance_dir: str,
    name: str,
    host_user: str,
    config: InstanceConfig,
    auth: MachinectlAuth = MachinectlAuth.SUDO,
) -> None:
    """Phase 6: docker compose up -d --build --wait via machinectl.

    - Source suppression env prefix: TERM=dumb NO_COLOR=1 BUILDKIT_PROGRESS=plain (D2 Layer 1)
    - --ansi never flag (D2 Layer 1)
    - --env-file injection for .sandbox.env (D14)
    - sentinel=True for exit code recovery (D1)
    """
    compose_files = _build_compose_files(instance_dir, config)
    files_str = " ".join(compose_files)
    env_file = os.path.join(instance_dir, ".sandbox.env")
    cmd = (
        f"TERM=dumb NO_COLOR=1 BUILDKIT_PROGRESS=plain "
        f"COMPOSE_PROJECT_NAME={name} docker compose {files_str} "
        f"--ansi never --env-file {env_file} up -d --build --wait"
    )
    executor = Executor()
    executor.run(
        [
            *machinectl_cmd(host_user, auth),
            "/bin/bash",
            "-c",
            cmd,
        ],
        sentinel=True,
    )


def _phase_handover(
    name: str,
    host_user: str,
    warmup_prompt: str = "",
    auth: MachinectlAuth = MachinectlAuth.SUDO,
) -> None:
    """Phase 7: PTY handover — docker exec -it via machinectl."""
    executor = Executor()
    exec_args = ["exec"]
    if warmup_prompt:
        exec_args.extend(["-e", f"SANDBOX_WARMUP_PROMPT={warmup_prompt}"])
    exec_args.extend(["-it", f"{name}-admin-1", "zsh"])

    executor.run(
        [
            *machinectl_cmd(host_user, auth),
            "/usr/bin/docker",
            *exec_args,
        ],
        interactive=True,
    )


# ─── Compose down / ACL revoke ──────────────────────────────────────────────


def _compose_down(
    instance_dir: str,
    name: str,
    host_user: str,
    config: InstanceConfig,
    *,
    volumes: bool = False,
    auth: MachinectlAuth = MachinectlAuth.SUDO,
) -> None:
    """Run docker compose down via machinectl. Pass volumes=True for -v."""
    compose_files = _build_compose_files(instance_dir, config)
    files_str = " ".join(compose_files)
    v_flag = " -v" if volumes else ""
    env_file = os.path.join(instance_dir, ".sandbox.env")
    cmd = (
        f"TERM=dumb NO_COLOR=1 BUILDKIT_PROGRESS=plain "
        f"COMPOSE_PROJECT_NAME={name} docker compose {files_str} "
        f"--ansi never --env-file {env_file} down{v_flag}"
    )
    executor = Executor()
    executor.run(
        [
            *machinectl_cmd(host_user, auth),
            "/bin/bash",
            "-c",
            cmd,
        ],
        sentinel=True,
    )


def _revoke_acls(instance_dir: str, host_user: str) -> list[str]:
    """Revoke sandbox user's ACL entries — fault-isolated, best-effort (D5).

    Iterates _acl_revoke_plan(). Uses check=False; failures are collected
    as warning strings, not raised. Returns list of warning messages.
    """
    warnings: list[str] = []
    for acl_cmd, description in _acl_revoke_plan(instance_dir, host_user):
        try:
            result = subprocess.run(acl_cmd, check=False, capture_output=True, text=True)
            if result.returncode != 0:
                detail = result.stderr.strip() if result.stderr else f"exit {result.returncode}"
                warnings.append(f"ACL revoke warning for {description}: {detail}")
        except OSError as e:
            warnings.append(f"ACL revoke warning for {description}: {e}")
    return warnings


# ─── Dry-Run Pipeline ───────────────────────────────────────────────────────


def _dry_run_pipeline(sandbox_ai_home: str, project_dir: str) -> None:
    """Simulate the full start pipeline without side effects.

    Validates config parsing, IPAM allocation, template rendering, secret
    completeness, and previews the subprocess commands that would execute.
    """
    console.print("\n[bold]Dry-run: sandbox start[/bold]\n")

    # ── Instance resolution ──────────────────────────────────────────────
    instance_dir, instance_id = _resolve_instance(sandbox_ai_home, project_dir)

    if instance_dir is None or instance_id is None:
        console.print(
            "No sandbox instance found. Run `sandbox init` first.",
            style="red",
        )
        raise typer.Exit(code=1)

    console.print(f"  Instance: [green]{instance_id}[/green] (existing)")
    config = _load_config(instance_dir)
    host_user, auth = _resolve_host_config(project_dir, config)

    name = config.instance.name

    # ── IPAM preview ─────────────────────────────────────────────────────
    ledger = IPAMLedger()
    try:
        slot, is_existing = ledger.peek_next_slot(instance_id)
        isolated, core_proxy, dns, admin, admin_proxy, egress, ipc = derive_subnets(slot)
        status = "existing" if is_existing else "preview — subject to concurrent changes"
        console.print(f"\n  IPAM slot: {slot} ({status})")
        console.print(f"    Isolated:    {isolated}")
        console.print(f"    Core Proxy:  {core_proxy}")
        console.print(f"    DNS:         {dns}")
        console.print(f"    Admin:       {admin}")
        console.print(f"    Admin Proxy: {admin_proxy}")
        console.print(f"    Egress:      {egress}")
        console.print(f"    IPC:         {ipc}")
    except IPAMExhaustedError as e:
        console.print(f"\n  [red]IPAM: {e}[/red]")
        raise typer.Exit(code=1) from None

    # ── Template validation ──────────────────────────────────────────────
    context = build_jinja_context(config, slot, "DRY_RUN_PASSWORD", instance_dir)
    validated, errors = validate_templates(
        context,
        sandbox_ai_home,
        db_postgres=config.components_db_postgres.enabled,
        mcp_firecrawl=config.components.mcp_firecrawl,
    )

    if errors:
        console.print("\n  [red]Template validation failed:[/red]")
        for err in errors:
            console.print(f"    ✗ {err}", style="red")
        raise typer.Exit(code=1) from None
    else:
        console.print(f"\n  Templates: [green]{validated} validated[/green]")

    # ── Secret completeness check ────────────────────────────────────────
    env_path = os.path.join(instance_dir, ".sandbox.env")
    missing_secrets = _check_secrets(env_path, config)
    if missing_secrets:
        console.print("\n  [yellow]Missing/empty secrets:[/yellow]")
        for secret in missing_secrets:
            console.print(f"    ⊘ {secret}")

    # ── Command preview ──────────────────────────────────────────────────
    compose_files = _build_compose_files(instance_dir, config)
    files_str = " ".join(compose_files)

    console.print("\n  [bold]Commands that would execute:[/bold]")

    # ACL grants — consume _acl_grant_plan (D4 — single source of truth)
    for acl_cmd, description in _acl_grant_plan(instance_dir, host_user):
        console.print(f"    $ {' '.join(acl_cmd)}  # {description}", style="dim")

    # Compose up — match actual _phase_compose_up command
    env_file = os.path.join(instance_dir, ".sandbox.env")
    machinectl_prefix = " ".join(machinectl_cmd(host_user, auth))
    compose_cmd = (
        f"{machinectl_prefix} /bin/bash -c "
        f"'TERM=dumb NO_COLOR=1 BUILDKIT_PROGRESS=plain "
        f"COMPOSE_PROJECT_NAME={name} docker compose {files_str} "
        f"--ansi never --env-file {env_file} up -d --build --wait'"
    )
    console.print(f"    $ {compose_cmd}", style="dim")

    # Handover
    handover_cmd = f"{machinectl_prefix} /usr/bin/docker exec -it {name}-admin-1 zsh"
    console.print(f"    $ {handover_cmd}", style="dim")

    console.print("\n  [green bold]Dry-run complete — all validations passed[/green bold]\n")


def _check_secrets(env_path: str, config: InstanceConfig) -> list[str]:
    """Check for missing or empty secrets in .sandbox.env."""
    required = ["CORE_ANTHROPIC_API_KEY"]
    if config.components_db_postgres.enabled:
        required.append("PG_PASSWORD")  # Auto-generated at init — validated here, not prompted
    if config.components.mcp_firecrawl:
        required.append("FIRECRAWL_API_KEY")

    missing: list[str] = []
    env_vars: dict[str, str] = {}

    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, _, val = line.partition("=")
                    env_vars[key.strip()] = val.strip().strip('"')

    for key in required:
        if key not in env_vars or not env_vars[key]:
            missing.append(key)

    return missing


def _resolve_host_config(project_dir: str, config: InstanceConfig) -> tuple[str, MachinectlAuth]:
    """Resolve host_user and auth from per-host ``sandbox-ai.toml``.

    Post-init commands SHALL fail when host config is absent — the field
    no longer exists on the per-instance ``SandboxInstanceSection``.
    """
    del project_dir, config  # accepted for signature stability; host config is authoritative
    try:
        project_config = HostConfig.from_toml()
    except FileNotFoundError as exc:
        console.print(str(exc), style="red")
        raise typer.Exit(code=1) from None
    return project_config.host.docker_unprivileged_user, project_config.host.machinectl_authentication


def _emit_auth_probe_failure(auth: MachinectlAuth, user: str, detail: str) -> None:
    """Print mode-specific remediation guidance when the init-time auth probe fails."""
    console.print(f"machinectl auth probe failed for user '{user}': {detail}", style="red")
    if auth == MachinectlAuth.SUDO:
        console.print(
            "Remediation: Verify 'sudo machinectl shell' works for this user. "
            "Ensure the user exists and sudo is configured.",
            style="yellow",
        )
    else:
        console.print(
            "Remediation: Verify polkit rules allow 'machinectl shell' without sudo. "
            "Ensure org.freedesktop.machine1.shell policy is configured for this user.",
            style="yellow",
        )


# ─── CLI Commands ────────────────────────────────────────────────────────────


def _stdin_is_tty() -> bool:
    """Boolean wrapper around ``sys.stdin.isatty()`` for ergonomic test patching.

    Why a wrapper: typer's ``CliRunner`` substitutes ``sys.stdin`` with an
    in-memory buffer at invoke time, which breaks ``patch("cli.main.sys.stdin")``
    for tests that need to simulate TTY mode. Patching this function instead
    is robust to runner-level stdin replacement.
    """
    return sys.stdin.isatty()


def _require_per_user_state_initialized() -> None:
    """Hard-fail when ``<home>/state/instances.json`` is absent.

    Lifecycle commands (`start`, `stop`, `destroy`, `status`, `attach`) call
    this as their first step. Initialization is signaled by the registry
    file's presence, which `sandbox init` writes via `ensure_registry_seed`.
    """
    home = sandbox_ai_user_home()
    registry = home / "state" / "instances.json"
    if not registry.exists():
        console.print(
            f"Error: per-user state not initialized at {home}. Run `sandbox init` first.",
            style="red",
        )
        raise typer.Exit(code=1)


def _seed_host_config_if_absent(user_home: Path, *, dry_run: bool) -> None:
    """Seed ``<user_home>/config/sandbox-ai.toml`` when missing.

    TTY mode prompts for ``docker_unprivileged_user`` (required, non-empty)
    and ``machinectl_authentication`` (default ``sudo``). Non-TTY mode exits
    with explicit guidance. Existing files are never overwritten. Dry-run
    skips seeding entirely.
    """
    config_path = user_home / "config" / "sandbox-ai.toml"
    if config_path.exists() or dry_run:
        return

    if not _stdin_is_tty():
        console.print(
            f"Cannot prompt for docker_unprivileged_user in non-interactive mode. "
            f"Create {config_path} with a [host] section containing docker_unprivileged_user "
            f"before running sandbox init.",
            style="red",
        )
        raise typer.Exit(code=1)

    while True:
        docker_user = typer.prompt("docker_unprivileged_user (e.g., sandbox)").strip()
        if docker_user:
            break
        console.print("docker_unprivileged_user must not be empty.", style="yellow")

    auth_input = (
        typer.prompt(
            "machinectl_authentication [sudo/polkit, default sudo]",
            default="sudo",
            show_default=False,
        ).strip()
        or "sudo"
    )
    try:
        resolved_auth_for_seed = MachinectlAuth(auth_input)
    except ValueError as exc:
        console.print(
            f"Invalid machinectl_authentication value: '{auth_input}'. Must be 'sudo' or 'polkit'.",
            style="red",
        )
        raise typer.Exit(code=1) from exc

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "[host]\n"
        f'docker_unprivileged_user = "{docker_user}"\n'
        f'machinectl_authentication = "{resolved_auth_for_seed.value}"\n'
    )


def _warn_legacy_cwd_files(project_dir: str, user_home: Path) -> None:
    """Warn when legacy ``<cwd>/sandbox-ai.toml`` or ``<cwd>/.state/`` exists.

    The legacy path tokens in this docstring are intentional and load-bearing:
    they preserve the exact strings users may grep for during migration.
    Per the per-user-config-and-state-relocation change (task 14.7), do not
    remove them in future cleanups.
    """
    legacy_toml = os.path.join(project_dir, "sandbox-ai.toml")
    if os.path.exists(legacy_toml):
        console.print(
            f"Found legacy {legacy_toml}. Per-host config now lives at "
            f"{user_home / 'config' / 'sandbox-ai.toml'}. "
            "Migrate manually or delete the legacy file.",
            style="yellow",
        )
    legacy_state = os.path.join(project_dir, ".state")
    if os.path.isdir(legacy_state):
        console.print(
            f"Found legacy {legacy_state}. Orchestrator state now lives at "
            f"{user_home / 'state'}. Migrate manually or delete the legacy directory.",
            style="yellow",
        )


@app.command()
def init(
    machinectl_auth: str | None = typer.Option(
        None, "--machinectl-auth", help="machinectl auth mode: 'sudo' or 'polkit'"
    ),
    git_user: str = typer.Option("", "--git-user", help="Git user.name (auto-detected if omitted)"),
    git_email: str = typer.Option("", "--git-email", help="Git user.email (auto-detected if omitted)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview scaffold without writing"),
) -> None:
    """Initialize a new sandbox instance for the current project."""
    sandbox_ai_home = _resolve_sandbox_ai_home()
    project_dir = _resolve_project_dir()

    # Per-user tree creation (idempotent, mode 0700)
    user_home = sandbox_ai_user_home()
    ensure_per_user_tree(user_home)

    # Legacy CWD-local file detection (advisory)
    _warn_legacy_cwd_files(project_dir, user_home)

    # Re-init guard (D-6)
    instance_dir, instance_id = _resolve_instance(sandbox_ai_home, project_dir)
    if instance_dir is not None:
        console.print(
            "Instance already initialized for this directory. Run `sandbox destroy` first.",
            style="red",
        )
        raise typer.Exit(code=1)

    # Seed canonical host config (TTY prompt or non-TTY fail) when absent
    _seed_host_config_if_absent(user_home, dry_run=dry_run)

    # Resolution: docker_unprivileged_user — canonical host config is authoritative
    project_config: HostConfig | None = None
    try:
        project_config = HostConfig.from_toml()
    except FileNotFoundError:
        pass

    resolved_user: str
    if project_config is not None:
        resolved_user = project_config.host.docker_unprivileged_user
    elif dry_run:
        resolved_user = "<dry-run>"
    else:
        # Should not happen post-seed in interactive mode; non-TTY already exited above.
        console.print(
            f"No host config at {user_home / 'config' / 'sandbox-ai.toml'}. "
            "Run sandbox init in an interactive shell or create the file manually.",
            style="red",
        )
        raise typer.Exit(code=1)

    # Resolution: machinectl_authentication (--machinectl-auth flag → host config → default sudo)
    resolved_auth: MachinectlAuth
    if machinectl_auth is not None:
        try:
            resolved_auth = MachinectlAuth(machinectl_auth)
        except ValueError:
            console.print(
                f"Invalid --machinectl-auth value: '{machinectl_auth}'. Must be 'sudo' or 'polkit'.",
                style="red",
            )
            raise typer.Exit(code=1) from None
    elif project_config is not None:
        resolved_auth = project_config.host.machinectl_authentication
    else:
        resolved_auth = MachinectlAuth.SUDO

    # Init-time auth mode probe (D5)
    if not dry_run:
        probe_cmd = machinectl_cmd(resolved_user, resolved_auth)
        probe_cmd_full = [*probe_cmd, "/bin/bash", "-c", "echo ok"]
        try:
            result = subprocess.run(
                probe_cmd_full,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                _emit_auth_probe_failure(resolved_auth, resolved_user, result.stderr)
                raise typer.Exit(code=1)
        except subprocess.TimeoutExpired:
            _emit_auth_probe_failure(resolved_auth, resolved_user, "probe timed out after 5 seconds")
            raise typer.Exit(code=1) from None
        except FileNotFoundError:
            _emit_auth_probe_failure(resolved_auth, resolved_user, "command not found on PATH")
            raise typer.Exit(code=1) from None

    # Doctor pre-flight: Chain 2 (Filesystem) + Chain 3 (Repo Integrity)
    # ancestor_traverse is excluded — ACLs are granted during `start`, not `init`,
    # so the ancestor check would always fail on first init (D10).
    if not dry_run:
        distro = detect_distro()
        preflight_results = run_check_subset(
            ["Filesystem", "Repo Integrity"],
            resolved_user,
            distro,
            exclude_ids={"ancestor_traverse"},
            auth_mode=resolved_auth,
        )
        has_failures = any(r.status == "fail" for r in preflight_results)
        if has_failures:
            render_results(preflight_results, console=console)
            raise typer.Exit(code=1)

    # Git config auto-detection (D-8)
    if not git_user or not git_email:
        detected_name, detected_email = _detect_git_config()
        if not git_user:
            git_user = detected_name
        if not git_email:
            git_email = detected_email

    # Derive instance identity
    instance_id = generate_instance_id(project_dir)
    instance_dir = os.path.join(sandbox_ai_home, "sandboxes", instance_id)
    instance_name = os.path.basename(project_dir)

    if dry_run:
        console.print("\n[bold]Dry-run: sandbox init[/bold]\n")
        console.print(f"  Instance ID: {instance_id}")
        console.print(f"  Directory: {instance_dir}")
        console.print(f"  User: {resolved_user}")
        console.print(f"  Git: {git_user} <{git_email}>")
        console.print(f"  Project: {instance_name}")
        console.print("\n  [green bold]Dry-run complete — no files written[/green bold]\n")
        return

    # S1: Directory tree
    create_instance_dirs(instance_dir)

    # S2: sandbox.toml
    write_sandbox_toml(
        instance_dir,
        instance_name,
        project_dir,
        git_user=git_user,
        git_email=git_email,
    )

    # S3: .sandbox.env
    config = _load_config(instance_dir)
    env_path = os.path.join(instance_dir, ".sandbox.env")
    create_env_file(
        env_path,
        db_postgres=config.components_db_postgres.enabled,
        mcp_firecrawl=config.components.mcp_firecrawl,
    )

    # S4: Default ACLs (Pattern B)
    dev_user = os.environ.get("USER", "dev")
    apply_default_acls(instance_dir, config.instance.user_project_root, dev_user)

    # S5: Register — ensure registry seed exists, then register
    ensure_registry_seed(user_home)
    registry = InstanceRegistry()
    registry.register(project_dir, instance_id)

    # S6: Secret prompting (non-TTY safe)
    required_secrets: list[tuple[str, str]] = [
        ("CORE_ANTHROPIC_API_KEY", "Anthropic API key"),
        ("CORE_GITHUB_TOKEN", "GitHub personal access token"),
    ]
    # PG_PASSWORD is auto-generated at scaffold time — not prompted
    if config.components.mcp_firecrawl:
        required_secrets.append(("FIRECRAWL_API_KEY", "Firecrawl API key"))
    prompt_secrets(
        env_path,
        required_secrets,
        prompt_func=lambda msg: typer.prompt(msg, hide_input=True),
    )

    # S7: Sentinel
    write_initialized_sentinel(instance_dir)

    console.print(f"Sandbox '{instance_name}' initialized. Run `sandbox start` to launch.")


@app.command()
def start(
    dry_run: bool = typer.Option(False, "--dry-run", help="Simulate start without side effects"),
) -> None:
    """Start the sandbox."""
    _require_per_user_state_initialized()
    sandbox_ai_home = _resolve_sandbox_ai_home()
    project_dir = _resolve_project_dir()

    if dry_run:
        _dry_run_pipeline(sandbox_ai_home, project_dir)
        return

    # Phase 0: Instance resolution
    instance_dir, instance_id = _resolve_instance(sandbox_ai_home, project_dir)

    if instance_dir is None or instance_id is None:
        console.print(
            "No sandbox instance found. Run `sandbox init` first.",
            style="red",
        )
        raise typer.Exit(code=1)

    # Sentinel check: verify init completed
    sentinel_path = os.path.join(instance_dir, ".initialized")
    if not os.path.exists(sentinel_path):
        console.print(
            "Instance partially initialized. Run `sandbox destroy` then `sandbox init`.",
            style="red",
        )
        raise typer.Exit(code=1)

    config = _load_config(instance_dir)
    name = config.instance.name
    host_user, auth = _resolve_host_config(project_dir, config)

    # Project name immutability check (sandbox-toml-schema spec)
    # instance_id format: <instance_name>-<md5[:6]> — strip last 7 chars to recover original name
    original_name = instance_id[:-7]
    if name != original_name:
        console.print(
            "WARNING: instance.name has changed since init. "
            "COMPOSE_PROJECT_NAME mismatch may orphan running containers.",
            style="yellow",
        )

    # Pre-flight: Secret completeness gate (D-7 — before lock acquisition)
    env_path = os.path.join(instance_dir, ".sandbox.env")
    missing_secrets = _check_secrets(env_path, config)
    if missing_secrets:
        console.print("Missing required secrets:", style="red")
        for secret in missing_secrets:
            console.print(f"  ⊘ {secret}", style="red")
        console.print(
            "\nPopulate secrets in .sandbox.env and retry.",
            style="red",
        )
        raise typer.Exit(code=1)

    # Pre-flight: Doctor Chain 1 — Privilege Boundary (before warm check)
    distro = detect_distro()
    preflight_results = run_check_subset(["Privilege Boundary"], host_user, distro, auth_mode=auth)
    has_preflight_failures = any(r.status == "fail" for r in preflight_results)
    if has_preflight_failures:
        render_results(preflight_results, console=console)
        raise typer.Exit(code=1)

    # Pre-lock warm check (D-52)
    if _warm_check(instance_dir, name, host_user, auth):
        console.print(f"Sandbox '{name}' is already running. Use 'sandbox attach' to reconnect.")
        return

    # Phase 1: Locking
    lock_fd: int | None = None
    try:
        lock_fd = _acquire_state_lock(instance_dir)
    except BlockingIOError:
        console.print(
            "Another sandbox start is already in progress for this instance.",
            style="red bold",
        )
        raise typer.Exit(code=1) from None

    acl_granted = False
    try:
        # Phase 2: IPAM
        base_index = _phase_ipam(sandbox_ai_home, instance_id)
        console.print("✓ IPAM — network allocation complete")

        # Phase 3: Credentials (generation only)
        proxy_password = _phase_credentials(
            instance_dir,
            core_ipc_ip=derive_static_ips(base_index)["core_ipc_ip"],
        )
        console.print("✓ Credentials — proxy auth + SSH keypairs configured")

        # Phase 4: Hydration
        _phase_hydrate(config, base_index, proxy_password, sandbox_ai_home, instance_dir)
        console.print("✓ Hydration — templates rendered")

        # Phase 5: ACL grants (Pattern A)
        acl_granted = True  # set BEFORE Phase 5 — handles partial grants (D7)
        _phase_acl_grant(instance_dir, host_user)
        console.print("✓ ACL — filesystem permissions granted")

        # Phase 5b: Credential ownership matching (after ACL grants)
        _phase_credential_ownership(instance_dir, host_user, auth=auth)
        console.print("✓ Ownership — credential files converged")

        # Phase 6: Compose up (D-5 — spinner for long-running phase)
        with console.status("⟳ Compose — starting containers…"):
            _phase_compose_up(instance_dir, name, host_user, config, auth)
        console.print("✓ Compose — containers healthy")

    except (IPAMExhaustedError, SandboxExecutionError) as e:
        console.print(f"[FATAL] {e}", style="red bold")
        # ACL cleanup on failure — only if Phase 5 has begun (D7)
        if acl_granted:
            acl_warnings = _revoke_acls(instance_dir, host_user)
            for w in acl_warnings:
                console.print(f"⚠ {w}", style="yellow")
        if lock_fd is not None:
            _release_lock(lock_fd)
        raise typer.Exit(code=1) from None

    # Phase 7: Handover — release lock first
    if lock_fd is not None:
        _release_lock(lock_fd)

    console.print("→ Handing over to admin shell")
    _phase_handover(name, host_user, config.instance.warmup_prompt, auth)


@app.command()
def stop(clean: bool = False) -> None:
    """Stop the sandbox."""
    _require_per_user_state_initialized()
    sandbox_ai_home = _resolve_sandbox_ai_home()
    project_dir = _resolve_project_dir()

    instance_dir, instance_id = _resolve_instance(sandbox_ai_home, project_dir)
    if instance_dir is None or instance_id is None:
        console.print("No sandbox instance found for this directory.", style="red")
        raise typer.Exit(code=1)

    config = _load_config(instance_dir)
    name = config.instance.name
    host_user, auth = _resolve_host_config(project_dir, config)

    # Warm check
    if not _warm_check(instance_dir, name, host_user, auth):
        console.print(f"Sandbox '{name}' is not running. Nothing to stop.")
        return

    # Lock acquisition (D8)
    try:
        lock_fd = _acquire_state_lock(instance_dir)
    except BlockingIOError:
        console.print(
            "Another sandbox operation is already in progress for this instance.",
            style="red bold",
        )
        raise typer.Exit(code=1) from None

    # Compose down
    _compose_down(instance_dir, name, host_user, config, volumes=clean, auth=auth)

    # ACL revocation (Pattern A) — fault-isolated (D5)
    acl_warnings = _revoke_acls(instance_dir, host_user)
    for w in acl_warnings:
        console.print(f"⚠ {w}", style="yellow")

    _release_lock(lock_fd)

    if clean:
        console.print(f"Sandbox '{name}' stopped. Named volumes destroyed — data unrecoverable.")
    else:
        console.print(f"Sandbox '{name}' stopped. Named volumes preserved.")


@app.command()
def attach() -> None:
    """Attach to a running sandbox."""
    _require_per_user_state_initialized()
    sandbox_ai_home = _resolve_sandbox_ai_home()
    project_dir = _resolve_project_dir()

    instance_dir, instance_id = _resolve_instance(sandbox_ai_home, project_dir)
    if instance_dir is None or instance_id is None:
        console.print("No sandbox instance found for this directory.", style="red")
        raise typer.Exit(code=1)

    config = _load_config(instance_dir)
    name = config.instance.name
    host_user, auth = _resolve_host_config(project_dir, config)

    # Warm check — reject if cold
    if not _warm_check(instance_dir, name, host_user, auth):
        console.print(f"Sandbox '{name}' is not running. Use 'sandbox start' to launch.")
        raise typer.Exit(code=1)

    # Direct handover — no hydration, no credentials, no locking
    _phase_handover(name, host_user, auth=auth)


@app.command()
def destroy(force: bool = False) -> None:
    """Permanently destroy a sandbox instance."""
    _require_per_user_state_initialized()
    sandbox_ai_home = _resolve_sandbox_ai_home()
    project_dir = _resolve_project_dir()

    instance_dir, instance_id = _resolve_instance(sandbox_ai_home, project_dir)
    if instance_dir is None or instance_id is None:
        console.print("No sandbox instance found for this directory.", style="red")
        raise typer.Exit(code=1)

    # Prefix guard — before anything else
    sandboxes_prefix = os.path.join(sandbox_ai_home, "sandboxes")
    if not instance_dir.startswith(sandboxes_prefix):
        console.print(
            "[FATAL] Instance directory path fails prefix guard. Aborting.",
            style="red bold",
        )
        raise typer.Exit(code=1)

    config = _load_config(instance_dir)
    name = config.instance.name
    host_user, auth = _resolve_host_config(project_dir, config)

    # Phase 0: Confirmation
    if not force:
        console.print(f"WARNING: This permanently deletes sandbox '{name}' and all its state.")
        console.print(f"         Your project at {config.instance.user_project_root} is NOT affected.")
        typed_name = typer.prompt("Type the sandbox name to confirm")
        if typed_name != name:
            console.print("Aborted.")
            return

    # Phase 1: Locking
    lock_fd = _acquire_state_lock(instance_dir)

    try:
        # Phase 2: Container and volume teardown — fault-isolated (D12)
        try:
            _compose_down(instance_dir, name, host_user, config, volumes=True, auth=auth)
        except SandboxExecutionError as e:
            console.print(f"⚠ Compose teardown warning: {e}", style="yellow")

        # Phase 3: ACL revocation — fault-isolated (D5)
        acl_warnings = _revoke_acls(instance_dir, host_user)
        for w in acl_warnings:
            console.print(f"⚠ {w}", style="yellow")

        # Phase 4: Directory removal — fault-isolated (D12)
        # FileNotFoundError silenced (idempotent); PermissionError propagates
        try:
            shutil.rmtree(instance_dir)
        except FileNotFoundError:
            pass

        # Phase 5: State cleanup — IPAM — fault-isolated (D12)
        try:
            ledger = IPAMLedger()
            ledger.release(instance_id)
        except Exception as e:
            console.print(f"⚠ IPAM release warning: {e}", style="yellow")

        # Phase 6: State cleanup — Registry — fault-isolated (D12)
        try:
            registry = InstanceRegistry()
            registry.remove(project_dir)
        except Exception as e:
            console.print(f"⚠ Registry cleanup warning: {e}", style="yellow")

    finally:
        # Close lock fd — safe after rmtree: kernel keeps inode alive while fd is open
        _release_lock(lock_fd)

    console.print(f"Sandbox '{name}' permanently destroyed. IPAM slot freed for reuse.")


@app.command()
def doctor(
    user: str | None = typer.Option(None, "--user", help="Unprivileged user to validate"),
    machinectl_auth: str | None = typer.Option(
        None, "--machinectl-auth", help="machinectl auth mode: 'sudo' or 'polkit'"
    ),
) -> None:
    """Run host readiness diagnostics."""
    project_config: HostConfig | None = None
    try:
        project_config = HostConfig.from_toml()
    except FileNotFoundError:
        pass

    if user is None:
        if project_config is None:
            console.print(
                "No user specified. Create sandbox-ai.toml with [host].docker_unprivileged_user or pass --user.",
                style="red",
            )
            raise typer.Exit(code=1)
        resolved_user = project_config.host.docker_unprivileged_user
    else:
        resolved_user = user

    if machinectl_auth is not None:
        try:
            resolved_auth = MachinectlAuth(machinectl_auth)
        except ValueError:
            console.print(
                f"Invalid --machinectl-auth value '{machinectl_auth}'. Expected 'sudo' or 'polkit'.",
                style="red",
            )
            raise typer.Exit(code=1) from None
    elif project_config is not None:
        resolved_auth = project_config.host.machinectl_authentication
    else:
        resolved_auth = MachinectlAuth.SUDO

    console.print(f"Per-user home: {sandbox_ai_user_home()}")

    distro = detect_distro()
    checks = build_check_registry(resolved_auth)
    results = run_checks(checks, resolved_user, distro)
    render_results(results, console=console)

    has_failures = any(r.status == "fail" for r in results)
    if has_failures:
        raise typer.Exit(code=1)


@app.command()
def status() -> None:
    """Show sandbox instance status and diagnostics."""
    _require_per_user_state_initialized()
    sandbox_ai_home = _resolve_sandbox_ai_home()
    project_dir = _resolve_project_dir()

    # Instance resolution
    instance_dir, instance_id = _resolve_instance(sandbox_ai_home, project_dir)
    if instance_dir is None or instance_id is None:
        console.print("No sandbox instance found for this directory.", style="red")
        raise typer.Exit(code=1)

    config = _load_config(instance_dir)
    name = config.instance.name
    host_user, auth = _resolve_host_config(project_dir, config)

    # Container status
    containers = _container_status(instance_dir, name, host_user, config, auth)
    is_running = len(containers) > 0
    has_unhealthy = any(c.health is not None and c.health.lower() in ("unhealthy", "starting") for c in containers)

    # Determine state
    if is_running and has_unhealthy:
        state_label = "⚠ degraded"
        border_color = "yellow"
    elif is_running:
        state_label = "● running"
        border_color = "green"
    else:
        state_label = "○ stopped"
        border_color = "red"

    # Instance header panel
    header_lines = [
        f"[bold]Name:[/bold]    {name}",
        f"[bold]ID:[/bold]      {instance_id}",
        f"[bold]Path:[/bold]    {config.instance.user_project_root}",
        f"[bold]User:[/bold]    {host_user}",
        f"[bold]State:[/bold]   {state_label}",
    ]
    panel = Panel(
        "\n".join(header_lines),
        title=f"Sandbox: {name}",
        border_style=border_color,
    )
    console.print(panel)

    # Container grid (running only)
    if is_running:
        # Derive static IPs for network column
        ledger = IPAMLedger()
        ip_map: dict[str, str] = {}
        try:
            slot, _is_existing = ledger.peek_next_slot(instance_id)
            if _is_existing:
                ips = derive_static_ips(slot)
                # Map service names to IPs from IPAM
                ip_map = {
                    "core": ips.get("agent_isolated_ip", ""),
                    "admin": ips.get("admin_admin_ip", ""),
                    "coredns": ips.get("coredns_dns_ip", ""),
                    "dnsdist": ips.get("dnsdist_isolated_ip", ""),
                    "db-postgres": ips.get("db_postgres_ip", ""),
                    "proxy": ips.get("proxy_core_ip", ""),
                    "mcp-firecrawl": ips.get("mcp_firecrawl_proxy_ip", ""),
                }
        except IPAMExhaustedError:
            pass

        table = Table(title="Containers")
        table.add_column("Service", style="cyan")
        table.add_column("State")
        table.add_column("Health")
        table.add_column("Network")
        table.add_column("Status")

        for c in containers:
            health_display = c.health or "—"
            if c.health and c.health.lower() == "unhealthy":
                health_display = f"[red]{c.health}[/red]"
            elif c.health and c.health.lower() == "healthy":
                health_display = f"[green]{c.health}[/green]"
            network = ip_map.get(c.service, "")
            table.add_row(c.service, c.state, health_display, network, c.status)

        console.print(table)

    # IPAM display
    ledger = IPAMLedger()
    try:
        slot, _is_existing = ledger.peek_next_slot(instance_id)
        if _is_existing:
            isolated, core_proxy, dns, admin, admin_proxy, egress, ipc = derive_subnets(slot)
            console.print(f"\n[bold]IPAM[/bold] slot {slot}")
            console.print(f"  Isolated:    {isolated}")
            console.print(f"  Core Proxy:  {core_proxy}")
            console.print(f"  DNS:         {dns}")
            console.print(f"  Admin:       {admin}")
            console.print(f"  Admin Proxy: {admin_proxy}")
            console.print(f"  Egress:      {egress}")
            console.print(f"  IPC:         {ipc}")
    except IPAMExhaustedError:
        pass

    # Config completeness warnings
    env_path = os.path.join(instance_dir, ".sandbox.env")
    if os.path.exists(env_path):
        missing = _check_secrets(env_path, config)
        if missing:
            console.print("\n[yellow bold]Warnings[/yellow bold]")
            for secret in missing:
                console.print(f"  ⊘ Missing secret: {secret}", style="yellow")


if __name__ == "__main__":
    app()
