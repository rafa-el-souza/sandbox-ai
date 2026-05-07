"""Sandbox CLI orchestrator — full lifecycle implementation.

Commands: init, start, stop, attach, destroy, doctor, status.
All Docker operations cross the dev/sandbox privilege boundary via machinectl.
"""

from __future__ import annotations

import errno
import fcntl
import json as _json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from pathlib import Path
from core.compose import compose_project_name
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
from core.helper_container import helper_chown_files, helper_mkdir_chown_dirs
from core.host_config import (
    HostConfig,
    HostSettings,
    MachinectlAuth,
    WorkspaceBridgeGroupMissingError,
    ensure_per_user_state,
    host_gid_for_in_container,
    host_id_for_in_container,
    machinectl_cmd,
    sandbox_ai_home,
    state_lock_path,
    workspace_bridge_gid,
)
from core.hydration import (
    InstanceConfig,
    build_jinja_context,
    render_templates,
    validate_templates,
)
from core.ipam import IPAMExhaustedError, IPAMLedger, derive_static_ips, derive_subnets
from core.locks import is_backup_lock_held
from core.registry import InstanceRegistry
from core.scaffold import (
    WorkspaceSpec,
    _detect_git_config,
    apply_default_acls,
    create_env_file,
    create_instance_dirs,
    ensure_registry_seed,
    mutate_workspaces,
    prompt_secrets,
    write_initialized_sentinel,
    write_sandbox_toml,
)
from core.walker import BoundaryPathError as WalkerBoundaryPathError
from core.walker import walk_ancestors
from core.workspace_backups import BackupError, create_backup
from core.workspace_copy import copy_workspace
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

app = typer.Typer()
workspace_app = typer.Typer(help="Workspace lifecycle commands")
app.add_typer(workspace_app, name="workspace")
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


_NAME_REGEX = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_RESERVED_NAMES: frozenset[str] = frozenset(
    {
        # Orchestrator-internal / spec-reserved
        "_backups",
        "default",
        "all",
        "none",
        "system",
        # Subnet names from the IPAM septuple
        "isolated",
        "core_proxy",
        "dns",
        "admin",
        "admin_proxy",
        "egress",
        "ipc",
    }
)
_INSTANCE_NAME_MAX = 30
_WORKSPACE_NAME_MAX = 32


def _validate_name(name: str, *, kind: str, max_len: int) -> None:
    """Apply the instance/workspace name regex + length + reserved-name rules.

    Raises:
        typer.BadParameter: ``name`` violates the rules; the message names the
            specific failure mode so the user can correct it.
    """
    if not name:
        raise typer.BadParameter(f"{kind} name must not be empty")
    if len(name) > max_len:
        raise typer.BadParameter(f"{kind} name {name!r} exceeds {max_len}-character cap")
    if name.startswith(("-", "_")):
        raise typer.BadParameter(f"{kind} name {name!r} must not start with '-' or '_'")
    if not _NAME_REGEX.match(name):
        raise typer.BadParameter(f"{kind} name {name!r} contains invalid characters; use [a-z0-9_-]")
    if name in _RESERVED_NAMES:
        raise typer.BadParameter(f"{kind} name {name!r} is reserved")


def _parse_workspace_flags(
    inst: str,
    user_home: Path,
    copy_specs: list[str],
    empty_specs: list[str],
) -> list[WorkspaceSpec]:
    """Resolve --copy/--empty multi-flags into a list of :class:`WorkspaceSpec`.

    Defaults to a single empty workspace named ``main`` when both lists are
    empty. Raises :class:`typer.BadParameter` on duplicate names, malformed
    ``--copy NAME=PATH`` values, or invalid workspace names.
    """
    if not copy_specs and not empty_specs:
        empty_specs = ["main"]

    specs: list[WorkspaceSpec] = []
    seen: set[str] = set()
    workspaces_root = user_home / "workspaces" / inst

    for raw in copy_specs:
        if "=" not in raw:
            raise typer.BadParameter(f"--copy value {raw!r} must be NAME=PATH")
        name, _, src = raw.partition("=")
        if not name or not src:
            raise typer.BadParameter(f"--copy value {raw!r} requires non-empty NAME and PATH")
        _validate_name(name, kind="workspace", max_len=_WORKSPACE_NAME_MAX)
        if name in seen:
            raise typer.BadParameter(f"workspace name {name!r} specified more than once")
        seen.add(name)
        specs.append(
            WorkspaceSpec(
                name=name,
                bootstrap_mode="copy",
                source=os.path.abspath(src),
                path=str(workspaces_root / name),
            )
        )

    for name in empty_specs:
        _validate_name(name, kind="workspace", max_len=_WORKSPACE_NAME_MAX)
        if name in seen:
            raise typer.BadParameter(f"workspace name {name!r} specified more than once")
        seen.add(name)
        specs.append(
            WorkspaceSpec(
                name=name,
                bootstrap_mode="empty",
                source=None,
                path=str(workspaces_root / name),
            )
        )

    return specs


def _preflight_workspace_source(source: str, *, inst: str, user_home: Path) -> None:
    """Validate a ``--copy`` source path before invoking rsync.

    Checks: realpath resolution, source existence, source readability, walker
    boundary list, cycle prevention (source must not be inside the orchestrator
    workspaces tree for this instance), 5GB size warning.
    """
    realpath = os.path.realpath(source)
    if not os.path.exists(realpath):
        raise typer.BadParameter(f"--copy source {source!r} does not exist")
    if not os.access(realpath, os.R_OK):
        raise typer.BadParameter(f"--copy source {source!r} is not readable")
    try:
        walk_ancestors(realpath)
    except WalkerBoundaryPathError as exc:
        raise typer.BadParameter(f"--copy source {source!r} is in the walker boundary list") from exc
    instance_workspaces = str((user_home / "workspaces" / inst).resolve())
    if realpath.startswith(instance_workspaces + os.sep) or realpath == instance_workspaces:
        raise typer.BadParameter(f"--copy source {source!r} is inside the destination workspaces tree (cycle)")
    total = sum(
        os.path.getsize(os.path.join(dp, f))
        for dp, _dn, fns in os.walk(realpath, followlinks=False)
        for f in fns
        if not os.path.islink(os.path.join(dp, f))
    )
    if total > 5 * 1024 * 1024 * 1024:
        console.print(
            f"⚠ --copy source {source!r} is larger than 5 GB ({total // (1024 * 1024)} MB); rsync will take a while.",
            style="yellow",
        )


def _lookup_instance_or_exit(inst: str) -> str:
    """Look up ``inst`` in the registry; exit 1 with guidance when absent."""
    entry = InstanceRegistry().get(inst)
    if entry is None:
        console.print(
            f"No sandbox instance named {inst!r}. Run `sandbox init {inst}` first.",
            style="red",
        )
        raise typer.Exit(code=1)
    return entry.instance_dir


def _load_config(instance_dir: str) -> InstanceConfig:
    """Parse sandbox.toml from instance directory."""
    toml_path = os.path.join(instance_dir, "sandbox.toml")
    return InstanceConfig.from_toml(toml_path)


# ─── Warm state check ───────────────────────────────────────────────────────


