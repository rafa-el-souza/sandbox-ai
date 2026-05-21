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
import shlex
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pydantic
import typer
from core import dispatch
from core.actions import (
    ActionContext,
    ComposeUpAction,
    HelperCpChownAction,
    HelperMkdirChownAction,
    NamedAclGrantAction,
    NamedAclRevokeAction,
    WorkspaceSharedGroupAction,
)
from core.compose import compose_project_name
from core.crypto import generate_credential, generate_ssh_keypair, hash_proxy_password, write_htpasswd
from core.doctor import (
    build_check_registry,
    check_compose_project_name_collision,
    detect_distro,
    render_results,
    run_check_subset,
    run_checks,
)
from core.exceptions import SandboxExecutionError
from core.executor import Executor
from core.host_config import (
    HostConfig,
    HostSettings,
    MachinectlAuth,
    WorkspaceBridgeGroupMissingError,
    ensure_per_user_state,
    host_gid_for_in_container,
    host_id_for_in_container,
    minimal_host_config,
    pipe_cmd,
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
from core.locks import acquire_backup_lock, is_backup_lock_held
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
from core.setup import cli_flow
from core.setup.extras import selected_extras
from core.setup.l0_identity import OperatorResolutionError, emit_distro_gate, resolve_operator
from core.setup.l6a_runsc import set_force_update
from core.setup.phase_runner import SetupContext, run_apply_pass, run_plan_pass
from core.walker import BoundaryPathError as WalkerBoundaryPathError
from core.walker import walk_ancestors
from core.workspace_backups import (
    BackupError,
    BackupFilter,
    BackupSpecAmbiguousError,
    BackupSpecNotFoundError,
    create_backup,
    list_backups,
    resolve_backup_spec,
    restore_backup,
)
from core.workspace_copy import copy_workspace
from rich.console import Console
from rich.markup import escape as rich_escape
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
        # Subnet names from the IPAM quintuple
        "isolated",
        "core_proxy",
        "dns",
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
    """Parse sandbox.toml from instance directory.

    Wraps Pydantic's ``ValidationError`` at this CLI boundary so all callers
    surface schema failures as one ``Invalid <toml>: <field>: <reason>`` line
    per error rather than a raw library traceback. Non-validation errors
    (``FileNotFoundError``, ``OSError``, ``tomllib.TOMLDecodeError``)
    propagate intact — the ``except`` matches ``pydantic.ValidationError``
    specifically, never bare ``Exception``.
    """
    toml_path = os.path.join(instance_dir, "sandbox.toml")
    try:
        return InstanceConfig.from_toml(toml_path)
    except pydantic.ValidationError as exc:
        for err in exc.errors():
            field = ".".join(str(p) for p in err["loc"])
            console.print(f"Invalid {toml_path}: {field}: {err['msg']}", style="red")
        raise typer.Exit(1) from None


# ─── Warm state check ───────────────────────────────────────────────────────


def _container_status(
    instance_dir: str,
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

    # NOTE: the daemon's `u:<host_user>:r` ACL on `.sandbox.env` is granted
    # once at start and is `granted-once, persistent` per cluster 3's
    # Environment File Read ACL requirement. It survives `sandbox stop`, so
    # no idempotent re-grant is needed here before the compose-ps query.

    # `compose-ps` is a *probe* callsite: a non-zero exit (stopped instance,
    # boundary error) must yield an empty list, not an abort. Per Q8 the
    # probe-style entry point is ``core.dispatch.probe`` — it returns a typed
    # ``ProbeOutcome`` and never raises for op failure. The Q6 project name /
    # ``--compose-file`` / ``--env-file`` operands are resolved internally by
    # ``invoke``'s single operator-side resolver (``_resolve_compose_state``),
    # so they are no longer constructed here.
    host_config = minimal_host_config(host_user, auth)
    outcome = dispatch.probe("compose-ps", [config.instance.name], host_config)
    if not outcome.ok:
        return []

    containers: list[ContainerInfo] = []
    if outcome.stdout:
        for line in outcome.stdout.strip().splitlines():
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
    return bool(_container_status(instance_dir, host_user, config, auth))


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

    Returns proxy password. Per-file daemon read access on the freshly-
    written secrets (and on the helper-cp ro/exec source files written
    by :func:`_phase_hydrate`) is granted by
    :func:`_phase_grant_post_hydrate_daemon_read` AFTER hydrate, in
    one unified pass. The unified pass exists because
    ``core.hydration.write_restricted`` does ``os.open(O_CREAT, mode)``
    followed by ``os.fchmod(mode)``, which zero the ACL ``mask::`` to
    match the new mode's group bits and would mask out any inherited
    named entry; setfacl-as-owner post-write recomputes the mask so the
    daemon's ``u:<host_user>:r`` named entry stays effective.

    Also runs an idempotent ownership reconciliation pass over the IPC
    client-side secrets (``ipc_ssh_key``, ``ipc_known_hosts``) per
    `admin-reframe` Fix B': those two files are dev-owned mode 0600 and
    read directly by the host's ssh client. Pre-Fix-B' instances may
    have them owned by a consumer subuid (legacy ``RO_FILE_RECIPES``
    pattern); the reconciliation brings them back to the operator's
    ownership on every start. No-op when already correct.
    """
    password = generate_credential()
    hashed = hash_proxy_password(password)
    htpasswd_line = f"proxyuser:{hashed}"
    config_proxy_dir = os.path.join(instance_dir, "config", "proxy")
    write_htpasswd(config_proxy_dir, htpasswd_line)

    # SSH keypairs for IPC transport
    generate_ssh_keypair(instance_dir, "auth")
    generate_ssh_keypair(instance_dir, "host", core_ipc_ip=core_ipc_ip)

    # Fix B' ownership reconciliation (admin-reframe design D3).
    # MUST NOT use os.replace/mv/install on these files — bind-mount
    # inode stability per orchestrator-volumes. We mutate the existing
    # inode in place via os.chown / os.chmod so any active bind-mount
    # source-fd remains valid across reconciliations.
    operator_uid = os.geteuid()
    operator_gid = os.getegid()
    for fname in ("ipc_ssh_key", "ipc_known_hosts"):
        path = os.path.join(instance_dir, "secrets", fname)
        st = os.stat(path)
        if st.st_uid != operator_uid:
            os.chown(path, operator_uid, operator_gid)
        if (st.st_mode & 0o7777) != 0o600:
            os.chmod(path, 0o600)

    return password


def _post_hydrate_daemon_read_targets(instance_dir: str) -> list[str]:
    """Return absolute paths the post-hydrate setfacl pass targets.

    Aggregates two distinct categories of dev-created files the daemon
    needs explicit DAC read on (cap_dac_override does NOT bypass DAC for
    dev-owned, userns-unmapped inodes — see "Helper-CP Source Files
    Daemon-Readable Pre-Recipe" rationale):

    1. **Helper-cp source files** (``RO_FILE_RECIPES`` + ``EXEC_FILE_RECIPES``):
       transferred to a consumer subuid by the helper-cp recipe. The
       per-file ``setfacl -m u:<host_user>:r`` covers the daemon's read
       through the bind mount during the recipe's ``cp /p/<file>`` step.
    2. **Daemon-read direct files** (``DAEMON_READ_DIRECT_FILES``):
       NEVER transferred — the daemon reads them in place forever. The
       canonical case is ``docker compose -f <compose.yml>`` and its
       conditional extras (``db-postgres.yml``, ``mcp-firecrawl.yml``);
       paths are derived from the same single-source-of-truth the
       compose-file list resolves from
       (``core.dispatch._resolve_compose_state``).

    Both categories converge in one post-hydrate setfacl pass because
    ``core.hydration.write_restricted`` zeros ``mask::`` via
    ``os.fchmod(mode)`` (which would mask out any inherited named
    entry); the per-file setfacl recomputes the mask so the named entry
    stays effective.

    Returns paths only for files that exist on disk (defensive — the
    compose extras are present only when ``InstanceConfig`` enables the
    matching component).
    """
    paths: list[str] = []
    for parent, files, _consumer_uid, _mode in (*RO_FILE_RECIPES, *EXEC_FILE_RECIPES, *RW_FILE_RECIPES):
        parent_abs = os.path.join(instance_dir, parent)
        for fname in files:
            path = os.path.join(parent_abs, fname)
            if os.path.isfile(path):
                paths.append(path)
    for parent, files in DAEMON_READ_DIRECT_FILES:
        parent_abs = os.path.join(instance_dir, parent)
        for fname in files:
            path = os.path.join(parent_abs, fname)
            if os.path.isfile(path):
                paths.append(path)
    return paths


def _grant_post_hydrate_daemon_read(instance_dir: str, host_user: str) -> None:
    """Add a ``u:<host_user>:r`` named ACL entry to every post-hydrate target.

    See :func:`_post_hydrate_daemon_read_targets` for the file inventory
    (helper-cp source files + daemon-read direct files like
    ``docker/compose.yml``). Runs ``setfacl -m u:<host_user>:r <path>``
    AS THE FILE OWNER (``dev``) on each target; setfacl-as-owner does
    not require escalated privilege. ``setfacl`` recomputes ``mask::``
    to cover the named entry, restoring effective daemon read access
    against ``core.hydration.write_restricted``'s fchmod-zeroed mask.

    Failures surface as ``SandboxExecutionError`` mentioning the
    offending path and the phrase "grant daemon read on post-hydrate
    target"; the failure is not silently swallowed.
    """
    for path in _post_hydrate_daemon_read_targets(instance_dir):
        try:
            subprocess.run(
                ["setfacl", "-m", f"u:{host_user}:r", path],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip() if exc.stderr else f"exit {exc.returncode}"
            raise SandboxExecutionError(
                f"Failed to grant daemon read on post-hydrate target {path}: {stderr}"
            ) from exc


def _phase_grant_post_hydrate_daemon_read(instance_dir: str, host_user: str) -> None:
    """Phase 5b: setfacl-as-owner pass on all post-hydrate daemon-read targets.

    Thin wrapper over :func:`_grant_post_hydrate_daemon_read` that runs
    after `_phase_hydrate` and `_phase_credentials` so every file the
    daemon will read (helper-cp source files AND direct-read files like
    ``docker/compose.yml``) has been written by then. Closes the
    "Helper-CP Source Files Daemon-Readable Pre-Recipe" requirement
    under orchestrator-volumes.
    """
    _grant_post_hydrate_daemon_read(instance_dir, host_user)


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
) -> list[NamedAclGrantAction]:
    """Build the ACL grant plan — single source of truth for Phase 5 and dry-run (D4).

    Returns a list of :class:`NamedAclGrantAction` objects:
    - Ancestors: ``--x`` (traverse only)
    - Instance root: ``r-x``
    - docker/: ``rX`` recursive
    - config/: ``rX`` recursive
    - .sandbox.env: ``r``
    - secrets/: dir-level ``rX`` traverse + default ACL ``u:<host_user>:r``
      so files created inside (by ``_phase_credentials``) inherit a
      daemon-readable named entry without an after-the-fact chgrp/chown.
    - per-workspace named-ACL: effective ``rwx`` plus default with named entry on each ``workspace.path``

    Cache/log Option-B grants are intentionally absent — replaced by the
    helper-mkdir+chown phase (acl-ownership-recipes Decision 1).
    """
    plan: list[NamedAclGrantAction] = []

    def _g(
        argv: list[str],
        description: str,
        *,
        target: str,
        entry: str,
        default: bool = False,
        recursive: bool = False,
    ) -> NamedAclGrantAction:
        # Tuple-cast at append time per design Decision 1: command is
        # immutable on the frozen Action so downstream code can't mutate
        # the live argv held by a grant entry. Structured fields
        # (target/entry/default/recursive) enable Refactor C dispatch.
        return NamedAclGrantAction(
            command=tuple(argv),
            description=description,
            target=Path(target),
            entry=entry,
            default=default,
            recursive=recursive,
        )

    # Ancestors — execute-only traverse
    for ancestor in _compute_ancestors(instance_dir):
        plan.append(
            _g(
                ["setfacl", "-m", f"u:{host_user}:--x", ancestor],
                f"ancestor traverse: {ancestor}",
                target=ancestor,
                entry=f"u:{host_user}:--x",
            )
        )

    # Instance root — read + execute
    plan.append(
        _g(
            ["setfacl", "-m", f"u:{host_user}:r-x", instance_dir],
            f"instance root: {instance_dir}",
            target=instance_dir,
            entry=f"u:{host_user}:r-x",
        )
    )

    # docker/ — recursive read + conditional execute
    docker_dir = os.path.join(instance_dir, "docker/")
    plan.append(
        _g(
            ["setfacl", "-R", "-m", f"u:{host_user}:rX", docker_dir],
            f"docker config: {docker_dir}",
            target=docker_dir,
            entry=f"u:{host_user}:rX",
            recursive=True,
        )
    )

    # docker/core/ — helper-cp target needs rwx on the parent so the recipe
    # can unlink+recreate entrypoint.sh (added to RO_FILE_RECIPES).
    docker_core_dir = os.path.join(instance_dir, "docker/core")
    plan.append(
        _g(
            ["setfacl", "-m", f"u:{host_user}:rwx", docker_core_dir],
            f"helper-cp parent: {docker_core_dir}",
            target=docker_core_dir,
            entry=f"u:{host_user}:rwx",
        )
    )

    # config/ — recursive READ + conditional execute (NOT write). The temp
    # commit ``6c3bcb4`` widened this to ``rwX`` so the helper-cp recipe's
    # cross-fs ``mv`` could unlink the existing destination at the host
    # level. With cluster-2's structural fixes this is no longer required:
    #   - the helper recipe is now ``unlink+cp+chmod+chown`` inside the
    #     helper container (helper_chown_files), running as in-container
    #     root with ``cap_dac_override`` — host DAC is bypassed for the
    #     unlink/create steps inside the helper.
    #   - the daemon's host-level write requirement is narrowed to BUG-B
    #     (provisioning write on each helper-cp parent dir), satisfied by
    #     the per-parent dir-level ``rwx`` entries below.
    # Read-only here keeps daemon DAC minimal-privilege on file CONTENTS
    # (mirrors secrets/ where dir-level ``rwx`` was retained but recursive
    # widening was rejected).
    config_dir = os.path.join(instance_dir, "config/")
    plan.append(
        _g(
            ["setfacl", "-R", "-m", f"u:{host_user}:rX", config_dir],
            f"config files: {config_dir}",
            target=config_dir,
            entry=f"u:{host_user}:rX",
            recursive=True,
        )
    )

    # config/<subdir> — dir-level ``rwx`` on each helper-cp parent dir to
    # satisfy BUG-B for config/ (parallel to the ``docker/core`` and
    # ``secrets/`` rwx grants). The helper-cp recipe's host-level
    # bind-mount of ``config/<subdir>`` lets the in-helper unlink reach
    # the host inode; even with ``cap_dac_override`` inside the helper
    # the on-host parent still needs the daemon write bit so the bind
    # mount itself is mountable read-write into the helper. Without this
    # the helper's unlink EPERMs.
    for rel in ("config/coredns", "config/dnsdist", "config/proxy", "config/core"):
        helper_cp_parent = os.path.join(instance_dir, rel)
        plan.append(
            _g(
                ["setfacl", "-m", f"u:{host_user}:rwx", helper_cp_parent],
                f"helper-cp parent: {helper_cp_parent}",
                target=helper_cp_parent,
                entry=f"u:{host_user}:rwx",
            )
        )

    # .sandbox.env — read only
    env_file = os.path.join(instance_dir, ".sandbox.env")
    plan.append(
        _g(
            ["setfacl", "-m", f"u:{host_user}:r", env_file],
            f"env file: {env_file}",
            target=env_file,
            entry=f"u:{host_user}:r",
        )
    )

    # secrets/ — dir-level ``rwx`` provisioning grant. The dir-level write
    # bit is load-bearing for BUG-B (the daemon's helper-cp ``mv /tmp/$f
    # /p/$f`` step requires write on the destination's parent so it can
    # unlink the existing dest and rename the new inode in). The named
    # entry's effect is partitioned along two axes that together replace
    # the temp's recursive ``rwX`` (which over-widened by granting daemon
    # write on file CONTENTS):
    #   - dir-level ``rwx`` (this entry) covers BUG-B (provisioning write
    #     on the parent — needed by ``mv``).
    #   - per-file ``r`` (granted by ``_phase_grant_post_hydrate_daemon_read``
    #     post-hydrate, iterating ``RO_FILE_RECIPES + EXEC_FILE_RECIPES``)
    #     covers BUG-A (runtime read on each secret's contents). The
    #     per-file pass also defeats ``write_restricted``'s ``fchmod``
    #     mask-reset which would mask out any inherited named entry.
    # The default ACL applied below is belt-and-suspenders / future-proof
    # for any write path that ultimately leaves an extended ACL intact.
    # The recursive ``rwX`` widening from temp commit ``0b35a53`` MUST
    # NOT appear in the plan.
    secrets_dir = os.path.join(instance_dir, "secrets/")
    plan.append(
        _g(
            ["setfacl", "-m", f"u:{host_user}:rwx", secrets_dir],
            f"secrets dir provisioning write: {secrets_dir}",
            target=secrets_dir,
            entry=f"u:{host_user}:rwx",
        )
    )
    plan.append(
        _g(
            ["setfacl", "-d", "-m", f"u::rw-,g::---,o::---,m::r--,u:{host_user}:r", secrets_dir],
            f"secrets default ACL: {secrets_dir}",
            target=secrets_dir,
            entry=f"u::rw-,g::---,o::---,m::r--,u:{host_user}:r",
            default=True,
        )
    )

    # Helper-cp parents — default ACL `u:<host_user>:r` so newly-created
    # replacement files (helper recipe re-creates each ro file from a tmpfs
    # scratch via cp+unlink+cp) inherit a daemon-readable named entry.
    for rel in (
        "config/coredns",
        "config/dnsdist",
        "config/proxy",
        "config/core",
        "secrets",
    ):
        parent_dir = os.path.join(instance_dir, rel)
        plan.append(
            _g(
                ["setfacl", "-d", "-m", f"u:{host_user}:r", parent_dir],
                f"helper-cp parent default ACL: {parent_dir}",
                target=parent_dir,
                entry=f"u:{host_user}:r",
                default=True,
            )
        )

    # Helper-recipe parents (cache/log) — grant <daemon>:rwx + matching default
    # so the helper-mkdir+chown phase can create leaves inside them.
    for rel in ("cache/core", "log"):
        parent_dir = os.path.join(instance_dir, rel)
        plan.append(
            _g(
                ["setfacl", "-m", f"u:{host_user}:rwx", parent_dir],
                f"helper-recipe parent: {parent_dir}",
                target=parent_dir,
                entry=f"u:{host_user}:rwx",
            )
        )
        plan.append(
            _g(
                ["setfacl", "-d", "-m", f"u::rwx,g::rwx,o::---,m::rwx,u:{host_user}:rwx", parent_dir],
                f"helper-recipe parent default ACL: {parent_dir}",
                target=parent_dir,
                entry=f"u::rwx,g::rwx,o::---,m::rwx,u:{host_user}:rwx",
                default=True,
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
                    _g(
                        ["setfacl", "-m", f"u:{host_user}:--x", ancestor],
                        f"workspace ancestor traverse: {ancestor}",
                        target=ancestor,
                        entry=f"u:{host_user}:--x",
                    )
                )
            plan.append(
                _g(
                    ["setfacl", "-m", f"u:{host_user}:rwx", ws_path],
                    f"workspace named-ACL: {ws_path}",
                    target=ws_path,
                    entry=f"u:{host_user}:rwx",
                )
            )
            default_entry = f"u::rwx,g::rwx,o::---,m::rwx,u:{host_user}:rwx"
            if dev_user:
                default_entry += f",u:{dev_user}:rwx"
            plan.append(
                _g(
                    ["setfacl", "-d", "-m", default_entry, ws_path],
                    f"workspace default ACL: {ws_path}",
                    target=ws_path,
                    entry=default_entry,
                    default=True,
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
) -> list[NamedAclRevokeAction]:
    """Build the ACL revoke plan — strict subset of revertible operations.

    The revoke plan is NOT a 1:1 inverse of the grant plan. It excludes every
    grant whose lifecycle is `granted-once, persistent` or whose mechanism is
    not the named-ACL (cluster 3 — Acl Revoke Plan Excludes Persistent Grants).

    Excluded by lifecycle/mechanism:
    - Ancestor traverse ACLs (granted-once, persistent — required for next start
      to find the instance directory)
    - Workspace shared-group chmod 2770 + group ownership (granted-once,
      persistent — only the named-ACL portion is revoked)
    - Cache/log subuid chowns (mechanism is chown, not setfacl; preserved
      across stop/start cycles by design)
    - Helper-cp parent default ACLs and recursive entries that would walk into
      consumer-owned files (the recursive `setfacl -R -x` walk EPERMs because
      dev lacks CAP_FOWNER on consumer-uid-owned files; helper-cp managed files
      are unlinked separately by `_phase_stop_unlink_consumer_files` and the
      parent default ACL is granted-once / re-applied idempotently on next
      start)
    - `.sandbox.env` named ACL (cluster 3 — Environment File Read ACL is now
      `granted-once, persistent`; survives stop, removed only by destroy's
      rmtree)

    Included (`granted-at-start, revoked-at-stop`):
    - Instance root named-ACL
    - docker/ dir-level named-ACL (NOT recursive — consumer-owned files inside
      are handled by the helper-cp unlink pass)
    - config/ dir-level named-ACL (NOT recursive — same rationale)
    - secrets/ dir-level traverse named-ACL
    - Per-workspace effective + default-ACL named entries
    """
    plan: list[NamedAclRevokeAction] = []
    user_entry = f"u:{host_user}"

    def _r(
        argv: list[str],
        description: str,
        *,
        target: str,
        entry: str = user_entry,
        default: bool = False,
    ) -> NamedAclRevokeAction:
        return NamedAclRevokeAction(
            command=tuple(argv),
            description=description,
            target=Path(target),
            entry=entry,
            default=default,
        )

    # Instance root
    plan.append(
        _r(
            ["setfacl", "-x", f"u:{host_user}", instance_dir],
            f"instance root: {instance_dir}",
            target=instance_dir,
        )
    )

    # docker/ — dir-level only (NOT recursive: consumer-owned files inside
    # would EPERM under setfacl from dev; the recursive walk was the source
    # of the cluster-3 stop-time setfacl warnings).
    docker_dir = os.path.join(instance_dir, "docker/")
    plan.append(
        _r(
            ["setfacl", "-x", f"u:{host_user}", docker_dir],
            f"docker config: {docker_dir}",
            target=docker_dir,
        )
    )

    # config/ — dir-level only (NOT recursive: same rationale as docker/).
    config_dir = os.path.join(instance_dir, "config/")
    plan.append(
        _r(
            ["setfacl", "-x", f"u:{host_user}", config_dir],
            f"config files: {config_dir}",
            target=config_dir,
        )
    )

    # secrets/ dir-level traverse + symmetric default-ACL revocation
    # (per cluster 2's "Secrets Inherit Daemon-Readable Default ACL"
    # requirement, which sets the default ACL at start and revokes it at
    # stop). The named per-user entries are dev-applied and dev-revocable
    # — no consumer-owned files are at the dir level here.
    secrets_dir = os.path.join(instance_dir, "secrets/")
    plan.append(
        _r(
            ["setfacl", "-x", f"u:{host_user}", secrets_dir],
            f"secrets dir traverse: {secrets_dir}",
            target=secrets_dir,
        )
    )
    plan.append(
        _r(
            ["setfacl", "-d", "-x", f"u:{host_user}", secrets_dir],
            f"secrets default ACL: {secrets_dir}",
            target=secrets_dir,
            default=True,
        )
    )

    # Workspace named-ACL — both effective and default-entry revocation.
    # Persistent shared-group state (chgrp/chmod/setgid + g::rwx, u:dev:rwx
    # default) is left intact (Decision 4). Per-workspace fan-out.
    if workspace_paths:
        for ws_path in workspace_paths:
            plan.append(
                _r(
                    ["setfacl", "-x", f"u:{host_user}", ws_path],
                    f"workspace named-ACL: {ws_path}",
                    target=ws_path,
                )
            )
            plan.append(
                _r(
                    ["setfacl", "-d", "-x", f"u:{host_user}", ws_path],
                    f"workspace default named entry: {ws_path}",
                    target=ws_path,
                    default=True,
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
    # Secrets — core consumer (agent) — mode 0600.
    #
    # Fix B' (admin-reframe design D3): the IPC client-side files
    # ``ipc_ssh_key`` and ``ipc_known_hosts`` are NOT in this table.
    # They remain dev-owned mode 0600 (hydration default) because the
    # host's ssh client reads them directly during ``sandbox attach`` —
    # they never participate in the helper-cp ownership transfer to a
    # consumer subuid. The cascade through
    # ``_phase_stop_unlink_consumer_files`` (which iterates this table
    # plus EXEC_/RW_FILE_RECIPES) is automatic: those two files survive
    # stop/start cycles as dev-owned 0600 and are reconciled idempotently
    # by ``_phase_credentials`` on every start.
    ("secrets", ("authorized_keys", "ipc_host_key"), 1000, 0o600),
)
"""Single source of truth (ro config + secrets) for the helper-cp+chown phase and dry-run preview."""


EXEC_FILE_RECIPES: tuple[tuple[str, tuple[str, ...], int, int], ...] = (
    # Core (agent) entrypoint — bind-mounted at /usr/local/bin/entrypoint.sh.
    # Owner-only r-x (mode 0500); the consumer is the sole reader/exec.
    # The executable-script kind is structurally distinct from RO_FILE_RECIPES
    # (mode 0640 for ro configs, 0600 for secrets): owner-only and exec-bit set.
    ("docker/core", ("entrypoint.sh",), 1000, 0o500),
)
"""Sibling of RO_FILE_RECIPES for executable-script (mode 0500 owner-only) entries.

Distinct from ro config / secrets recipes because the entrypoint scripts need
the exec bit and a tighter access set. Consumed alongside RO_FILE_RECIPES by
:func:`_helper_cp_chown_plan` so the helper-cp+chown phase processes both
tables in a single pass.
"""


RW_FILE_RECIPES: tuple[tuple[str, tuple[str, ...], int, int], ...] = (
    # Agent (core) `.claude.json` — bind-mounted RW at /home/agent/.claude.json
    # so Claude Code's CLI can persist its session/state. Consumer is uid 1000
    # inside the container (mapped to claude-sandbox subuid 165536+999=166535
    # on host). Mode 0660 so the consumer's primary group (matching subgid)
    # also gets read+write — this matches the bind mount's RW intent.
    #
    # Distinct from RO_FILE_RECIPES (mode 0640, daemon-readable r--) and
    # EXEC_FILE_RECIPES (mode 0500 owner r-x): RW config files MUST be
    # consumer-writable, which requires owner and group write bits and
    # consumer-uid ownership transfer (otherwise the file presents as
    # ``nobody:nobody`` ``---`` to the agent and silently fails RW).
    ("config/core", (".claude.json",), 1000, 0o660),
)
"""Single source of truth for dev-created RW config files transferred to consumer.

Sibling of RO_FILE_RECIPES + EXEC_FILE_RECIPES. The structural distinction is
that compose mounts these files RW (not RO), so the in-container consumer must
be able to write them. Today the only entry is ``config/core/.claude.json``.

Treatment matches RO_FILE_RECIPES in every dimension except mode:
- Pre-helper-cp: included in :func:`_post_hydrate_daemon_read_targets` so the
  per-file ``setfacl -m u:<host_user>:r`` lets the daemon (and thus the
  helper container's bind-mount source) read the dev-owned source during the
  helper-cp ``cp /p/<file> /tmp/<file>`` step.
- Helper-cp: :func:`_helper_cp_chown_plan` iterates this table alongside
  RO_FILE_RECIPES + EXEC_FILE_RECIPES, transferring ownership to the
  consumer's host subuid+subgid with the per-file mode.
- Stop-time: :func:`_phase_stop_unlink_consumer_files` iterates the same
  plan, so RW files are unlinked before the next start's hydrate writes
  fresh dev-owned replacements.

If a future bind mount adds a new dev-created RW single-file mount, add it
here in lockstep with the compose template change.
"""


DAEMON_READ_DIRECT_FILES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Files the daemon (host_user) reads in-place from the orchestrator-owned
    # tree, with NO ownership transfer through helper-cp. Distinct from
    # RO_FILE_RECIPES / EXEC_FILE_RECIPES (which transfer ownership to a
    # consumer subuid via helper-cp) — these files stay dev-owned forever
    # and the daemon needs explicit DAC read via a named ACL entry because
    # cap_dac_override does not bypass DAC for dev-owned (host-uid-1000,
    # unmapped-in-userns) inodes (per "Helper-CP Source Files
    # Daemon-Readable Pre-Recipe" rationale).
    #
    # Each tuple: ``(parent_relative_to_instance, files)``. The
    # post-hydrate setfacl pass iterates and runs
    # ``setfacl -m u:<host_user>:r <path>`` on each existing file
    # (skip-if-missing for the conditional compose extras whose presence
    # depends on InstanceConfig component flags).
    #
    # ── Compose YAML inputs ───────────────────────────────────────────
    # Consumed by ``docker compose -f <path> ...`` invocations whose
    # canonical path-set is resolved by
    # ``core.dispatch._resolve_compose_state`` (the Q6 single source) and
    # used by ``_phase_compose_up``, ``_compose_down``, and the ``docker
    # compose ps`` callsites in ``_container_status`` /
    # ``_render_status_detailed``.
    ("docker", ("compose.yml",)),
    # Conditional compose extras — present iff the instance enables the
    # component. Listed unconditionally; the post-hydrate phase skips
    # entries whose files do not exist on disk. Same skip-if-missing
    # semantics as ``_grant_post_hydrate_daemon_read`` for `RO_FILE_RECIPES`.
    ("docker/extras", ("db-postgres.yml", "mcp-firecrawl.yml")),
    # ── Build context (compose up --build) ────────────────────────────
    # Buildkit (running as the daemon) reads each Dockerfile + any local
    # COPY sources from the build context during ``docker compose up
    # --build``. These files are dev-created by hydrate (rendered or
    # static-copied — see ``core.hydration.render_templates``) and never
    # transferred via helper-cp; they stay dev-owned forever, so the
    # daemon needs explicit DAC read via a named ACL entry. The empirical
    # symptom of missing this category was
    # ``target coredns: failed to solve: the Dockerfile cannot be empty``
    # (the daemon's open(Dockerfile) returned EACCES; buildkit treats an
    # unreadable Dockerfile as empty).
    #
    # The Dockerfile.<distro>.<family> source files are NOT listed —
    # those live inside the templates wheel and are consumed via Jinja2
    # PackageLoader, not on the per-instance host filesystem. What lands
    # on disk per-instance is the rendered ``Dockerfile.core`` /
    # ``Dockerfile.admin`` (no distro suffix) plus the static
    # ``Dockerfile.coredns`` / ``Dockerfile.mcp-firecrawl`` copies.
    ("docker/core", ("Dockerfile.core",)),
    # admin/fwd.go is COPY'd by Dockerfile.admin during the build;
    # the admin image is a static Go forwarder (no shell entrypoint and
    # nothing bind-mounted at runtime). docker/core's entrypoint.sh IS in
    # ``EXEC_FILE_RECIPES`` and is therefore covered by the helper-cp
    # branch of the post-hydrate setfacl pass.
    ("docker/admin", ("Dockerfile.admin", "fwd.go")),
    ("docker/coredns", ("Dockerfile.coredns",)),
    # Conditional extras Dockerfile — present iff ``mcp_firecrawl`` is
    # enabled. Skip-if-missing covers the disabled case.
    ("docker/extras", ("Dockerfile.mcp-firecrawl",)),
)
"""Authoritative inventory of dev-created files the daemon reads in place.

Single source of truth for the post-hydrate daemon-read setfacl pass on
files that are NOT subject to helper-cp ownership transfer. Spans two
sub-categories:

1. **Compose YAML inputs**: ``compose.yml`` + the conditional compose
   extras (``db-postgres.yml``, ``mcp-firecrawl.yml``). Consumed via
   ``docker compose -f``.
2. **Build context**: Dockerfiles + their local-COPY sources, consumed
   by buildkit during ``docker compose up --build``. The current
   inventory is ``Dockerfile.core``, ``Dockerfile.admin`` +
   ``admin/fwd.go``, ``Dockerfile.coredns``, and the conditional
   ``Dockerfile.mcp-firecrawl``.

If a future Dockerfile gains a new local-COPY source (anything not
``COPY --from=<stage>``), that source MUST be added here in lockstep
with the template change.
"""


CACHE_LOG_LEAVES_BY_PARENT: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cache/core", (".claude",)),
    ("log", ("core",)),
)
"""Per-parent grouping of cache/log leaves consumed by helper-mkdir+chown phase.

Single source of truth for both execution (_phase_helper_mkdir_chown_cache_log)
and dry-run preview (_helper_mkdir_chown_plan).
"""


def _helper_mkdir_chown_plan(instance_dir: str, host_user: str) -> list[HelperMkdirChownAction]:
    """Return one :class:`HelperMkdirChownAction` per cache/log parent.

    ``owner_uid``/``owner_gid`` map in-container uid/gid 1000 (agent / human)
    to their host subuid/subgid via :func:`core.host_config.host_id_for_in_container`.
    """
    owner_uid = host_id_for_in_container(1000, host_user)
    owner_gid = host_gid_for_in_container(1000, host_user)
    return [
        HelperMkdirChownAction(
            parent=Path(os.path.join(instance_dir, parent)),
            leaves=leaves,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )
        for parent, leaves in CACHE_LOG_LEAVES_BY_PARENT
    ]


def _helper_cp_chown_plan(instance_dir: str, host_user: str) -> list[HelperCpChownAction]:
    """Return one :class:`HelperCpChownAction` per recipe entry across ro/exec/rw files.

    Owner uid and gid are both mapped via the host_config forward resolvers:
    owner gid matches the consumer's host subgid; in-container root reads via
    ``cap_dac_override`` (in the helper's cap-add baseline), not via group
    ownership. The literal-0 prior pattern was redundant and incompatible with
    the host-absolute helper API.
    """
    return [
        HelperCpChownAction(
            parent=Path(os.path.join(instance_dir, parent)),
            files=files,
            owner_uid=host_id_for_in_container(consumer_uid, host_user),
            owner_gid=host_gid_for_in_container(consumer_uid, host_user),
            mode=mode,
        )
        for parent, files, consumer_uid, mode in (*RO_FILE_RECIPES, *EXEC_FILE_RECIPES, *RW_FILE_RECIPES)
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
) -> list[WorkspaceSharedGroupAction]:
    """Return one :class:`WorkspaceSharedGroupAction` per chgrp/chmod/setfacl step.

    Per design Decision 1 the per-step ``op`` string is precomputed at
    plan-construction time (interpolating ``bridge_gid`` / ``host_user`` /
    ``dev_user``); ``ActionContext`` carries ``host_user`` but not
    ``dev_user``, so recomputing in ``.describe()`` would force ``dev_user``
    onto the Action — precomputing ``op`` is simpler and matches the
    precomputed-``command`` choice on the ACL Actions.

    Per-workspace fan-out lives in the caller; this function returns the
    plan for **one** workspace.
    """
    default_entry = f"u::rwx,g::rwx,o::---,m::rwx,u:{host_user}:rwx"
    if dev_user:
        default_entry += f",u:{dev_user}:rwx"
    ws = Path(workspace_path)
    ws_str = str(ws)
    return [
        WorkspaceSharedGroupAction(
            workspace_path=ws,
            bridge_gid=bridge_gid,
            step="chgrp",
            op=f"chgrp {bridge_gid}",
            command=(),
        ),
        WorkspaceSharedGroupAction(
            workspace_path=ws,
            bridge_gid=bridge_gid,
            step="chmod_2770",
            op="chmod 2770",
            command=(),
        ),
        WorkspaceSharedGroupAction(
            workspace_path=ws,
            bridge_gid=bridge_gid,
            step="setfacl_effective",
            op=f"setfacl -m u:{host_user}:rwx",
            command=("setfacl", "-m", f"u:{host_user}:rwx", ws_str),
        ),
        WorkspaceSharedGroupAction(
            workspace_path=ws,
            bridge_gid=bridge_gid,
            step="setfacl_default",
            op=f"setfacl -d -m {default_entry}",
            command=("setfacl", "-d", "-m", default_entry, ws_str),
        ),
    ]


def _phase_workspace_shared_group(
    workspace_path: str,
    host: HostSettings,
    dev_user: str | None = None,
    auth: MachinectlAuth = MachinectlAuth.SUDO,
) -> None:
    """Phase 5e: chgrp + chmod 2770 + setfacl on a single workspace tree.

    Drift-detects the workspace root: when not yet at setgid + bridge_gid,
    runs the recursive recipe (best-effort, per-file failures aggregated and
    reported, orchestrator never escalates to sudo per Decision 17). Then
    iterates ``_workspace_shared_group_plan`` and dispatches each step via
    :meth:`WorkspaceSharedGroupAction.execute` — single carrier for the
    dry-run preview and the live phase per the
    "Plan Items Are Typed Action Objects" requirement.

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

    # Steady-state idempotent root setup — same Action plan that the dry-run preview
    # consumes, executed here via .execute(ctx).
    ctx = ActionContext(
        host_user=host_user,
        auth=auth,
        executor=Executor(),
        instance_dir=Path(workspace_path),
    )
    for action in _workspace_shared_group_plan(workspace_path, bridge_gid, dev_user, host_user):
        try:
            action.execute(ctx)
        except SandboxExecutionError as exc:
            # Preserve legacy error-message format for chgrp/chmod root steps —
            # downstream callers and tests assert on "chgrp/chmod failed for ..." prose.
            if action.step in ("chgrp", "chmod_2770"):
                raise SandboxExecutionError(
                    f"chgrp/chmod failed for {workspace_path}: {exc.__cause__ or exc}"
                ) from exc
            raise


def _phase_helper_cp_chown_ro_files(
    instance_dir: str,
    host_user: str,
    auth: MachinectlAuth,
) -> None:
    """Phase 5d: helper-cp + chown for ro single-file mounts.

    Replaces today's ``_phase_credential_ownership`` — IPC SSH secrets are now
    handled by the standard recipe (``secrets/`` group with consumer uid 1000,
    mode 0600). One helper invocation per (parent, consumer_uid, mode) group.

    Each ``HelperCpChownAction`` is executed via ``.execute(ctx)`` so the
    dry-run preview and the live phase share the single carrier
    (per the ``orchestrator-volumes`` "Plan Items Are Typed Action Objects"
    requirement).
    """
    ctx = ActionContext(
        host_user=host_user,
        auth=auth,
        executor=Executor(),
        instance_dir=Path(instance_dir),
    )
    for action in _helper_cp_chown_plan(instance_dir, host_user):
        action.execute(ctx)


def _phase_stop_unlink_consumer_files(instance_dir: str, host_user: str) -> list[str]:
    """Stop-time recipe symmetry partner of `_phase_helper_cp_chown_ro_files`.

    Each helper-cp-managed file is owned by an unmapped consumer subuid after
    start. On a subsequent start, hydration would EACCES trying to overwrite
    those files as dev. We unlink them here so the next hydration's `O_CREAT`
    creates fresh dev-owned files. ``unlink`` requires write+exec on the parent
    only, which dev has on every helper-cp parent (parents are dev-owned).

    Returns a list of human-readable warning strings (one per FileNotFoundError /
    OSError encountered); the phase is fault-isolated, never raises.
    """
    warnings: list[str] = []
    for action in _helper_cp_chown_plan(instance_dir, host_user):
        for fname in action.files:
            path = os.path.join(str(action.parent), fname)
            try:
                os.unlink(path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                warnings.append(f"unlink {path}: {exc}")
    return warnings


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

    ctx = ActionContext(
        host_user=host_user,
        auth=auth,
        executor=Executor(),
        instance_dir=Path(instance_dir),
    )
    for action in _helper_mkdir_chown_plan(instance_dir, host_user):
        parent_abs = str(action.parent)
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
        action.execute(ctx)


def _phase_acl_grant(
    instance_dir: str,
    host_user: str,
    workspace_paths: list[str] | None = None,
    dev_user: str | None = None,
    auth: MachinectlAuth = MachinectlAuth.SUDO,
) -> None:
    """Phase 5: Grant sandbox user ACLs via _acl_grant_plan() (Pattern A).

    Each setfacl call runs via :meth:`NamedAclGrantAction.execute` (which
    invokes ``subprocess.run`` directly — NOT via Executor.run —
    sentinel injection would corrupt the setfacl command, per I-1).
    Action failures raise ``SandboxExecutionError``; we catch and enrich
    with the traverse diagnostic before re-raising (D6).
    """
    ctx = ActionContext(
        host_user=host_user,
        auth=auth,
        executor=Executor(),
        instance_dir=Path(instance_dir),
    )
    for action in _acl_grant_plan(instance_dir, host_user, workspace_paths, dev_user):
        try:
            action.execute(ctx)
        except SandboxExecutionError as exc:
            diag = _diagnose_traverse_failure(instance_dir, host_user)
            if diag:
                raise SandboxExecutionError(f"{exc}\n{diag}") from exc
            raise


def _compose_up_cmd_plan(inst: str) -> ComposeUpAction:
    """Return the :class:`ComposeUpAction` carrying the typed instance intent.

    Sole producer of the compose-up Action. Per Q6/D2 the Action carries ONLY
    the typed instance name; the ``--project`` / ``--env-file`` /
    ``--compose-file`` operands and the env-prefix/verb are resolved internally
    by ``core.dispatch.build_invocation`` (the single command-construction
    seam ``core.dispatch.invoke`` also consumes). The Action is consumed by
    :func:`_phase_compose_up` (via ``.execute(ctx)``) and the dry-run preview
    (via ``.render_command(host_config)``); both derive from the one
    ``build_invocation`` seam so they cannot drift — same single-source-of-truth
    pattern as :func:`_acl_grant_plan`, :func:`_helper_mkdir_chown_plan`, and
    :func:`_helper_cp_chown_plan`.
    """
    return ComposeUpAction(instance_name=inst)


def _phase_compose_up(
    inst: str,
    instance_dir: str,
    host_config: HostConfig,
) -> None:
    """Phase 6: docker compose up via the typed ``compose-up`` dispatcher op.

    Routes through :class:`~core.actions.ComposeUpAction`, whose ``.execute``
    calls ``core.dispatch.invoke("compose-up", [inst], host_config)``. The
    env prefix / ``--ansi never`` / ``--env-file`` / verb (and the Q6
    ``--project`` / ``--compose-file`` expansion) are all internal to the
    ``core.dispatch.build_invocation`` seam — not constructed here. ``invoke``
    raises :class:`~core.exceptions.SandboxExecutionError` on a non-zero exit,
    preserving the abort behavior the prior ``sentinel=True`` path had.
    """
    action = _compose_up_cmd_plan(inst)
    # The full ActionContext is built deliberately: ActionContext is the uniform
    # per-phase plumbing bundle every Action.execute receives, and its four
    # non-host_config fields are required. ComposeUpAction.execute happens to
    # consume only ctx.host_config, but the context contract is uniform across
    # the Action hierarchy by design — not weakened to a per-Action shape (a
    # narrower ctx would force type special-casing the phase loop, the very
    # non-uniformity the render_command contract removed).
    ctx = ActionContext(
        host_user=host_config.host.docker_unprivileged_user,
        auth=host_config.host.machinectl_authentication,
        executor=Executor(),
        instance_dir=Path(instance_dir),
        host_config=host_config,
    )
    action.execute(ctx)


def _build_attach_argv(inst: str, ws: str, host_config: HostConfig) -> list[str]:
    """Build the canonical PTY-handover argv for ``sandbox attach`` / ``sandbox start``.

    The invocation: ``tlog-rec`` wraps a host-side ssh client whose
    ``ProxyCommand`` uses :func:`pipe_cmd` to cross the privilege boundary
    via the byte-pipe primitive, then dials ``/fwd`` inside admin's net
    namespace, which forwards stdio↔TCP to core's sshd on ipc_net. The PTY lives at the host ssh layer; admin
    is a dumb byte pipe (per `admin-reframe` design D1 — Shape 2). Workspace
    cwd is set via the ssh remote-command suffix (per design D9), not via
    ``docker exec -w``.

    The ``ProxyCommand`` uses :func:`pipe_cmd` (polkit-authenticated via
    ``manage-units``) regardless of the host's ``machinectl_authentication``
    mode — ``pipe_cmd`` is auth-mode-independent (per design D2). The ssh
    client itself runs as the operator (dev) with no boundary crossing.

    The ``--file-path`` value is the operator-side ``tlog-rec`` session log
    path; it lives at ``<sandbox_ai_home()>/sessions/<inst>/<UTC-ts>.log``
    and is created on demand.

    Args:
        inst: Instance name (used for the admin container name and the
            sessions log directory).
        ws: Workspace name (used for the remote-command ``cd`` target).
        host_config: Per-host config (used for ``docker_unprivileged_user``).

    Returns:
        The argv to pass to :func:`subprocess.run` (no shell needed).
    """
    sbuser = host_config.host.docker_unprivileged_user
    home = sandbox_ai_home()
    inst_dir = home / "instances" / inst
    secrets = inst_dir / "secrets"

    # Container name uses the compose project name (sanitized-username
    # prefix), not the bare instance name. Mismatching this produces
    # ``Error response from daemon: No such container: <inst>-admin-1``
    # at attach time — verified empirically during admin-reframe smoke.
    project_name = compose_project_name(inst)

    # Core IPC IP — read-only ledger peek (no allocation; instance is
    # already started by the time this argv is built). Per cli-attach
    # spec: attach does not mutate IPAM state.
    base_index, _existing = IPAMLedger().peek_next_slot(inst)
    core_ipc_ip = derive_static_ips(base_index)["core_ipc_ip"]

    # Operator-side session log path — host filesystem only, never a
    # bind mount or instance secrets directory (per cli-attach spec).
    # Keyed by project_name so concurrent instances under different
    # operators don't collide on the session log directory.
    session_log_dir = home / "sessions" / project_name
    session_log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    session_log = session_log_dir / f"{timestamp}.log"

    # ProxyCommand: pipe_cmd → docker exec -i <admin> /fwd <ip>:9999.
    # Joined via shlex.join so the value can be passed as a single
    # ``-o ProxyCommand=...`` token to ssh.
    proxy_argv = [
        *pipe_cmd(sbuser),
        "/usr/bin/docker",
        "exec",
        "-i",
        f"{project_name}-admin-1",
        "/fwd",
        f"{core_ipc_ip}:9999",
    ]
    proxy_command = shlex.join(proxy_argv)

    return [
        "tlog-rec",
        "--writer=file",
        f"--file-path={session_log}",
        "--",
        "ssh",
        "-i",
        str(secrets / "ipc_ssh_key"),
        "-o",
        f"UserKnownHostsFile={secrets / 'ipc_known_hosts'}",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"ProxyCommand={proxy_command}",
        "-p",
        "9999",
        "-t",
        f"agent@{core_ipc_ip}",
        f"cd /workspaces/{shlex.quote(ws)} && exec bash -l",
    ]


# ─── Compose down / ACL revoke ──────────────────────────────────────────────


def _compose_down(
    host_user: str,
    config: InstanceConfig,
    *,
    volumes: bool = False,
    auth: MachinectlAuth = MachinectlAuth.SUDO,
) -> None:
    """Run docker compose down via the typed ``compose-down`` dispatcher op.

    Routes through ``core.dispatch.invoke("compose-down", [inst (+ "--volumes")],
    host_config)``. ``invoke`` raises :class:`SandboxExecutionError` on a
    non-zero exit, preserving the abort behavior the prior ``sentinel=True``
    path had (this callsite never branched on failure — ``stop`` propagates and
    ``destroy`` demotes it to a warning at *their* call sites, not here). The
    stop-vs-destroy distinction is the optional literal ``--volumes`` typed arg
    (``volumes=True`` for ``destroy``/``stop --clean``); the env prefix /
    ``--ansi never`` / ``--env-file`` / verb and the Q6 ``--project`` /
    ``--compose-file`` expansion are all internal to the single
    ``core.dispatch`` seam — not constructed here.
    """
    host_config = minimal_host_config(host_user, auth)
    args = [config.instance.name, "--volumes"] if volumes else [config.instance.name]
    dispatch.invoke("compose-down", args, host_config)


def _revoke_acls(
    instance_dir: str,
    host_user: str,
    workspace_paths: list[str] | None = None,
    auth: MachinectlAuth = MachinectlAuth.SUDO,
) -> list[str]:
    """Revoke sandbox user's ACL entries — fault-isolated, best-effort (D5).

    Iterates ``_acl_revoke_plan()`` and dispatches each entry via
    :meth:`NamedAclRevokeAction.execute`. The Action raises
    ``SandboxExecutionError`` on non-zero exit or OSError; we capture
    those messages into the warnings list (never re-raised — phase is
    fault-isolated).
    """
    warnings: list[str] = []
    ctx = ActionContext(
        host_user=host_user,
        auth=auth,
        executor=Executor(),
        instance_dir=Path(instance_dir),
    )
    for action in _acl_revoke_plan(instance_dir, host_user, workspace_paths):
        try:
            action.execute(ctx)
        except SandboxExecutionError as exc:
            warnings.append(str(exc))
    return warnings


def _phase_stop_teardown(
    instance_dir: str,
    host_user: str,
    config: InstanceConfig,
    workspace_paths: list[str] | None,
    *,
    volumes: bool,
    auth: MachinectlAuth,
) -> list[str]:
    """Shared teardown sequence for `stop` and `destroy` (cluster 3 — Teardown
    Sequence requirement).

    Executes the load-bearing ordering:
      1. ``compose down [-v]`` (with volumes flag controlled by caller)
      2. unlink helper-cp-managed files (so the recursive ACL revoke and the
         next start's hydration both see dev-owned parents only)
      3. revoke named-ACLs via :func:`_revoke_acls`

    Steps 2 and 3 are fault-isolated; their warnings are returned to the caller
    as a list of human-readable strings. Step 1 may raise
    :class:`SandboxExecutionError` and is not caught here — callers wrap
    individually because `stop` and `destroy` differ in how they treat a failed
    compose-down (stop propagates, destroy demotes to a warning).
    """
    warnings: list[str] = []
    _compose_down(host_user, config, volumes=volumes, auth=auth)
    warnings.extend(_phase_stop_unlink_consumer_files(instance_dir, host_user))
    warnings.extend(_revoke_acls(instance_dir, host_user, workspace_paths, auth))
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

    project_name = compose_project_name(inst)

    # ── IPAM preview ─────────────────────────────────────────────────────
    ledger = IPAMLedger()
    try:
        slot, is_existing = ledger.peek_next_slot(inst)
        isolated, core_proxy, dns, egress, ipc = derive_subnets(slot)
        status = "existing" if is_existing else "preview — subject to concurrent changes"
        console.print(f"\n  IPAM slot: {slot} ({status})")
        console.print(f"    Isolated:    {isolated}")
        console.print(f"    Core Proxy:  {core_proxy}")
        console.print(f"    DNS:         {dns}")
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
    console.print("\n  [bold]Commands that would execute:[/bold]")

    dev_user = os.environ.get("USER")
    workspace_paths = [ws.path for _, ws in sorted(config.workspaces.items())]

    # Uniform Action contract: every plan item is rendered via
    # ``.render_command(host_config)``. The base default delegates to
    # ``.describe()`` (HostConfig-independent items ignore it); compose-up
    # overrides it to emit the HostConfig-dependent wire form. Resolve the
    # full HostConfig once here (compose-up is the only consumer that reads it).
    preview_host_config = _resolve_full_host_config()

    # Workspace shared-group plan — runs BEFORE Phase-5 named-ACL grants per
    # the "Workspace Shared-Group Phase Ordering" requirement.
    try:
        bridge_gid = workspace_bridge_gid(host_settings)
        for ws_path in workspace_paths:
            for ws_action in _workspace_shared_group_plan(
                ws_path, bridge_gid, os.environ.get("USER"), host_user
            ):
                console.print(ws_action.render_command(preview_host_config), style="dim")
    except SandboxExecutionError as exc:
        console.print(f"    [red]workspace shared-group plan unavailable: {exc}[/red]")

    # ACL grants — consume _acl_grant_plan (D4 — single source of truth)
    for grant_action in _acl_grant_plan(instance_dir, host_user, workspace_paths, dev_user):
        console.print(grant_action.render_command(preview_host_config), style="dim")

    # Post-hydrate setfacl-as-owner pass — covers the write_restricted
    # fchmod-mask-reset bug. Per-file setfacl on each helper-cp source
    # file (RO_FILE_RECIPES + EXEC_FILE_RECIPES + RW_FILE_RECIPES) AND
    # each daemon-read direct file (DAEMON_READ_DIRECT_FILES — compose.yml
    # + extras).
    for parent, files, _consumer_uid, _mode in (*RO_FILE_RECIPES, *EXEC_FILE_RECIPES, *RW_FILE_RECIPES):
        parent_abs = os.path.join(instance_dir, parent)
        for fname in files:
            console.print(
                f"    $ setfacl -m u:{host_user}:r {parent_abs}/{fname}  # helper-cp source",
                style="dim",
            )
    for parent, files in DAEMON_READ_DIRECT_FILES:
        parent_abs = os.path.join(instance_dir, parent)
        for fname in files:
            console.print(
                f"    $ setfacl -m u:{host_user}:r {parent_abs}/{fname}  # daemon-read direct",
                style="dim",
            )

    # Helper-mkdir+chown for cache/log — single source of truth via plan function
    try:
        for mkdir_action in _helper_mkdir_chown_plan(instance_dir, host_user):
            console.print(mkdir_action.render_command(preview_host_config), style="dim")
        for cp_action in _helper_cp_chown_plan(instance_dir, host_user):
            console.print(cp_action.render_command(preview_host_config), style="dim")
    except SandboxExecutionError as exc:
        console.print(f"    [red]helper-mkdir plan unavailable: {exc}[/red]")

    # Compose up — rendered from the SAME core.dispatch.build_invocation seam
    # _phase_compose_up's ComposeUpAction.execute consumes via invoke(), so the
    # preview is byte-identical to the live invocation (no parallel
    # construction). The Q6 wire expansion resolves compose state via the
    # registry, but ``_lookup_instance_or_exit`` above already guaranteed the
    # instance is registered, so no soft-fail guard is needed here.
    compose_action = _compose_up_cmd_plan(inst)
    compose_cmd = compose_action.render_command(preview_host_config)
    console.print(f"    $ {compose_cmd}", style="dim")

    # Handover — admin-reframe D1: tlog-rec → ssh → ProxyCommand → /fwd into core.
    # The full argv is constructed by `_build_attach_argv` at runtime; the
    # dry-run preview shows the canonical shape rather than re-deriving it
    # (the IPAM peek + sessions log mkdir are runtime-only side effects).
    workspace_names = sorted(config.workspaces.keys())
    ws_preview = workspace_names[0] if len(workspace_names) == 1 else "<ws>"
    sbuser = host_user
    proxy_cmd_preview = shlex.join(
        [*pipe_cmd(sbuser), "/usr/bin/docker", "exec", "-i", f"{project_name}-admin-1", "/fwd", "<core_ipc_ip>:9999"]
    )
    handover_preview = (
        f"tlog-rec --writer=file --file-path=<sessions>/{project_name}/<UTC>.log -- "
        f"ssh -i <inst>/secrets/ipc_ssh_key "
        f"-o UserKnownHostsFile=<inst>/secrets/ipc_known_hosts "
        f"-o StrictHostKeyChecking=yes "
        f"-o ProxyCommand={shlex.quote(proxy_cmd_preview)} "
        f"-p 9999 -t agent@<core_ipc_ip> "
        f"'cd /workspaces/{ws_preview} && exec bash -l'"
    )
    console.print(f"    $ {handover_preview}", style="dim")

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
    return _resolve_full_host_config().host


def _resolve_full_host_config() -> HostConfig:
    """Resolve the full ``HostConfig`` from per-host ``sandbox-ai.toml``.

    Used by ``sandbox attach`` and ``sandbox start``'s handover path —
    :func:`_build_attach_argv` consumes the full ``HostConfig`` (it reads
    ``host.docker_unprivileged_user``); other call sites can keep using
    :func:`_resolve_host_settings` / :func:`_resolve_host_config`.
    """
    try:
        return HostConfig.from_toml()
    except FileNotFoundError as exc:
        console.print(str(exc), style="red")
        raise typer.Exit(code=1) from None


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
            markup=False,
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


_COPY_FLAG = typer.Option([], "--copy", help="Workspace from a copied tree: NAME=PATH (repeatable)")
_EMPTY_FLAG = typer.Option([], "--empty", help="Empty workspace: NAME (repeatable)")


# ─── `sandbox setup` (privileged host-provisioning ceremony) ─────────────────


class _SetupAborted(Exception):
    """Operator pressed Ctrl-C during the setup ceremony (exit 130)."""


def _run_setup_update_runsc(ctx: SetupContext) -> int:
    """``--update-runsc``: run ONLY the L6a phase with ``force=True``.

    Per spec task 8.6 / R1: L6a is a distinct phase, so "run only L6a" is
    well-defined — filter the discovered phase set to the ``l6a`` phase and
    flip the module-local force toggle so ``install_pinned(force=True)``
    bypasses the drift-skip.
    """
    set_force_update(True)
    phases = [p for p in cli_flow.build_phase_list(()) if p.id == "l6a"]
    # Subset run: l6a's ``l6`` dependency is not in this single-phase list but
    # is known-satisfied on the converged host --update-runsc runs against, so
    # order the subset with external deps assumed satisfied (else order_phases'
    # strict guard raises PhaseDependencyError on the dangling ``l6`` edge).
    plan = run_plan_pass(phases, ctx, allow_external_deps=True)
    for line in cli_flow.render_plan(phases, plan):
        console.print(line, markup=False)
    console.print(
        cli_flow.plan_summary_line(cli_flow.tally_plan(plan)), markup=False
    )
    apply_outcomes = run_apply_pass(phases, ctx, allow_external_deps=True)
    for line in cli_flow.summarize_apply(phases, apply_outcomes):
        console.print(line, markup=False)
    return 1 if cli_flow.apply_pass_failed(apply_outcomes) else 0


@app.command()
def setup(
    operator: str | None = typer.Option(
        None, "--operator", help="Operator user (precedence: this flag → $SUDO_USER → $PKEXEC_UID)"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Run only the plan pass; apply nothing"),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the untested-distro and apply-confirm prompts"
    ),
    update_runsc: bool = typer.Option(
        False, "--update-runsc", help="Re-run ONLY the L6a runsc phase with force=True"
    ),
    enable_fapolicyd_integration: bool = typer.Option(
        False, "--enable-fapolicyd-integration", help="Opt in to the fapolicyd trust drop-in phase"
    ),
    enable_aide_integration: bool = typer.Option(
        False, "--enable-aide-integration", help="Opt in to the AIDE config drop-in phase"
    ),
) -> None:
    """Provision the host: privileged plan/apply two-pass ceremony (run as root)."""
    if os.geteuid() != 0:
        console.print(
            "sandbox setup must be run as root. Re-invoke as: sudo sandbox setup",
            style="red",
            markup=False,
        )
        raise typer.Exit(code=1)

    try:
        ctx = _build_setup_context_with_operator(operator)
    except OperatorResolutionError as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(code=1) from None

    original_handler = signal.getsignal(signal.SIGINT)

    def _sigint(_signum: int, _frame: object) -> None:
        raise _SetupAborted

    signal.signal(signal.SIGINT, _sigint)
    try:
        exit_code = _setup_body(
            ctx,
            dry_run=dry_run,
            yes=yes,
            update_runsc=update_runsc,
            flags={
                "fapolicyd": enable_fapolicyd_integration,
                "aide": enable_aide_integration,
            },
        )
    except _SetupAborted:
        print(
            "aborted by operator (SIGINT). No mutations applied.",
            file=sys.stderr,
        )
        raise typer.Exit(code=130) from None
    finally:
        signal.signal(signal.SIGINT, original_handler)

    raise typer.Exit(code=exit_code)


def _build_setup_context_with_operator(operator_flag: str | None) -> SetupContext:
    """Build the per-run :class:`SetupContext` for ``sandbox setup``.

    On a fresh host ``<sandbox_ai_home()>/config/sandbox-ai.toml`` does not
    exist yet — phase L4 is what seeds it — but the CLI needs a
    :class:`HostConfig` to construct the context *before* any phase runs. The
    spec is silent on the absent-toml CLI bootstrap; per the brief we take the
    simplest spec-consistent option: when the toml is absent, use a
    defaults-based config (``minimal_host_config`` with the same defaults L4's
    ``_SEED_DEFAULTS`` writes — ``docker_unprivileged_user="sandbox"``, SUDO
    auth). When the toml is present, the real loaded config is used. Operator
    is resolved once via the canonical L0 resolver, threading ``--operator``.
    """
    try:
        host_config = HostConfig.from_toml()
    except FileNotFoundError:
        host_config = minimal_host_config("sandbox", MachinectlAuth.SUDO)
    return SetupContext(
        host_config=host_config, operator=resolve_operator(operator_flag)
    )


def _setup_body(
    ctx: SetupContext,
    *,
    dry_run: bool,
    yes: bool,
    update_runsc: bool,
    flags: dict[str, bool],
) -> int:
    """The setup ceremony proper. Returns the process exit code.

    SIGINT is handled by the caller (translated to exit 130); the distro gate,
    plan pass, gating decision, prompt, and apply pass all run here.
    """
    if update_runsc:
        return _run_setup_update_runsc(ctx)

    emit_distro_gate(is_tty=_stdin_is_tty(), assume_yes=yes)

    extras = selected_extras(flags)
    phases = cli_flow.build_phase_list(extras)

    plan = run_plan_pass(phases, ctx)
    for line in cli_flow.render_plan(phases, plan):
        console.print(line, markup=False)
    tally = cli_flow.tally_plan(plan)
    console.print(cli_flow.plan_summary_line(tally), markup=False)

    if dry_run:
        return 0

    decision = cli_flow.decide_gate(
        plan, is_tty=_stdin_is_tty(), assume_yes=yes
    )

    if decision.outcome == cli_flow.GateOutcome.NOTHING_TO_APPLY:
        console.print("Nothing to apply. Setup is complete.", markup=False)
        return 0

    if decision.outcome == cli_flow.GateOutcome.REFUSED:
        for line in cli_flow.refusal_lines(phases, plan):
            console.print(line, markup=False)
        console.print("Setup will not enter the apply pass.", markup=False)
        return 1

    if decision.outcome == cli_flow.GateOutcome.NON_TTY_NEEDS_YES:
        console.print(
            "non-interactive context requires --yes flag to apply mutations",
            style="red",
            markup=False,
        )
        return 1

    if decision.outcome == cli_flow.GateOutcome.PROMPT:
        response = input("Proceed with apply? [y/N]: ")
        if not cli_flow.prompt_response_proceeds(response):
            console.print(
                "aborted by operator (n). No mutations applied.",
                markup=False,
            )
            return 0

    apply_outcomes = run_apply_pass(phases, ctx)
    for line in cli_flow.summarize_apply(phases, apply_outcomes):
        console.print(line, markup=False)
    return 1 if cli_flow.apply_pass_failed(apply_outcomes) else 0


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

    # Init-time auth mode probe (D5). This is a *probe* callsite: it must
    # branch (reachable → continue; unreachable/timeout → guidance + exit 1),
    # not crash. Per Q8 the probe-style entry point is ``core.dispatch.probe``,
    # which collapses the boundary-crossing outcome into a typed
    # ``ProbeOutcome`` (``ok`` / ``timed_out`` / ``message``). The timeout
    # branch preserves the exact original message; the non-timeout failure
    # branch surfaces ``ProbeOutcome.message`` — the prior non-zero (stderr)
    # and ``FileNotFoundError`` ("command not found on PATH") cases reach it
    # as distinct ``SandboxExecutionError`` texts (the sterile ``Executor``
    # wraps both), so ``message`` naturally distinguishes them in the
    # operator-facing detail.
    if not dry_run:
        probe_host_config = minimal_host_config(resolved_user, resolved_auth)
        probe_outcome = dispatch.probe("auth-probe", [], probe_host_config, timeout=5)
        if not probe_outcome.ok:
            if probe_outcome.timed_out:
                _emit_auth_probe_failure(resolved_auth, resolved_user, "probe timed out after 5 seconds")
            else:
                _emit_auth_probe_failure(resolved_auth, resolved_user, probe_outcome.message)
            raise typer.Exit(code=1)

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

        # Compose-project-name collision pre-flight (per cli-doctor's
        # "Init pre-flight includes compose_project_name_collision"). The
        # auth probe above proved machinectl_reachable; call the check
        # directly and surface failure.
        collision_result = check_compose_project_name_collision(
            resolved_user, distro, auth_mode=resolved_auth
        )
        if collision_result.status == "fail":
            render_results([collision_result], console=console)
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
            console.print(f"  Workspace [{rich_escape(ws.name)}]: {origin} → {ws.path}")
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
    no_handover: bool = typer.Option(
        False,
        "--no-handover",
        help="Skip the interactive admin-shell handover; print attach hint and return.",
    ),
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
    if _warm_check(instance_dir, host_user, auth):
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
    dev_user = os.environ.get("USER")
    ws_paths = [ws.path for _, ws in sorted(config.workspaces.items())]
    try:
        # Phase 2: IPAM
        base_index = _phase_ipam(inst)
        console.print("✓ IPAM — network allocation complete")

        # Phase 3a: workspace shared-group recipe — runs BEFORE Phase-3b named-ACL
        # grants so chmod 2770 lands on a non-extended-ACL inode and group::
        # propagates correctly (the "Workspace Shared-Group Phase Ordering"
        # requirement; closes the bug class fixed by temp commit 6f1831e).
        try:
            for ws_path in ws_paths:
                _phase_workspace_shared_group(ws_path, host_settings, dev_user, auth)
        except WorkspaceBridgeGroupMissingError as exc:
            console.print(
                f"[FATAL] {exc}\nRun `sandbox doctor` for setup commands.",
                style="red bold",
                markup=False,
            )
            if lock_fd is not None:
                _release_lock(lock_fd)
            raise typer.Exit(code=1) from None
        console.print("✓ Workspace — shared-group recipe applied")

        # Phase 3b: ACL grants (Pattern A) — fan out across [workspaces]. Runs
        # BEFORE _phase_credentials so the default ACL on `secrets/` is in place
        # when generate_ssh_keypair() opens new files inside it; the named
        # entry `u:<host_user>:r` then inherits onto each freshly-created
        # secret without an after-the-fact chgrp/chown (the "Secrets Inherit
        # Daemon-Readable Default ACL" requirement, closing finding 8.D
        # alternative #1).
        acl_granted = True  # set BEFORE grant — handles partial grants (D7)
        _phase_acl_grant(instance_dir, host_user, ws_paths, dev_user, auth)
        console.print("✓ ACL — filesystem permissions granted")

        # Phase 4: Credentials.
        proxy_password = _phase_credentials(
            instance_dir,
            core_ipc_ip=derive_static_ips(base_index)["core_ipc_ip"],
        )
        console.print("✓ Credentials — proxy auth + SSH keypairs configured")

        # Phase 5a: Hydration. WorkspaceBridgeGroupMissingError is caught by
        # _phase_workspace_shared_group above (it runs first); when ws_paths
        # is empty AND hydrate raises, the outer SandboxExecutionError
        # handler catches it (WorkspaceBridgeGroupMissingError subclass) and
        # surfaces the [FATAL] message without the doctor hint — acceptable
        # for the empty-workspaces edge case.
        _phase_hydrate(config, base_index, proxy_password, instance_dir, host_settings)
        console.print("✓ Hydration — templates rendered")

        # Phase 5b: setfacl-as-owner the helper-cp source files (config/<subdir>
        # files written by hydrate + secrets/ files written by credentials)
        # AND every dev-created file the daemon reads in place (compose.yml
        # + conditional extras per DAEMON_READ_DIRECT_FILES). Required
        # because `write_restricted`'s fchmod zeroes ACL mask, masking
        # out any inherited named entry; per-file setfacl recomputes
        # mask. Closes the "Helper-CP Source Files Daemon-Readable
        # Pre-Recipe" requirement.
        _phase_grant_post_hydrate_daemon_read(instance_dir, host_user)
        console.print("✓ ACL — post-hydrate daemon-readable files")

        # Phase 6a: helper-mkdir+chown for cache/log leaves
        _phase_helper_mkdir_chown_cache_log(instance_dir, host_user, auth, dev_user)
        console.print("✓ Cache/log — leaves chowned to consumer subuid")

        # Phase 6b: helper-cp+chown for ro single-file mounts (replaces credential-ownership)
        _phase_helper_cp_chown_ro_files(instance_dir, host_user, auth)
        console.print("✓ Ownership — ro config files converged")

        # Phase 6: Compose up (D-5 — spinner for long-running phase).
        # ComposeUpAction routes through core.dispatch.invoke, which needs the
        # full HostConfig for operator-side compose-state resolution (Q6).
        with console.status("⟳ Compose — starting containers…"):
            _phase_compose_up(inst, instance_dir, _resolve_full_host_config())
        console.print("✓ Compose — containers healthy")

    except (IPAMExhaustedError, SandboxExecutionError) as e:
        console.print(f"[FATAL] {e}", style="red bold", markup=False)
        # ACL cleanup on failure (Decision 4 of acl-ownership-recipes):
        # named-ACL grants from Phase 5 are revoked here. Helper-recipe
        # mutations (subuid chowns on cache/log/ro-files; chgrp+chmod+default
        # ACL on the workspace) are NOT reverted — they're persistent state
        # that survives intermediate stop/start cycles by design.
        if acl_granted:
            acl_warnings = _revoke_acls(instance_dir, host_user, ws_paths, auth)
            for w in acl_warnings:
                console.print(f"⚠ {w}", style="yellow")
        if lock_fd is not None:
            _release_lock(lock_fd)
        raise typer.Exit(code=1) from None

    # Phase 7: Handover — release lock first
    if lock_fd is not None:
        _release_lock(lock_fd)

    if no_handover or not _stdin_is_tty():
        console.print(f"Sandbox '{inst}' started. Attach with: sandbox attach {inst}")
        return

    # Default-handover lands in core (not admin) via the canonical
    # tlog-rec → ssh → ProxyCommand → /fwd path (admin-reframe D1).
    # When N=1 we default to the single workspace; when N>1 we print
    # the attach hint and return without handing over (the operator
    # picks a workspace explicitly via `sandbox attach <inst> <ws>`).
    workspace_names = sorted(config.workspaces.keys())
    if len(workspace_names) != 1:
        console.print(
            f"Sandbox '{inst}' started. Multiple workspaces — attach with: "
            f"sandbox attach {inst} <ws> (one of: {', '.join(workspace_names)})."
        )
        return
    ws = workspace_names[0]

    # Re-resolve the full HostConfig for `_build_attach_argv` (the start
    # command earlier resolved only HostSettings via `_resolve_host_settings`).
    host_config = _resolve_full_host_config()
    console.print("→ Handing over to core via ssh-through-admin")
    completed = subprocess.run(_build_attach_argv(inst, ws, host_config), check=False)
    raise typer.Exit(code=completed.returncode)


@app.command()
def stop(
    inst: str = typer.Argument(..., help="Instance name"),
    clean: bool = False,
) -> None:
    """Stop the sandbox."""
    _require_per_user_state_initialized()

    instance_dir = _lookup_instance_or_exit(inst)
    config = _load_config(instance_dir)
    host_user, auth = _resolve_host_config()

    # Warm check
    if not _warm_check(instance_dir, host_user, auth):
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

    # Teardown sequence — compose down → unlink helper-cp-managed →
    # revoke named-ACLs. Shared with `destroy`'s D5 phase via
    # `_phase_stop_teardown` (cluster 3 — Teardown Sequence requirement).
    ws_paths = [ws.path for _, ws in sorted(config.workspaces.items())]
    for w in _phase_stop_teardown(
        instance_dir, host_user, config, ws_paths, volumes=clean, auth=auth
    ):
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
    host_config = _resolve_full_host_config()
    host_user = host_config.host.docker_unprivileged_user
    auth = host_config.host.machinectl_authentication

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
    if not _warm_check(instance_dir, host_user, auth):
        console.print(f"Sandbox '{inst}' is not running. Use 'sandbox start {inst}' to launch.")
        raise typer.Exit(code=1)

    # PTY handover via tlog-rec → ssh → ProxyCommand → /fwd (admin-reframe D1).
    # No hydration, no credentials, no locking, no IPAM mutation.
    completed = subprocess.run(_build_attach_argv(inst, ws, host_config), check=False)
    raise typer.Exit(code=completed.returncode)


def _resolve_backup_workspaces_spec(
    spec: str,
    available: set[str],
) -> list[str]:
    """Resolve the ``--backup-workspaces=<spec>`` value to a list of workspace
    names to back up. Empty list means "back up nothing".

    Forms: ``all`` | ``none`` | ``<csv>``. Rejects ``all,foo`` combinations
    and unknown names in csv. Callers handle the no-flag case (``None``)
    separately by routing into TTY/non-TTY paths.
    """
    parts = [p.strip() for p in spec.split(",")]
    if "all" in parts and len(parts) > 1:
        raise typer.BadParameter("cannot combine 'all' with named workspaces")
    if parts == ["all"]:
        return sorted(available)
    if parts == ["none"]:
        return []
    if "none" in parts:
        raise typer.BadParameter("cannot combine 'none' with named workspaces")
    unknown = [p for p in parts if p not in available]
    if unknown:
        joined = ", ".join(unknown)
        raise typer.BadParameter(f"workspace(s) not found in instance: {joined}")
    return parts


def _prompt_backup_selection(workspaces: list[str]) -> list[str]:
    """TTY interactive: ask per-workspace whether to back up. Returns the
    selected names. Implementation uses ``typer.confirm`` per workspace as a
    portable substitute for the spec's "Rich toggleable list"."""
    selected: list[str] = []
    for name in workspaces:
        if typer.confirm(f"Back up workspace {name!r}?", default=False):
            selected.append(name)
    return selected


@app.command()
def destroy(
    inst: str = typer.Argument(..., help="Instance name"),
    force: bool = False,
    backup_workspaces: str | None = typer.Option(
        None,
        "--backup-workspaces",
        help="Backup spec: 'all' | 'none' | comma-separated workspace names",
    ),
) -> None:
    """Permanently destroy a sandbox instance.

    Phases (per `cli-destroy` "Phase Order Preserves Recoverability Through
    Backup"). D5 onward is irreversible:

    D1  confirmation (typed name) unless --force; backup-set selection
    D2  acquire state.lock (briefly) + <inst>.backup.lock (held)
    D3  compose down (REVERSIBLE)
    D4  per-workspace backup (any failure aborts: partial retained, no
        irreversible mutation)
    D5  compose down -v (IRREVERSIBLE from here)
    D6  ACL revoke (per workspace)
    D7  rmtree(instances/<inst>/)
    D8  rmtree(workspaces/<inst>/<ws>/) for ALL workspaces
    D9  rmdir(workspaces/<inst>/) if empty
    D10 IPAM release + registry remove
    D11 release locks
    """
    _require_per_user_state_initialized()

    instance_dir = _lookup_instance_or_exit(inst)

    # Prefix guard — before anything else.
    instances_prefix = str(sandbox_ai_home() / "instances") + os.sep
    if not instance_dir.startswith(instances_prefix):
        console.print(
            "[FATAL] Instance directory path fails prefix guard. Aborting.",
            style="red bold",
            markup=False,
        )
        raise typer.Exit(code=1)

    config = _load_config(instance_dir)
    host_user, auth = _resolve_host_config()
    available = set(config.workspaces.keys())

    # D1: Confirmation + backup-set selection.
    if not force:
        console.print(f"WARNING: This permanently deletes sandbox {inst!r} and all its state.")
        ws_summary = ", ".join(sorted(available)) or "<none>"
        console.print(f"         Workspaces affected: {ws_summary}")
        typed_name = typer.prompt("Type the sandbox name to confirm")
        if typed_name != inst:
            console.print("Aborted.")
            return

    if backup_workspaces is not None:
        backup_set = _resolve_backup_workspaces_spec(backup_workspaces, available)
    elif _stdin_is_tty():
        backup_set = _prompt_backup_selection(sorted(available))
    else:
        console.print(
            "destroy in non-interactive mode requires --backup-workspaces=...",
            style="red",
        )
        raise typer.Exit(code=1)

    # D2: Acquire state.lock briefly (gate); refuse if backup.lock held; then
    # acquire backup.lock and release state.lock for the long phases.
    try:
        gate_lock_fd = _acquire_state_lock(instance_dir)
    except BlockingIOError:
        console.print(
            "Another sandbox operation is already in progress for this instance.",
            style="red",
        )
        raise typer.Exit(code=1) from None
    if is_backup_lock_held(inst):
        _release_lock(gate_lock_fd)
        console.print(
            f"Backup in progress for {inst!r}; wait or `sandbox doctor` to inspect.",
            style="red",
        )
        raise typer.Exit(code=1)

    with acquire_backup_lock(inst):
        _release_lock(gate_lock_fd)

        # NOTE: the daemon's `u:<host_user>:r` ACL on `.sandbox.env` is
        # `granted-once, persistent` (cluster 3 — Environment File Read ACL),
        # so it survives a prior `sandbox stop` and no defensive re-grant is
        # needed before compose-down here.

        # D3: compose down (REVERSIBLE).
        try:
            _compose_down(host_user, config, volumes=False, auth=auth)
        except SandboxExecutionError as e:
            console.print(f"⚠ Compose down warning: {e}", style="yellow")

        # D4: per-workspace backup. Abort destroy on any failure — instance
        # remains recoverable via `sandbox start <inst>`.
        for ws_name in backup_set:
            target = config.workspaces[ws_name]
            try:
                create_backup(
                    instance_name=inst,
                    workspace_name=ws_name,
                    source_path=target.path,
                    source_bootstrap_mode=target.bootstrap_mode.value,
                    dev_primary_gid=os.getgid(),
                    acquire_lock=False,  # we already hold backup.lock
                )
            except BackupError as exc:
                console.print(
                    f"Backup of workspace {ws_name!r} failed: {exc}\n"
                    f"Destroy aborted; partial retained for diagnosis. "
                    f"Run `sandbox start {inst}` to resume or fix and retry.",
                    style="red",
                )
                raise typer.Exit(code=1) from None

        # D5: compose down -v (IRREVERSIBLE).
        try:
            lock_fd = _acquire_state_lock(instance_dir)
        except BlockingIOError:
            console.print(
                "Another sandbox operation is already in progress for this instance.",
                style="red",
            )
            raise typer.Exit(code=1) from None

        try:
            # D5+D6: shared teardown sequence with `stop` (cluster 3 —
            # Teardown Sequence). compose down -v + unlink helper-cp-managed
            # + named-ACL revoke, in that ordering. compose-down failures are
            # demoted to warnings here (destroy proceeds with rmtree
            # regardless), unlike `stop` which propagates.
            ws_paths = [ws.path for _, ws in sorted(config.workspaces.items())]
            try:
                teardown_warnings = _phase_stop_teardown(
                    instance_dir,
                    host_user,
                    config,
                    ws_paths,
                    volumes=True,
                    auth=auth,
                )
            except SandboxExecutionError as e:
                console.print(f"⚠ Compose teardown warning: {e}", style="yellow")
                # Compose-down failed but the rest of the destroy MUST proceed
                # (rmtree is irreversible by design). Run the post-compose
                # phases separately so warnings still surface.
                teardown_warnings = _phase_stop_unlink_consumer_files(instance_dir, host_user)
                teardown_warnings.extend(_revoke_acls(instance_dir, host_user, ws_paths, auth))
            for w in teardown_warnings:
                console.print(f"⚠ {w}", style="yellow")

            # D7: rmtree(instances/<inst>/).
            try:
                shutil.rmtree(instance_dir)
            except FileNotFoundError:
                pass

            # D8: rmtree workspace trees (regardless of backup status).
            for ws in config.workspaces.values():
                try:
                    shutil.rmtree(ws.path)
                except FileNotFoundError:
                    pass

            # D9: rmdir workspaces/<inst>/ if empty.
            inst_workspaces_dir = sandbox_ai_home() / "workspaces" / inst
            try:
                inst_workspaces_dir.rmdir()
            except (FileNotFoundError, OSError):
                pass

            # D10: state cleanup — IPAM and registry, fault-isolated.
            try:
                IPAMLedger().release(inst)
            except Exception as e:
                console.print(f"⚠ IPAM release warning: {e}", style="yellow")
            try:
                InstanceRegistry().remove(inst)
            except Exception as e:
                console.print(f"⚠ Registry cleanup warning: {e}", style="yellow")
        finally:
            # D11: release state.lock; backup.lock context manager releases on exit.
            _release_lock(lock_fd)

    console.print(f"Sandbox {inst!r} permanently destroyed. IPAM slot freed for reuse.")


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
                markup=False,
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
    host_settings = _resolve_host_settings()
    host_user = host_settings.docker_unprivileged_user
    auth = host_settings.machinectl_authentication

    # Container status
    containers = _container_status(instance_dir, host_user, config, auth)
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
                    "admin": ips.get("admin_ipc_ip", ""),
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
            isolated, core_proxy, dns, egress, ipc = derive_subnets(slot)
            console.print(f"\n[bold]IPAM[/bold] slot {slot}")
            console.print(f"  Isolated:    {isolated}")
            console.print(f"  Core Proxy:  {core_proxy}")
            console.print(f"  DNS:         {dns}")
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
    host_user, auth = _resolve_host_config()
    if _warm_check(instance_dir, host_user, auth):
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

        # Populate --copy workspaces via the rsync recipe, then normalize the
        # workspace root mode to 0700. rsync `-a` preserves source mode, which
        # would silently inherit a `0775` (group/world-readable) source onto the
        # workspace and undermine the privacy default the `--empty` path enforces
        # via `mkdir(..., mode=0o700)`.
        for ws in new_specs:
            if ws.bootstrap_mode == "copy" and ws.source is not None:
                copy_workspace(ws.source, ws.path)
                os.chmod(ws.path, 0o700)

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

    # Refuse to leave the instance with zero workspaces. The check precedes
    # the --backup/--purge branching so the refusal is identical regardless
    # of mode (no backup directory, no rsync, no rmtree, no sandbox.toml
    # mutation). Schema's min_length=1 invariant is preserved as a runtime
    # contract — operators who want a "blank" instance use `sandbox destroy`.
    if len(config.workspaces) == 1 and ws_name in config.workspaces:
        console.print(
            f"Cannot remove the last workspace from {inst!r}. "
            f"Add a replacement workspace first "
            f"('sandbox workspace add {inst} --empty <name>' or '--copy <name>=<path>'), "
            f"or use 'sandbox destroy {inst}' to remove the instance entirely.",
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


@workspace_app.command("restore")
def workspace_restore(
    inst: str = typer.Argument(..., help="Destination instance name"),
    ws_name: str = typer.Argument(..., help="Destination workspace name"),
    from_spec: str | None = typer.Option(
        None,
        "--from",
        help="Backup spec: omit (latest by ws name), <src-inst>/<src-ws>, or <src-inst>/<src-ws>/<ts>",
    ),
) -> None:
    """Restore a backup into a stopped instance as a new workspace."""
    _require_per_user_state_initialized()
    _validate_name(ws_name, kind="workspace", max_len=_WORKSPACE_NAME_MAX)

    instance_dir = _lookup_instance_or_exit(inst)
    config = _load_config(instance_dir)
    if ws_name in config.workspaces:
        console.print(
            f"Workspace {ws_name!r} already exists in {inst!r}. "
            f"Run `sandbox workspace remove {inst} {ws_name}` first.",
            style="red",
        )
        raise typer.Exit(code=1)

    try:
        backup = resolve_backup_spec(from_spec, ws_name)
    except BackupSpecAmbiguousError as exc:
        console.print(str(exc), style="red")
        raise typer.Exit(code=1) from None
    except BackupSpecNotFoundError as exc:
        console.print(str(exc), style="red")
        raise typer.Exit(code=1) from None

    _require_instance_stopped(inst, instance_dir)
    _refuse_if_backup_in_progress(inst)

    try:
        lock_fd = _acquire_state_lock(instance_dir)
    except BlockingIOError:
        console.print(
            "Another sandbox operation is already in progress for this instance.",
            style="red",
        )
        raise typer.Exit(code=1) from None

    try:
        new_path = restore_backup(backup, inst, ws_name)
        merged = [
            WorkspaceSpec(
                name=name,
                bootstrap_mode=ws.bootstrap_mode.value,
                source=ws.source,
                path=ws.path,
            )
            for name, ws in config.workspaces.items()
        ] + [
            WorkspaceSpec(
                name=ws_name,
                bootstrap_mode="copy",
                source=str(backup.path),
                path=str(new_path),
            )
        ]
        mutate_workspaces(instance_dir, merged)
    finally:
        _release_lock(lock_fd)

    spec_id = f"{backup.source_instance}/{backup.source_workspace}/{backup.timestamp}"
    console.print(f"Restored backup {spec_id} → {inst}/{ws_name}.")


def _format_age(timestamp: str) -> str:
    """Format a YYYY-MM-DD-HH-MM-SS timestamp as a coarse age string.

    Used for the ``workspace list`` Backups column. Format follows the
    convention of "<n>d ago" / "<n>h ago" / "<n>m ago" with the largest
    sensible unit.
    """
    import datetime as _dt

    try:
        ts = _dt.datetime.strptime(timestamp, "%Y-%m-%d-%H-%M-%S").replace(tzinfo=_dt.UTC)
    except ValueError:
        return "unknown"
    delta = _dt.datetime.now(tz=_dt.UTC) - ts
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


@workspace_app.command("list")
def workspace_list(
    inst: str = typer.Argument(..., help="Instance name"),
    no_backups: bool = typer.Option(False, "--no-backups", help="Suppress the Backups section"),
    json_out: bool = typer.Option(False, "--json", help="Emit structured JSON output"),
) -> None:
    """List live workspaces and (by default) available backups for an instance."""
    _require_per_user_state_initialized()

    instance_dir = _lookup_instance_or_exit(inst)
    config = _load_config(instance_dir)

    backups = []
    if not no_backups:
        backups = list_backups(BackupFilter(source_instance=inst))

    if json_out:
        payload: dict[str, list[dict[str, object]]] = {
            "workspaces": [
                {
                    "name": name,
                    "bootstrap_mode": ws.bootstrap_mode.value,
                    "path": ws.path,
                }
                for name, ws in sorted(config.workspaces.items())
            ],
            "backups": [
                {
                    "id": f"{b.source_instance}/{b.source_workspace}/{b.timestamp}",
                    "source_instance": b.source_instance,
                    "source_workspace": b.source_workspace,
                    "timestamp": b.timestamp,
                    "size_bytes": b.size_bytes,
                }
                for b in backups
            ],
        }
        console.print_json(_json.dumps(payload))
        return

    live_table = Table(title=f"Live workspaces ({inst})")
    live_table.add_column("NAME")
    live_table.add_column("MODE")
    live_table.add_column("PATH")
    for name, ws in sorted(config.workspaces.items()):
        live_table.add_row(name, ws.bootstrap_mode.value, ws.path)
    console.print(live_table)

    if no_backups:
        return

    backup_table = Table(title=f"Backups ({len(backups)})")
    backup_table.add_column("ID")
    backup_table.add_column("SIZE")
    backup_table.add_column("AGE")
    for b in backups:
        size_kb = b.size_bytes // 1024
        size_str = f"{size_kb} KB" if size_kb < 1024 else f"{size_kb // 1024} MB"
        backup_table.add_row(
            f"{b.source_instance}/{b.source_workspace}/{b.timestamp}",
            size_str,
            _format_age(b.timestamp),
        )
    console.print(backup_table)


if __name__ == "__main__":
    app()