def _container_status(
    instance_dir: str,
    project_name: str,
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
                    f"TERM=dumb NO_COLOR=1 BUILDKIT_PROGRESS=plain COMPOSE_PROJECT_NAME={project_name} "
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


def _warm_check(
    instance_dir: str,
    project_name: str,
    host_user: str,
    auth: MachinectlAuth = MachinectlAuth.SUDO,
) -> bool:
    """Check if containers are already running. Returns True if warm.

    Delegates to _container_status (D-3) — returns True if any containers exist.
    """
    compose_file = os.path.join(instance_dir, "docker", "compose.yml")
    if not os.path.exists(compose_file):
        return False

    config = _load_config(instance_dir)
    return bool(_container_status(instance_dir, project_name, host_user, config, auth))


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


def _phase_ipam(inst: str) -> int:
    """Phase 2: IPAM allocation. Returns base_index."""
    ledger = IPAMLedger()
    return ledger.allocate(inst)


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


def _phase_hydrate(
    config: InstanceConfig,
    base_index: int,
    proxy_password: str,
    instance_dir: str,
    host: HostSettings,
) -> None:
    """Phase 4: Pydantic + Jinja2 hydration pipeline."""
    context = build_jinja_context(config, base_index, proxy_password, instance_dir, host=host)
    render_templates(
        context,
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


def _acl_grant_plan(
    instance_dir: str,
    host_user: str,
    workspace_paths: list[str] | None = None,
    dev_user: str | None = None,
) -> list[tuple[list[str], str]]:
    """Build the ACL grant plan — single source of truth for Phase 5 and dry-run (D4).

    Returns a list of (setfacl_args, description) tuples:
    - Ancestors: ``--x`` (traverse only)
    - Instance root: ``r-x``
    - docker/: ``rX`` recursive
    - config/: ``rX`` recursive
    - .sandbox.env: ``r``
    - secrets/: dir-level ``rX`` traverse
    - workspace named-ACL: effective ``rwx`` plus default with named entry on user_project_root

    Cache/log Option-B grants are intentionally absent — replaced by the
    helper-mkdir+chown phase (acl-ownership-recipes Decision 1).
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

    # secrets/ — dir-level traverse only (per-file ownership handled by helper-cp+chown)
    secrets_dir = os.path.join(instance_dir, "secrets/")
    plan.append(
        (
            ["setfacl", "-m", f"u:{host_user}:rX", secrets_dir],
            f"secrets dir traverse: {secrets_dir}",
        )
    )

    # Workspace named-ACL — granted at start, revoked at stop. Provides the
    # rootless Docker daemon traverse + r/w access to the gofer-mounted /workspace.
    # Persistent shared-group state (chgrp/chmod/setgid + persistent default ACL
    # entries) is applied separately by _phase_workspace_shared_group.
    # Per-workspace fan-out with execution-side ancestor dedup.
    if workspace_paths:
        seen_ancestors: set[str] = set()
        for ws_path in workspace_paths:
            for ancestor in _compute_workspace_ancestors(ws_path):
                if ancestor in seen_ancestors:
                    continue
                seen_ancestors.add(ancestor)
                plan.append(
                    (
                        ["setfacl", "-m", f"u:{host_user}:--x", ancestor],
                        f"workspace ancestor traverse: {ancestor}",
                    )
                )
            plan.append(
                (
                    ["setfacl", "-m", f"u:{host_user}:rwx", ws_path],
                    f"workspace named-ACL: {ws_path}",
                )
            )
            default_entry = f"u::rwx,g::rwx,o::---,m::rwx,u:{host_user}:rwx"
            if dev_user:
                default_entry += f",u:{dev_user}:rwx"
            plan.append(
                (
                    ["setfacl", "-d", "-m", default_entry, ws_path],
                    f"workspace default ACL: {ws_path}",
                )
            )

    return plan


def _compute_workspace_ancestors(workspace_path: str) -> list[str]:
    """Walk ancestors of ``workspace_path`` owned by the current uid.

    Returns shallowest-first; excludes ``/`` and ``workspace_path`` itself.
    """
    current_uid = os.getuid()
    abs_path = os.path.abspath(workspace_path)
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
    ancestors.reverse()
    return ancestors


def _acl_revoke_plan(
    instance_dir: str,
    host_user: str,
    workspace_paths: list[str] | None = None,
) -> list[tuple[list[str], str]]:
    """Build the ACL revoke plan — intentionally asymmetric with grant plan (D4).

    Ancestors are NOT revoked (D3 — grant-only model). Returns a list of
    (setfacl_args, description) tuples for: instance root, docker/ (recursive),
    config/ (recursive), .sandbox.env, secrets/ dir, and the workspace
    named-ACL (effective + default entry portion).

    Cache/log entries are intentionally absent post-change-4 — there is no
    named ACL on those paths to revoke.
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

    # secrets/ dir-level traverse
    secrets_dir = os.path.join(instance_dir, "secrets/")
    plan.append(
        (
            ["setfacl", "-x", f"u:{host_user}", secrets_dir],
            f"secrets dir traverse: {secrets_dir}",
        )
    )

    # Workspace named-ACL — both effective and default-entry revocation.
    # Persistent shared-group state (chgrp/chmod/setgid + g::rwx, u:dev:rwx
    # default) is left intact (Decision 4). Per-workspace fan-out.
    if workspace_paths:
        for ws_path in workspace_paths:
            plan.append(
                (
                    ["setfacl", "-x", f"u:{host_user}", ws_path],
                    f"workspace named-ACL: {ws_path}",
                )
            )
            plan.append(
                (
                    ["setfacl", "-d", "-x", f"u:{host_user}", ws_path],
                    f"workspace default named entry: {ws_path}",
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


# ─── Per-class consumer mapping (orchestrator-volumes Decision 1) ───────────
# Each entry is (parent_relative_to_instance, files, in_container_consumer_uid, mode).
# The helper-cp+chown phase chowns each group's files to (host_id_for_in_container(uid), 0).

RO_FILE_RECIPES: tuple[tuple[str, tuple[str, ...], int, int], ...] = (
    # CoreDNS rendered config
    ("config/coredns", ("Corefile",), 65532, 0o640),
    # dnsdist rendered config
    ("config/dnsdist", ("dnsdist.conf",), 953, 0o640),
    # Squid proxy ro files (post-drop worker uid)
    (
        "config/proxy",
        ("squid.conf", "allowed_domains.txt", "read_only_domains.txt", "ERR_SANDBOX_403", ".htpasswd"),
        13,
        0o640,
    ),
    # Agent (core) dotfiles + sshd config
    (
        "config/core",
        (".bashrc", ".npmrc", ".gitconfig", "CLAUDE.md", "sshd_config"),
        1000,
        0o640,
    ),
    # Human (admin) dotfiles + statusline assets
    (
        "config/admin",
        (".zshrc", ".tmux.conf", ".gitconfig", "gitmux.conf", "starship.toml"),
        1000,
        0o640,
    ),
    # Secrets — core consumer (agent) — mode 0600
    ("secrets", ("authorized_keys", "ipc_host_key"), 1000, 0o600),
    # Secrets — admin consumer (human) — mode 0600
    ("secrets", ("ipc_known_hosts", "ipc_ssh_key"), 1000, 0o600),
)
"""Single source of truth for the helper-cp+chown phase and dry-run preview."""


CACHE_LOG_LEAVES_BY_PARENT: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cache/core", (".claude",)),
    ("cache/admin", ("tmux_resurrect",)),
    ("log", ("core", "admin")),
)
"""Per-parent grouping of cache/log leaves consumed by helper-mkdir+chown phase.

Single source of truth for both execution (_phase_helper_mkdir_chown_cache_log)
and dry-run preview (_helper_mkdir_chown_plan).
"""


def _helper_mkdir_chown_plan(instance_dir: str, host_user: str) -> list[tuple[str, tuple[str, ...], int, int]]:
    """Return ``[(parent_abs, leaves, owner_uid, owner_gid), ...]`` for cache/log.

    ``owner_uid``/``owner_gid`` map in-container uid/gid 1000 (agent / human)
    to their host subuid/subgid via :func:`core.host_config.host_id_for_in_container`.
    """
    owner_uid = host_id_for_in_container(1000, host_user)
    owner_gid = host_gid_for_in_container(1000, host_user)
    return [
        (os.path.join(instance_dir, parent), leaves, owner_uid, owner_gid)
        for parent, leaves in CACHE_LOG_LEAVES_BY_PARENT
    ]


def _helper_cp_chown_plan(instance_dir: str, host_user: str) -> list[tuple[str, tuple[str, ...], int, int, int]]:
    """Return ``[(parent_abs, files, owner_uid, owner_gid, mode), ...]`` for ro files.

    Owner uid is mapped via :func:`core.host_config.host_id_for_in_container`;
    owner gid is always 0 (in-container root) so root-running parsers can read
    the file before dropping privileges.
    """
    return [
        (
            os.path.join(instance_dir, parent),
            files,
            host_id_for_in_container(consumer_uid, host_user),
            0,
            mode,
        )
        for parent, files, consumer_uid, mode in RO_FILE_RECIPES
    ]


def _workspace_needs_recursive_setup(workspace: str, bridge_gid: int) -> bool:
    """Detect drift on the workspace root: setgid bit AND group ownership."""
    import stat as _stat

    try:
        st = os.stat(workspace)
    except OSError:
        # Workspace inaccessible; treat as needing recursive setup so the
        # caller fails loudly with the underlying chgrp/setfacl error.
        return True
    has_setgid = bool(st.st_mode & _stat.S_ISGID)
    has_correct_group = st.st_gid == bridge_gid
    return not (has_setgid and has_correct_group)


def _workspace_shared_group_recursive(workspace: str, bridge_gid: int) -> tuple[int, list[str]]:
    """Apply chgrp + chmod recursively, best-effort.

    Returns (failure_count, sample_failure_paths). Per-file failures (typically
    EPERM on non-dev-owned files) are collected and reported in aggregate per
    Decision 17 — the orchestrator does not escalate via sudo.
    """
    failures: list[str] = []
    sample_limit = 5
    for root, dirs, files in os.walk(workspace):
        for name in (*dirs, *files):
            path = os.path.join(root, name)
            try:
                os.chown(path, -1, bridge_gid, follow_symlinks=False)
            except OSError:
                if len(failures) < sample_limit:
                    failures.append(path)
                continue
            try:
                if name in dirs:
                    os.chmod(path, 0o2770)
                else:
                    if not os.path.islink(path):
                        os.chmod(path, 0o0660)
            except OSError:
                if len(failures) < sample_limit:
                    failures.append(path)
    return len(failures), failures


def _workspace_shared_group_plan(
    workspace_path: str, bridge_gid: int, dev_user: str | None, host_user: str
) -> list[tuple[str, str]]:
    """Return the (operation_summary, target) list for the workspace shared-group recipe.

    Used by both execution and dry-run preview.
    """
    default_entry = f"u::rwx,g::rwx,o::---,m::rwx,u:{host_user}:rwx"
    if dev_user:
        default_entry += f",u:{dev_user}:rwx"
    return [
        (f"chgrp {bridge_gid}", workspace_path),
        ("chmod 2770", workspace_path),
        (f"setfacl -m u:{host_user}:rwx", workspace_path),
        (f"setfacl -d -m {default_entry}", workspace_path),
    ]


def _phase_workspace_shared_group(
    workspace_path: str,
    host: HostSettings,
    dev_user: str | None = None,
) -> None:
    """Phase 5e: chgrp + chmod 2770 + setfacl on a single workspace tree.

    Drift-detects the workspace root: when not yet at setgid + bridge_gid,
    runs the recursive recipe (best-effort, per-file failures aggregated and
    reported, orchestrator never escalates to sudo per Decision 17). Then
    applies idempotent root-level chgrp/chmod/setfacl on every start.

    Raises:
        WorkspaceBridgeGroupMissingError: if the bridge group is missing.
    """
    bridge_gid = workspace_bridge_gid(host)
    host_user = host.docker_unprivileged_user

    if _workspace_needs_recursive_setup(workspace_path, bridge_gid):
        failure_count, sample_paths = _workspace_shared_group_recursive(workspace_path, bridge_gid)
        if failure_count:
            sample = ", ".join(sample_paths)
            console.print(
                f"⚠ Workspace shared-group: {failure_count} file(s) skipped (non-dev-owned). Sample: {sample}",
                style="yellow",
            )

    # Steady-state idempotent root setup
    try:
        os.chown(workspace_path, -1, bridge_gid, follow_symlinks=False)
        os.chmod(workspace_path, 0o2770)
    except OSError as exc:
        raise SandboxExecutionError(f"Workspace root chgrp/chmod failed for {workspace_path}: {exc}") from exc

    default_entry = f"u::rwx,g::rwx,o::---,m::rwx,u:{host_user}:rwx"
    if dev_user:
        default_entry += f",u:{dev_user}:rwx"
    for args, label in (
        (["setfacl", "-m", f"u:{host_user}:rwx", workspace_path], "effective"),
        (["setfacl", "-d", "-m", default_entry, workspace_path], "default"),
    ):
        try:
            subprocess.run(args, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip() if exc.stderr else f"exit {exc.returncode}"
            raise SandboxExecutionError(
                f"Workspace shared-group {label} ACL failed for {workspace_path}: {stderr}"
            ) from exc


def _phase_helper_cp_chown_ro_files(
    instance_dir: str,
    host_user: str,
    auth: MachinectlAuth,
) -> None:
    """Phase 5d: helper-cp + chown for ro single-file mounts.

    Replaces today's ``_phase_credential_ownership`` — IPC SSH secrets are now
    handled by the standard recipe (``secrets/`` group with consumer uid 1000,
    mode 0600). One helper invocation per (parent, consumer_uid, mode) group.
    """
    for parent_abs, files, owner_uid, owner_gid, mode in _helper_cp_chown_plan(instance_dir, host_user):
        helper_chown_files(host_user, parent_abs, files, owner_uid, owner_gid, mode, auth)


def _phase_helper_mkdir_chown_cache_log(
    instance_dir: str,
    host_user: str,
    auth: MachinectlAuth,
    dev_user: str | None = None,
) -> None:
    """Phase 5c: helper-mkdir + chown for cache/log leaves.

    For each cache/log parent: set the parent's default ACL so dev can read
    agent-created files inside the leaf, then invoke the helper container to
    ``mkdir -p`` and ``chown`` each leaf to the consumer subuid (in-container
    uid 1000). Idempotent — re-runs are no-ops in the steady state.
    """
    default_entry = "u::rwx,g::---,o::---,m::rwx"
    if dev_user:
        default_entry += f",u:{dev_user}:rwx"

    for parent_abs, leaves, owner_uid, owner_gid in _helper_mkdir_chown_plan(instance_dir, host_user):
        try:
            subprocess.run(
                ["setfacl", "-d", "-m", default_entry, parent_abs],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip() if exc.stderr else f"exit {exc.returncode}"
            raise SandboxExecutionError(f"Default ACL setup failed for {parent_abs}: {stderr}") from exc
        helper_mkdir_chown_dirs(host_user, parent_abs, leaves, owner_uid, owner_gid, auth)


def _phase_acl_grant(
    instance_dir: str,
    host_user: str,
    workspace_paths: list[str] | None = None,
    dev_user: str | None = None,
) -> None:
    """Phase 5: Grant sandbox user ACLs via _acl_grant_plan() (Pattern A).

    Each setfacl call runs as direct subprocess.run (NOT via Executor.run —
    sentinel injection would corrupt the setfacl command, per I-1).
    CalledProcessError is wrapped in SandboxExecutionError (D6).
    """
    for acl_cmd, description in _acl_grant_plan(instance_dir, host_user, workspace_paths, dev_user):
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
    project_name: str,
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
        f"COMPOSE_PROJECT_NAME={project_name} docker compose {files_str} "
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
    project_name: str,
    host_user: str,
    warmup_prompt: str = "",
    auth: MachinectlAuth = MachinectlAuth.SUDO,
    cwd_workspace: str | None = None,
) -> None:
    """Phase 7: PTY handover — docker exec -it via machinectl.

    ``cwd_workspace`` is the runtime cwd selector: when supplied, ``-w
    /workspaces/<ws>`` sets the in-container cwd at exec time per cli-attach.
    ``attach`` always passes a value (resolved per the N=1 default / N>1 list
    rule). ``start`` intentionally passes ``None`` — cli-start does NOT mandate
    a runtime cwd, so the admin shell inherits whatever the entrypoint sets.
    The Dockerfile WORKDIR is intentionally ignored for runtime cwd.
    """
    executor = Executor()
    exec_args = ["exec"]
    if warmup_prompt:
        exec_args.extend(["-e", f"SANDBOX_WARMUP_PROMPT={warmup_prompt}"])
    if cwd_workspace is not None:
        exec_args.extend(["-w", f"/workspaces/{cwd_workspace}"])
    exec_args.extend(["-it", f"{project_name}-admin-1", "zsh"])

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
    project_name: str,
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
        f"COMPOSE_PROJECT_NAME={project_name} docker compose {files_str} "
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


def _revoke_acls(instance_dir: str, host_user: str, workspace_paths: list[str] | None = None) -> list[str]:
    """Revoke sandbox user's ACL entries — fault-isolated, best-effort (D5).

    Iterates _acl_revoke_plan(). Uses check=False; failures are collected
    as warning strings, not raised. Returns list of warning messages.
    """
    warnings: list[str] = []
    for acl_cmd, description in _acl_revoke_plan(instance_dir, host_user, workspace_paths):
        try:
            result = subprocess.run(acl_cmd, check=False, capture_output=True, text=True)
            if result.returncode != 0:
                detail = result.stderr.strip() if result.stderr else f"exit {result.returncode}"
                warnings.append(f"ACL revoke warning for {description}: {detail}")
        except OSError as e:
            warnings.append(f"ACL revoke warning for {description}: {e}")
    return warnings


# ─── Dry-Run Pipeline ───────────────────────────────────────────────────────


def _dry_run_pipeline(inst: str) -> None:
    """Simulate the full start pipeline without side effects.

    Validates config parsing, IPAM allocation, template rendering, secret
    completeness, and previews the subprocess commands that would execute.
    """
    console.print("\n[bold]Dry-run: sandbox start[/bold]\n")

    instance_dir = _lookup_instance_or_exit(inst)
    console.print(f"  Instance: [green]{inst}[/green] (existing)")
    config = _load_config(instance_dir)
    host_settings = _resolve_host_settings()
    host_user = host_settings.docker_unprivileged_user
    auth = host_settings.machinectl_authentication

    project_name = compose_project_name(inst)

    # ── IPAM preview ─────────────────────────────────────────────────────
    ledger = IPAMLedger()
    try:
        slot, is_existing = ledger.peek_next_slot(inst)
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
    try:
        context = build_jinja_context(config, slot, "DRY_RUN_PASSWORD", instance_dir, host=host_settings)
    except WorkspaceBridgeGroupMissingError as exc:
        console.print(f"\n  [red]{exc}[/red]")
        console.print("  [red]Run `sandbox doctor` for setup commands.[/red]")
        raise typer.Exit(code=1) from None
    validated, errors = validate_templates(
        context,
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
    dev_user = os.environ.get("USER")
    workspace_paths = [ws.path for _, ws in sorted(config.workspaces.items())]
    for acl_cmd, description in _acl_grant_plan(instance_dir, host_user, workspace_paths, dev_user):
        console.print(f"    $ {' '.join(acl_cmd)}  # {description}", style="dim")

    # Helper-mkdir+chown for cache/log — single source of truth via plan function
    try:
        for parent_abs, leaves, owner_uid, owner_gid in _helper_mkdir_chown_plan(instance_dir, host_user):
            leaves_str = ", ".join(leaves)
            console.print(
                f"    helper-mkdir+chown {parent_abs}/{{{leaves_str}}} → {owner_uid}:{owner_gid}",
                style="dim",
            )
        for parent_abs, files, owner_uid, owner_gid, mode in _helper_cp_chown_plan(instance_dir, host_user):
            files_str = ", ".join(files)
            console.print(
                f"    helper-cp+chown {parent_abs}/{{{files_str}}} → {owner_uid}:{owner_gid} {mode:o}",
                style="dim",
            )
    except SandboxExecutionError as exc:
        console.print(f"    [red]helper-mkdir plan unavailable: {exc}[/red]")

    # Workspace shared-group plan — fanned out per workspace
    try:
        bridge_gid = workspace_bridge_gid(host_settings)
        for ws_path in workspace_paths:
            for op, target in _workspace_shared_group_plan(ws_path, bridge_gid, os.environ.get("USER"), host_user):
                console.print(f"    workspace: {op} {target}", style="dim")
    except SandboxExecutionError as exc:
        console.print(f"    [red]workspace shared-group plan unavailable: {exc}[/red]")

    # Compose up — match actual _phase_compose_up command
    env_file = os.path.join(instance_dir, ".sandbox.env")
    machinectl_prefix = " ".join(machinectl_cmd(host_user, auth))
    compose_cmd = (
        f"{machinectl_prefix} /bin/bash -c "
        f"'TERM=dumb NO_COLOR=1 BUILDKIT_PROGRESS=plain "
        f"COMPOSE_PROJECT_NAME={project_name} docker compose {files_str} "
        f"--ansi never --env-file {env_file} up -d --build --wait'"
    )
    console.print(f"    $ {compose_cmd}", style="dim")

    # Handover
    handover_cmd = f"{machinectl_prefix} /usr/bin/docker exec -it {project_name}-admin-1 zsh"
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


def _resolve_host_config() -> tuple[str, MachinectlAuth]:
    """Resolve host_user and auth from per-host ``sandbox-ai.toml``.

    Post-init commands SHALL fail when host config is absent — the field
    no longer exists on the per-instance ``SandboxInstanceSection``.
    """
    settings = _resolve_host_settings()
    return settings.docker_unprivileged_user, settings.machinectl_authentication


def _resolve_host_settings() -> HostSettings:
    """Resolve the full ``HostSettings`` from per-host ``sandbox-ai.toml``."""
    try:
        project_config = HostConfig.from_toml()
    except FileNotFoundError as exc:
        console.print(str(exc), style="red")
        raise typer.Exit(code=1) from None
    return project_config.host


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
    home = sandbox_ai_home()
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


_COPY_FLAG = typer.Option([], "--copy", help="Workspace from a copied tree: NAME=PATH (repeatable)")
_EMPTY_FLAG = typer.Option([], "--empty", help="Empty workspace: NAME (repeatable)")


@app.command()
def init(
    inst: str = typer.Argument(..., help="Instance name (1-30 chars, [a-z0-9_-])"),
    copy: list[str] = _COPY_FLAG,
    empty: list[str] = _EMPTY_FLAG,
    machinectl_auth: str | None = typer.Option(
        None, "--machinectl-auth", help="machinectl auth mode: 'sudo' or 'polkit'"
    ),
    git_user: str = typer.Option("", "--git-user", help="Git user.name (auto-detected if omitted)"),
    git_email: str = typer.Option("", "--git-email", help="Git user.email (auto-detected if omitted)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview scaffold without writing"),
) -> None:
    """Initialize a new sandbox instance with one or more workspaces."""
    _validate_name(inst, kind="instance", max_len=_INSTANCE_NAME_MAX)

    # Per-user tree creation (idempotent, mode 0700)
    user_home = sandbox_ai_home()
    ensure_per_user_state(user_home)

    # Legacy CWD-local file detection (advisory) — uses CWD only as a warning trigger.
    _warn_legacy_cwd_files(os.path.abspath(os.getcwd()), user_home)

    # Resolve workspace specs (defaults to single empty `main` when no flags supplied).
    workspace_specs = _parse_workspace_flags(inst, user_home, copy, empty)

    # Re-init guard (D-6) — registry uniqueness on instance name (per-user).
    if InstanceRegistry().get(inst) is not None:
        console.print(
            f"Instance {inst!r} is already registered. Run `sandbox destroy {inst}` first.",
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

    # Pre-flight gates for every --copy source (boundary, exists, readable, cycle, size).
    for ws in workspace_specs:
        if ws.bootstrap_mode == "copy" and ws.source is not None:
            _preflight_workspace_source(ws.source, inst=inst, user_home=user_home)

    instance_dir = str(sandbox_ai_home() / "instances" / inst)

    if dry_run:
        console.print("\n[bold]Dry-run: sandbox init[/bold]\n")
        console.print(f"  Instance: {inst}")
        console.print(f"  Directory: {instance_dir}")
        console.print(f"  User: {resolved_user}")
        console.print(f"  Git: {git_user} <{git_email}>")
        for ws in workspace_specs:
            origin = f"copy from {ws.source}" if ws.bootstrap_mode == "copy" else "empty"
            console.print(f"  Workspace [{ws.name}]: {origin} → {ws.path}")
        console.print("\n  [green bold]Dry-run complete — no files written[/green bold]\n")
        return

    # S1: Directory tree (instance + per-workspace orchestrator-owned trees).
    create_instance_dirs(instance_dir, workspace_specs)

    # S1b: Populate --copy workspaces via the rsync recipe (default-excludes,
    # safe-link refusal). --empty workspaces start as bare 0700 dev:dev dirs.
    for ws in workspace_specs:
        if ws.bootstrap_mode == "copy" and ws.source is not None:
            copy_workspace(ws.source, ws.path)

    # S2: sandbox.toml
    write_sandbox_toml(
        instance_dir,
        inst,
        workspace_specs,
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

    # S4: Default ACLs (Pattern B) — fanned out across the workspaces map.
    dev_user = os.environ.get("USER", "dev")
    apply_default_acls(instance_dir, [ws.path for ws in config.workspaces.values()], dev_user)

    # S5: Register — ensure registry seed exists, then register by name.
    ensure_registry_seed(user_home)
    InstanceRegistry().register(inst, instance_dir)

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

    console.print(f"Sandbox '{inst}' initialized. Run `sandbox start {inst}` to launch.")


@app.command()
def start(
    inst: str = typer.Argument(..., help="Instance name"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Simulate start without side effects"),
) -> None:
    """Start the sandbox."""
    _require_per_user_state_initialized()

    if dry_run:
        _dry_run_pipeline(inst)
        return

    # Phase 0: Instance resolution
    instance_dir = _lookup_instance_or_exit(inst)

    # Sentinel check: verify init completed
    sentinel_path = os.path.join(instance_dir, ".initialized")
    if not os.path.exists(sentinel_path):
        console.print(
            f"Instance partially initialized. Run `sandbox destroy {inst}` then `sandbox init {inst}`.",
            style="red",
        )
        raise typer.Exit(code=1)

    config = _load_config(instance_dir)
    host_settings = _resolve_host_settings()
    host_user = host_settings.docker_unprivileged_user
    auth = host_settings.machinectl_authentication
    project_name = compose_project_name(inst)

    # Operator-edited TOML guard: ``instance.name`` should match the registry
    # key (the typer arg). Divergence is harmless for compose-project-name
    # correctness — that's derived from ``inst`` per Group 7 — but produces
    # confusing operator-facing output (status panel, sandbox.toml display).
    if config.instance.name != inst:
        console.print(
            f"WARNING: sandbox.toml instance.name ({config.instance.name!r}) differs from the "
            f"registry key ({inst!r}). Edit sandbox.toml to set instance.name = {inst!r}.",
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
    if _warm_check(instance_dir, project_name, host_user, auth):
        console.print(f"Sandbox '{inst}' is already running. Use 'sandbox attach {inst} [<ws>]' to reconnect.")
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

    # Backup-lock check (after state.lock acquisition per cli-start spec).
    if is_backup_lock_held(inst):
        if lock_fd is not None:
            _release_lock(lock_fd)
        console.print(
            f"Backup in progress for {inst!r}; wait or `sandbox doctor` to inspect.",
            style="red",
        )
        raise typer.Exit(code=1)

    acl_granted = False
    try:
        # Phase 2: IPAM
        base_index = _phase_ipam(inst)
        console.print("✓ IPAM — network allocation complete")

        # Phase 3: Credentials (generation only)
        proxy_password = _phase_credentials(
            instance_dir,
            core_ipc_ip=derive_static_ips(base_index)["core_ipc_ip"],
        )
        console.print("✓ Credentials — proxy auth + SSH keypairs configured")

        # Phase 4: Hydration
        try:
            _phase_hydrate(config, base_index, proxy_password, instance_dir, host_settings)
        except WorkspaceBridgeGroupMissingError as exc:
            console.print(
                f"[FATAL] {exc}\nRun `sandbox doctor` for setup commands.",
                style="red bold",
            )
            if lock_fd is not None:
                _release_lock(lock_fd)
            raise typer.Exit(code=1) from None
        console.print("✓ Hydration — templates rendered")

        # Phase 5: ACL grants (Pattern A) — fan out across [workspaces].
        acl_granted = True  # set BEFORE Phase 5 — handles partial grants (D7)
        dev_user = os.environ.get("USER")
        ws_paths = [ws.path for _, ws in sorted(config.workspaces.items())]
        _phase_acl_grant(instance_dir, host_user, ws_paths, dev_user)
        console.print("✓ ACL — filesystem permissions granted")

        # Phase 5c: helper-mkdir+chown for cache/log leaves
        _phase_helper_mkdir_chown_cache_log(instance_dir, host_user, auth, dev_user)
        console.print("✓ Cache/log — leaves chowned to consumer subuid")

        # Phase 5d: helper-cp+chown for ro single-file mounts (replaces credential-ownership)
        _phase_helper_cp_chown_ro_files(instance_dir, host_user, auth)
        console.print("✓ Ownership — ro config files converged")

        # Phase 5e: workspace shared-group recipe — per-workspace fan-out.
        for ws_path in ws_paths:
            _phase_workspace_shared_group(ws_path, host_settings, dev_user)
        console.print("✓ Workspace — shared-group recipe applied")

        # Phase 6: Compose up (D-5 — spinner for long-running phase)
        with console.status("⟳ Compose — starting containers…"):
            _phase_compose_up(instance_dir, project_name, host_user, config, auth)
        console.print("✓ Compose — containers healthy")

    except (IPAMExhaustedError, SandboxExecutionError) as e:
        console.print(f"[FATAL] {e}", style="red bold")
        # ACL cleanup on failure (Decision 4 of acl-ownership-recipes):
        # named-ACL grants from Phase 5 are revoked here. Helper-recipe
        # mutations (subuid chowns on cache/log/ro-files; chgrp+chmod+default
        # ACL on the workspace) are NOT reverted — they're persistent state
        # that survives intermediate stop/start cycles by design.
        if acl_granted:
            acl_warnings = _revoke_acls(instance_dir, host_user, ws_paths)
            for w in acl_warnings:
                console.print(f"⚠ {w}", style="yellow")
        if lock_fd is not None:
            _release_lock(lock_fd)
        raise typer.Exit(code=1) from None

    # Phase 7: Handover — release lock first
    if lock_fd is not None:
        _release_lock(lock_fd)

    console.print("→ Handing over to admin shell")
    _phase_handover(project_name, host_user, config.instance.warmup_prompt, auth)


@app.command()
def stop(
    inst: str = typer.Argument(..., help="Instance name"),
    clean: bool = False,
) -> None:
    """Stop the sandbox."""
    _require_per_user_state_initialized()

    instance_dir = _lookup_instance_or_exit(inst)
    config = _load_config(instance_dir)
    project_name = compose_project_name(inst)
    host_user, auth = _resolve_host_config()

    # Warm check
    if not _warm_check(instance_dir, project_name, host_user, auth):
        console.print(f"Sandbox '{inst}' is not running. Nothing to stop.")
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

    # Backup-lock check (after state.lock per cli-stop spec).
    if is_backup_lock_held(inst):
        _release_lock(lock_fd)
        console.print(
            f"Backup in progress for {inst!r}; wait or `sandbox doctor` to inspect.",
            style="red",
        )
        raise typer.Exit(code=1)

    # Compose down
    _compose_down(instance_dir, project_name, host_user, config, volumes=clean, auth=auth)

    # ACL revocation (Pattern A) — fault-isolated (D5), per-workspace fan-out
    ws_paths = [ws.path for _, ws in sorted(config.workspaces.items())]
    acl_warnings = _revoke_acls(instance_dir, host_user, ws_paths)
    for w in acl_warnings:
        console.print(f"⚠ {w}", style="yellow")

    _release_lock(lock_fd)

    if clean:
        console.print(f"Sandbox '{inst}' stopped. Named volumes destroyed — data unrecoverable.")
    else:
        console.print(f"Sandbox '{inst}' stopped. Named volumes preserved.")


@app.command()
def attach(
    inst: str = typer.Argument(..., help="Instance name"),
    ws: str | None = typer.Argument(None, help="Workspace name (optional iff N=1)"),
) -> None:
    """Attach to a running sandbox."""
    _require_per_user_state_initialized()

    # Backup-lock check (cli-attach: refuse fast if held; attach holds no
    # state.lock, so the only cleanup needed on this path is the exit).
    if is_backup_lock_held(inst):
        console.print(
            f"Backup in progress for {inst!r}; wait or `sandbox doctor` to inspect.",
            style="red",
        )
        raise typer.Exit(code=1)

    instance_dir = _lookup_instance_or_exit(inst)
    config = _load_config(instance_dir)
    project_name = compose_project_name(inst)
    host_user, auth = _resolve_host_config()

    # Workspace selection per cli-attach spec.
    workspace_names = sorted(config.workspaces.keys())
    if ws is None:
        if len(workspace_names) == 1:
            ws = workspace_names[0]
        else:
            joined = ", ".join(workspace_names)
            console.print(
                f"Multiple workspaces in {inst!r}. Pick one: {joined}.",
                style="red",
            )
            raise typer.Exit(code=1)
    elif ws not in config.workspaces:
        console.print(
            f"Workspace {ws!r} not found in instance {inst!r}. Available: {', '.join(workspace_names)}.",
            style="red",
        )
        raise typer.Exit(code=1)

    # Warm check — reject if cold
    if not _warm_check(instance_dir, project_name, host_user, auth):
        console.print(f"Sandbox '{inst}' is not running. Use 'sandbox start {inst}' to launch.")
        raise typer.Exit(code=1)

    # Direct handover — no hydration, no credentials, no locking
    _phase_handover(project_name, host_user, auth=auth, cwd_workspace=ws)


@app.command()
def destroy(
    inst: str = typer.Argument(..., help="Instance name"),
    force: bool = False,
) -> None:
    """Permanently destroy a sandbox instance."""
    _require_per_user_state_initialized()

    instance_dir = _lookup_instance_or_exit(inst)

    # Prefix guard — before anything else.
    instances_prefix = str(sandbox_ai_home() / "instances") + os.sep
    if not instance_dir.startswith(instances_prefix):
        console.print(
            "[FATAL] Instance directory path fails prefix guard. Aborting.",
            style="red bold",
        )
        raise typer.Exit(code=1)

    config = _load_config(instance_dir)
    project_name = compose_project_name(inst)
    host_user, auth = _resolve_host_config()

    # Phase 0: Confirmation
    if not force:
        console.print(f"WARNING: This permanently deletes sandbox '{inst}' and all its state.")
        ws_summary = ", ".join(sorted(config.workspaces.keys()))
        console.print(f"         Workspaces affected: {ws_summary}")
        typed_name = typer.prompt("Type the sandbox name to confirm")
        if typed_name != inst:
            console.print("Aborted.")
            return

    # Phase 1: Locking
    lock_fd = _acquire_state_lock(instance_dir)

    # Backup-lock check (after state.lock per cli-destroy concurrency matrix).
    if is_backup_lock_held(inst):
        _release_lock(lock_fd)
        console.print(
            f"Backup in progress for {inst!r}; wait or `sandbox doctor` to inspect.",
            style="red",
        )
        raise typer.Exit(code=1)

    try:
        # Phase 2: Container and volume teardown — fault-isolated (D12)
        try:
            _compose_down(instance_dir, project_name, host_user, config, volumes=True, auth=auth)
        except SandboxExecutionError as e:
            console.print(f"⚠ Compose teardown warning: {e}", style="yellow")

        # Phase 3: ACL revocation — fault-isolated (D5), per-workspace fan-out
        ws_paths = [ws.path for _, ws in sorted(config.workspaces.items())]
        acl_warnings = _revoke_acls(instance_dir, host_user, ws_paths)
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
            ledger.release(inst)
        except Exception as e:
            console.print(f"⚠ IPAM release warning: {e}", style="yellow")

        # Phase 6: State cleanup — Registry — fault-isolated (D12)
        try:
            InstanceRegistry().remove(inst)
        except Exception as e:
            console.print(f"⚠ Registry cleanup warning: {e}", style="yellow")

    finally:
        # Close lock fd — safe after rmtree: kernel keeps inode alive while fd is open
        _release_lock(lock_fd)

    console.print(f"Sandbox '{inst}' permanently destroyed. IPAM slot freed for reuse.")


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

    console.print(f"Per-user home: {sandbox_ai_home()}")

    distro = detect_distro()
    checks = build_check_registry(resolved_auth)
    results = run_checks(checks, resolved_user, distro)
    render_results(results, console=console)

    has_failures = any(r.status == "fail" for r in results)
    if has_failures:
        raise typer.Exit(code=1)


def _workspace_state_label(ws_path: str, host_settings: HostSettings) -> str:
    """Return the per-cli-status state label for a single workspace path.

    `● ok`    — path exists, setgid + group ownership matches the bridge gid.
    `⚠ drift` — path exists but bridge-group state is missing or wrong.
    `✗ missing` — path does not exist on disk.
    """
    try:
        st = os.stat(ws_path)
    except FileNotFoundError:
        return "[red]✗ missing[/red]"
    try:
        expected_gid = workspace_bridge_gid(host_settings)
    except WorkspaceBridgeGroupMissingError:
        return "[yellow]⚠ drift[/yellow]"
    setgid_ok = bool(st.st_mode & 0o2000)
    group_ok = st.st_gid == expected_gid
    if setgid_ok and group_ok:
        return "[green]● ok[/green]"
    return "[yellow]⚠ drift[/yellow]"


def _workspace_du_size(ws_path: str) -> str:
    """Return a `du -sh`-style size string, or "—" when the path is unreadable."""
    try:
        result = subprocess.run(
            ["du", "-sh", ws_path],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except subprocess.CalledProcessError:
        return "—"
    except subprocess.TimeoutExpired:
        return "—"
    except FileNotFoundError:
        return "—"
    return result.stdout.split()[0] if result.stdout.strip() else "—"


def _render_status_summary() -> None:
    """Render the all-instances summary table for ``sandbox status`` (no inst)."""
    entries = InstanceRegistry().all()
    if not entries:
        console.print("No instances registered. Run `sandbox init <inst>` to create one.")
        return

    table = Table(title="Sandbox instances")
    table.add_column("Name", style="cyan")
    table.add_column("State")
    table.add_column("Workspaces", justify="right")
    table.add_column("IPAM slot", justify="right")

    ledger = IPAMLedger()
    for inst_name in sorted(entries):
        entry = entries[inst_name]
        try:
            config = _load_config(entry.instance_dir)
            ws_count = str(len(config.workspaces))
        except Exception:
            ws_count = "?"
        slot_str = "—"
        try:
            slot, is_existing = ledger.peek_next_slot(inst_name)
            if is_existing:
                slot_str = str(slot)
        except IPAMExhaustedError:
            pass
        # State is computed cheaply: "registered" only — exact running-state requires
        # daemon round-trips which the summary view does not justify.
        table.add_row(inst_name, "registered", ws_count, slot_str)

    console.print(table)


def _render_status_detailed(inst: str, *, detailed: bool) -> None:
    """Render the per-instance detailed panel + workspaces + containers."""
    instance_dir = _lookup_instance_or_exit(inst)
    config = _load_config(instance_dir)
    project_name = compose_project_name(inst)
    host_settings = _resolve_host_settings()
    host_user = host_settings.docker_unprivileged_user
    auth = host_settings.machinectl_authentication

    # Container status
    containers = _container_status(instance_dir, project_name, host_user, config, auth)
    is_running = len(containers) > 0
    has_unhealthy = any(c.health is not None and c.health.lower() in ("unhealthy", "starting") for c in containers)

    if is_running and has_unhealthy:
        state_label = "⚠ degraded"
        border_color = "yellow"
    elif is_running:
        state_label = "● running"
        border_color = "green"
    else:
        state_label = "○ stopped"
        border_color = "red"

    header_lines = [
        f"[bold]Name:[/bold]        {inst}",
        f"[bold]Dir:[/bold]         {instance_dir}",
        f"[bold]User:[/bold]        {host_user}",
        f"[bold]State:[/bold]       {state_label}",
    ]
    panel = Panel(
        "\n".join(header_lines),
        title=f"Sandbox: {inst}",
        border_style=border_color,
    )
    console.print(panel)

    # Workspaces table per cli-status spec.
    ws_table = Table(title="Workspaces")
    ws_table.add_column("Name", style="cyan")
    ws_table.add_column("Mode")
    ws_table.add_column("Path")
    ws_table.add_column("State")
    if detailed:
        ws_table.add_column("Size", justify="right")
    for ws_name, ws in sorted(config.workspaces.items()):
        row = [ws_name, str(ws.bootstrap_mode.value), ws.path, _workspace_state_label(ws.path, host_settings)]
        if detailed:
            row.append(_workspace_du_size(ws.path))
        ws_table.add_row(*row)
    console.print(ws_table)

    # Containers table (running only)
    if is_running:
        ledger = IPAMLedger()
        ip_map: dict[str, str] = {}
        try:
            slot, is_existing = ledger.peek_next_slot(inst)
            if is_existing:
                ips = derive_static_ips(slot)
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
        slot, is_existing = ledger.peek_next_slot(inst)
        if is_existing:
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


@app.command()
def status(
    inst: str | None = typer.Argument(None, help="Instance name (omit for all-instances summary)"),
    detailed: bool = typer.Option(False, "--detailed", help="Include `du -sh` per workspace"),
) -> None:
    """Show sandbox instance status and diagnostics."""
    _require_per_user_state_initialized()
    if inst is None:
        if detailed:
            console.print("--detailed requires an explicit <inst> argument.", style="red")
            raise typer.Exit(code=1)
        _render_status_summary()
        return
    _render_status_detailed(inst, detailed=detailed)


# ─── workspace subcommands ──────────────────────────────────────────────────


_WORKSPACE_COPY_FLAG = typer.Option(
    [],
    "--copy",
    help="Copy a workspace from a host path: --copy NAME=PATH",
)
_WORKSPACE_EMPTY_FLAG = typer.Option(
    [],
    "--empty",
    help="Create an empty workspace: --empty NAME",
)


def _require_instance_stopped(inst: str, instance_dir: str) -> None:
    """Refuse if any container for ``inst`` is running.

    Workspace mutations (add/remove/rename/restore) require the instance to
    be STOPPED — otherwise live bind-mounts disagree with sandbox.toml.
    """
    project_name = compose_project_name(inst)
    host_user, auth = _resolve_host_config()
    if _warm_check(instance_dir, project_name, host_user, auth):
        console.print(
            f"Instance {inst!r} must be stopped. Run `sandbox stop {inst}` first.",
            style="red",
        )
        raise typer.Exit(code=1)


def _refuse_if_backup_in_progress(inst: str) -> None:
    if is_backup_lock_held(inst):
        console.print(
            f"Backup in progress for {inst!r}; wait or `sandbox doctor` to inspect.",
            style="red",
        )
        raise typer.Exit(code=1)


@workspace_app.command("add")
def workspace_add(
    inst: str = typer.Argument(..., help="Instance name"),
    copy: list[str] = _WORKSPACE_COPY_FLAG,
    empty: list[str] = _WORKSPACE_EMPTY_FLAG,
) -> None:
    """Add one or more workspaces to a stopped instance."""
    _require_per_user_state_initialized()
    if not copy and not empty:
        console.print("Specify at least one --copy or --empty flag.", style="red")
        raise typer.Exit(code=1)

    instance_dir = _lookup_instance_or_exit(inst)
    config = _load_config(instance_dir)
    existing_names = set(config.workspaces.keys())

    # Parse flags (will not autofill `main` because at least one is non-empty).
    user_home = sandbox_ai_home()
    new_specs = _parse_workspace_flags(inst, user_home, copy, empty)

    # Reject collisions with existing workspaces.
    for ws in new_specs:
        if ws.name in existing_names:
            console.print(
                f"Workspace {ws.name!r} already exists in instance {inst!r}.",
                style="red",
            )
            raise typer.Exit(code=1)

    # Pre-flight every --copy source.
    for ws in new_specs:
        if ws.bootstrap_mode == "copy" and ws.source is not None:
            _preflight_workspace_source(ws.source, inst=inst, user_home=user_home)

    _require_instance_stopped(inst, instance_dir)
    _refuse_if_backup_in_progress(inst)

    # Acquire state.lock for the duration of the mutation.
    try:
        lock_fd = _acquire_state_lock(instance_dir)
    except BlockingIOError:
        console.print(
            "Another sandbox operation is already in progress for this instance.",
            style="red",
        )
        raise typer.Exit(code=1) from None

    try:
        # Create workspace dirs (orchestrator-owned, mode 0700).
        for ws in new_specs:
            os.makedirs(ws.path, mode=0o700, exist_ok=True)

        # Populate --copy workspaces via the rsync recipe.
        for ws in new_specs:
            if ws.bootstrap_mode == "copy" and ws.source is not None:
                copy_workspace(ws.source, ws.path)

        # Mutate sandbox.toml: append new entries to the [workspaces] block.
        merged = [
            WorkspaceSpec(name=name, bootstrap_mode=ws.bootstrap_mode, source=ws.source, path=ws.path)
            for name, ws in config.workspaces.items()
        ] + list(new_specs)
        mutate_workspaces(instance_dir, merged)
    finally:
        _release_lock(lock_fd)

    added = ", ".join(ws.name for ws in new_specs)
    console.print(f"Added workspace(s) to {inst!r}: {added}.")


@workspace_app.command("remove")
def workspace_remove(
    inst: str = typer.Argument(..., help="Instance name"),
    ws_name: str = typer.Argument(..., help="Workspace name"),
    backup: bool = typer.Option(False, "--backup", help="Back up the workspace before removal"),
    purge: bool = typer.Option(False, "--purge", help="Remove without backup"),
) -> None:
    """Remove a workspace from a stopped instance."""
    _require_per_user_state_initialized()

    if backup and purge:
        console.print("--backup and --purge are mutually exclusive.", style="red")
        raise typer.Exit(code=1)

    instance_dir = _lookup_instance_or_exit(inst)
    config = _load_config(instance_dir)
    if ws_name not in config.workspaces:
        names = ", ".join(sorted(config.workspaces.keys())) or "<none>"
        console.print(
            f"Workspace {ws_name!r} not found in instance {inst!r}. Available: {names}.",
            style="red",
        )
        raise typer.Exit(code=1)

    # Resolve mode in TTY/non-TTY contexts when neither flag is given.
    if not backup and not purge:
        if _stdin_is_tty():
            response = typer.prompt(
                f"Backup workspace {ws_name!r} before removing? [Y/n]",
                default="Y",
                show_default=False,
            )
            backup = response.strip().lower() not in ("n", "no")
            purge = not backup
        else:
            console.print(
                "Use --backup or --purge to specify removal mode in non-interactive contexts.",
                style="red",
            )
            raise typer.Exit(code=1)

    _require_instance_stopped(inst, instance_dir)
    if not backup:
        _refuse_if_backup_in_progress(inst)

    target = config.workspaces[ws_name]
    last_workspace = len(config.workspaces) == 1

    if backup:
        # Phase order: state.lock not held → backup acquires backup.lock and
        # runs rsync → returns → state.lock acquired below for rmtree +
        # sandbox.toml mutation.
        try:
            create_backup(
                instance_name=inst,
                workspace_name=ws_name,
                source_path=target.path,
                source_bootstrap_mode=target.bootstrap_mode.value,
                dev_primary_gid=os.getgid(),
            )
        except BackupError as exc:
            console.print(f"Backup failed: {exc}", style="red")
            raise typer.Exit(code=1) from exc

    try:
        lock_fd = _acquire_state_lock(instance_dir)
    except BlockingIOError:
        console.print(
            "Another sandbox operation is already in progress for this instance.",
            style="red",
        )
        raise typer.Exit(code=1) from None

    try:
        shutil.rmtree(target.path, ignore_errors=False)
        remaining = [
            WorkspaceSpec(
                name=name,
                bootstrap_mode=ws.bootstrap_mode.value,
                source=ws.source,
                path=ws.path,
            )
            for name, ws in config.workspaces.items()
            if name != ws_name
        ]
        mutate_workspaces(instance_dir, remaining)
    finally:
        _release_lock(lock_fd)

    console.print(f"Removed workspace {ws_name!r} from {inst!r}.")
    if last_workspace:
        console.print(
            f"WARNING: {inst!r} now has zero workspaces; "
            f"`sandbox start {inst}` will fail until you add one.",
            style="yellow",
        )


@workspace_app.command("rename")
def workspace_rename(
    inst: str = typer.Argument(..., help="Instance name"),
    old: str = typer.Argument(..., help="Existing workspace name"),
    new: str = typer.Argument(..., help="New workspace name"),
) -> None:
    """Rename a workspace in a stopped instance (atomic, ACL-preserving)."""
    _require_per_user_state_initialized()

    if old == new:
        console.print(
            f"Old and new workspace names are identical ({old!r}); refusing no-op rename.",
            style="red",
        )
        raise typer.Exit(code=1)

    _validate_name(new, kind="workspace", max_len=_WORKSPACE_NAME_MAX)

    instance_dir = _lookup_instance_or_exit(inst)
    config = _load_config(instance_dir)

    if old not in config.workspaces:
        names = ", ".join(sorted(config.workspaces.keys())) or "<none>"
        console.print(
            f"Workspace {old!r} not found in instance {inst!r}. Available: {names}.",
            style="red",
        )
        raise typer.Exit(code=1)
    if new in config.workspaces:
        console.print(
            f"Workspace {new!r} already exists in instance {inst!r}.",
            style="red",
        )
        raise typer.Exit(code=1)

    _require_instance_stopped(inst, instance_dir)
    _refuse_if_backup_in_progress(inst)

    old_spec = config.workspaces[old]
    new_path = str(sandbox_ai_home() / "workspaces" / inst / new)

    try:
        lock_fd = _acquire_state_lock(instance_dir)
    except BlockingIOError:
        console.print(
            "Another sandbox operation is already in progress for this instance.",
            style="red",
        )
        raise typer.Exit(code=1) from None

    try:
        # R2: atomic same-fs rename. ACL/setgid/xattrs preserved by inode.
        try:
            os.rename(old_spec.path, new_path)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                console.print(
                    f"Cross-filesystem rename not supported "
                    f"({old_spec.path!r} → {new_path!r}). "
                    "See doctor's `workspace_home_single_filesystem` check.",
                    style="red",
                )
                raise typer.Exit(code=1) from None
            raise

        # R3: rewrite [workspaces.<old>] → [workspaces.<new>] + path field.
        renamed = [
            WorkspaceSpec(
                name=(new if name == old else name),
                bootstrap_mode=ws.bootstrap_mode.value,
                source=ws.source,
                path=(new_path if name == old else ws.path),
            )
            for name, ws in config.workspaces.items()
        ]
        mutate_workspaces(instance_dir, renamed)
    finally:
        _release_lock(lock_fd)

    console.print(f"Renamed workspace {old!r} → {new!r} in {inst!r}.")


if __name__ == "__main__":
    app()
