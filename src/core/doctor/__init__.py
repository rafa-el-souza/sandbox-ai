"""Doctor module: host readiness diagnostics for sandbox operation.

Provides 16 diagnostic checks across 4 independent chains:
- Chain 1 (privilege boundary, 10 checks): sudo -> machinectl -> user -> machined
  -> reachable -> docker -> rootless -> runsc -> runsc_runtimeargs -> host_uds
- Chain 2 (filesystem, 3 checks): setfacl → ACL support → ancestor traverse
- Chain 3 (repo integrity, 2 checks): tooling plane, state dir (independent)
- Chain 4 (supply chain, 1 check): image_digests (depends on docker_available)
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
from collections import defaultdict, deque
from typing import TYPE_CHECKING

from core.doctor.checks.filesystem import _ACL_PROBE_FAILURES as _ACL_PROBE_FAILURES
from core.doctor.checks.filesystem import _has_acl_exec as _has_acl_exec
from core.doctor.checks.filesystem import check_acl_support as check_acl_support
from core.doctor.checks.filesystem import check_ancestor_traverse as check_ancestor_traverse
from core.doctor.checks.filesystem import check_setfacl as check_setfacl
from core.doctor.checks.per_user_tree import check_legacy_cwd_files as check_legacy_cwd_files
from core.doctor.checks.per_user_tree import check_legacy_registry_shape as check_legacy_registry_shape
from core.doctor.checks.per_user_tree import (
    check_legacy_sandboxes_dir_detected as check_legacy_sandboxes_dir_detected,
)
from core.doctor.checks.per_user_tree import (
    check_legacy_workspace_in_user_project_root as check_legacy_workspace_in_user_project_root,
)
from core.doctor.checks.per_user_tree import check_per_user_tree_exists as check_per_user_tree_exists
from core.doctor.checks.per_user_tree import check_per_user_tree_mode as check_per_user_tree_mode
from core.doctor.checks.privilege_boundary import (
    check_compose_project_name_collision as check_compose_project_name_collision,
)
from core.doctor.checks.privilege_boundary import check_docker_available as check_docker_available
from core.doctor.checks.privilege_boundary import check_docker_rootless as check_docker_rootless
from core.doctor.checks.privilege_boundary import check_host_uds as check_host_uds
from core.doctor.checks.privilege_boundary import check_machinectl as check_machinectl
from core.doctor.checks.privilege_boundary import check_machinectl_reachable as check_machinectl_reachable
from core.doctor.checks.privilege_boundary import check_runsc_registered as check_runsc_registered
from core.doctor.checks.privilege_boundary import check_runsc_runtimeargs as check_runsc_runtimeargs
from core.doctor.checks.privilege_boundary import check_sudo as check_sudo
from core.doctor.checks.privilege_boundary import check_systemd_machined as check_systemd_machined
from core.doctor.checks.privilege_boundary import check_user_exists as check_user_exists
from core.doctor.checks.repo_integrity import _UNCONDITIONAL_FILES as _UNCONDITIONAL_FILES
from core.doctor.checks.repo_integrity import _resource_files as _resource_files
from core.doctor.checks.repo_integrity import check_state_dir_writable as check_state_dir_writable
from core.doctor.checks.repo_integrity import check_tooling_plane as check_tooling_plane
from core.doctor.checks.supply_chain import check_image_digests as check_image_digests
from core.doctor.types import _BINARY_PACKAGES as _BINARY_PACKAGES
from core.doctor.types import Check as Check
from core.doctor.types import CheckResult as CheckResult
from core.doctor.types import detect_distro as detect_distro
from core.doctor.types import get_install_cmd as get_install_cmd
from core.host_config import (
    HostConfig,
    HostSettings,
    MachinectlAuth,
    NoFreeGidInSubgidRangeError,
    NoSubgidRangeError,
    NoSubuidRangeError,
    SubgidOutOfRangeError,
    WorkspaceBridgeGroupMissingError,
    autodetect_workspace_bridge_gid_recommendation,
    host_id_for_in_container,
    sandbox_ai_home,
    workspace_bridge_gid,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from rich.console import Console


# ─── Section 8: Check Runner ────────────────────────────────────────────────


# ─── Acl-Ownership-Recipes Checks ──────────────────────────────────────────


def _load_host_settings_or_skip(check_name: str) -> HostSettings | CheckResult:
    """Helper: load HostSettings or return a CheckResult skip if absent."""
    try:
        return HostConfig.from_toml().host
    except FileNotFoundError:
        return CheckResult(
            status="skip",
            name=check_name,
            detail="sandbox-ai.toml not found; run `sandbox init` first",
            category="Workspace Bridge",
        )


def check_workspace_bridge_group_exists(host_user: str, distro: str | None) -> CheckResult:
    """Validate the workspace bridge group exists at a gid in the daemon's subgid range."""
    del distro
    settings_or_skip = _load_host_settings_or_skip("workspace bridge group")
    if isinstance(settings_or_skip, CheckResult):
        return settings_or_skip
    host = settings_or_skip
    name = f"workspace bridge group {host.workspace_bridge_group!r}"
    try:
        gid = workspace_bridge_gid(host)
    except WorkspaceBridgeGroupMissingError:
        try:
            recommended = autodetect_workspace_bridge_gid_recommendation(host_user)
            rec_str = str(recommended)
        except NoSubgidRangeError:
            rec_str = "<pick-a-gid-in-claude-sandbox-subgid-range>"
        except NoFreeGidInSubgidRangeError:
            rec_str = "<pick-a-gid-in-claude-sandbox-subgid-range>"
        remediation = (
            f"sudo groupadd -g {rec_str} {host.workspace_bridge_group} && "
            f"sudo usermod -aG {host.workspace_bridge_group} $USER && "
            "log out and back in"
        )
        return CheckResult(
            status="fail",
            name=name,
            detail=f"group {host.workspace_bridge_group!r} does not exist on this host",
            remediation=remediation,
            category="Workspace Bridge",
        )
    except SubgidOutOfRangeError as exc:
        return CheckResult(
            status="fail",
            name=name,
            detail=str(exc),
            remediation=(f"Recreate the bridge group at a gid within {host_user}'s /etc/subgid range"),
            category="Workspace Bridge",
        )
    return CheckResult(
        status="pass",
        name=name,
        detail=f"gid={gid}",
        category="Workspace Bridge",
    )


def check_dev_in_workspace_bridge_group(host_user: str, distro: str | None) -> CheckResult:
    """Validate dev's current process has the bridge gid in supplementary groups."""
    del distro
    import grp
    import pwd

    settings_or_skip = _load_host_settings_or_skip("dev in workspace bridge group")
    if isinstance(settings_or_skip, CheckResult):
        return settings_or_skip
    host = settings_or_skip
    try:
        bridge_gid = workspace_bridge_gid(host)
    except (WorkspaceBridgeGroupMissingError, SubgidOutOfRangeError, NoSubgidRangeError) as exc:
        return CheckResult(
            status="fail",
            name="dev in workspace bridge group",
            detail=str(exc),
            remediation="See `workspace bridge group` check",
            category="Workspace Bridge",
        )

    if bridge_gid in os.getgroups():
        return CheckResult(
            status="pass",
            name="dev in workspace bridge group",
            detail=f"current process supplementary groups include gid {bridge_gid}",
            category="Workspace Bridge",
        )

    current_user = pwd.getpwuid(os.getuid()).pw_name
    in_etc_group = bridge_gid in {g.gr_gid for g in grp.getgrall() if current_user in g.gr_mem}
    if in_etc_group:
        return CheckResult(
            status="fail",
            name="dev in workspace bridge group",
            detail=(
                f"User {current_user!r} is a member of {host.workspace_bridge_group!r} in "
                f"/etc/group but the current process's supplementary groups do not include gid "
                f"{bridge_gid}"
            ),
            remediation="Log out and log back in to refresh group membership",
            category="Workspace Bridge",
        )
    return CheckResult(
        status="fail",
        name="dev in workspace bridge group",
        detail=f"User {current_user!r} is not a member of {host.workspace_bridge_group!r}",
        remediation=f"sudo usermod -aG {host.workspace_bridge_group} {current_user} && relogin",
        category="Workspace Bridge",
    )


def check_subuid_resolver_works(host_user: str, distro: str | None) -> CheckResult:
    """Validate /etc/subuid is readable and host_user has a usable subuid range."""
    del distro
    try:
        host_uid = host_id_for_in_container(1000, host_user)
    except NoSubuidRangeError as exc:
        return CheckResult(
            status="fail",
            name="subuid resolver",
            detail=str(exc),
            remediation=(
                f"Add a /etc/subuid entry for {host_user} (rootless docker setup); "
                "see https://docs.docker.com/engine/security/rootless/"
            ),
            category="Workspace Bridge",
        )
    return CheckResult(
        status="pass",
        name="subuid resolver",
        detail=f"in-container uid 1000 → host uid {host_uid}",
        category="Workspace Bridge",
    )


def check_helper_image_pulled(host_user: str, distro: str | None) -> CheckResult:
    """Warn-only: helper container image is locally available."""
    del host_user, distro
    from core.hydration import IMAGE_REGISTRY

    image = IMAGE_REGISTRY["busybox_musl"].pinned
    docker_inspect_failures: tuple[type[BaseException], ...] = (FileNotFoundError, subprocess.TimeoutExpired)
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except docker_inspect_failures:
        return CheckResult(
            status="warn",
            name="helper image cached",
            detail="docker not reachable from current shell; will pull on first sandbox start",
            category="Workspace Bridge",
        )
    if result.returncode == 0:
        return CheckResult(
            status="pass",
            name="helper image cached",
            detail=f"{image} present locally",
            category="Workspace Bridge",
        )
    return CheckResult(
        status="warn",
        name="helper image cached",
        detail=f"{image} not in local cache; will be pulled on first sandbox start",
        category="Workspace Bridge",
    )


def _scan_instance_dirs() -> list[str]:
    """Return registered instance directories from ``<home>/state/instances.json``.

    Per change-5's name-keyed registry, each entry has shape
    ``{instance_dir, created_at}``. Iterating the registry is install-mode
    independent (closes change-4's deferred wheel-install behavior on
    ``secrets_hydrated_restrictively`` and ``pre_existing_instance_layout``).

    Returns the list of registered ``instance_dir`` paths that exist on disk,
    or ``[]`` if the registry is missing/empty/malformed.
    """
    state_path = sandbox_ai_home() / "state" / "instances.json"
    try:
        with open(state_path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    out: list[str] = []
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        inst_dir = entry.get("instance_dir")
        if isinstance(inst_dir, str) and os.path.isdir(inst_dir):
            out.append(inst_dir)
    return out


def check_secrets_hydrated_restrictively(host_user: str, distro: str | None) -> CheckResult:
    """Warn-only: scan registered instances' secrets/ for world-readable mode bits."""
    del host_user, distro
    instances = _scan_instance_dirs()
    leaks: list[str] = []
    for inst in instances:
        secrets_dir = os.path.join(inst, "secrets")
        if not os.path.isdir(secrets_dir):
            continue
        for fname in os.listdir(secrets_dir):
            fpath = os.path.join(secrets_dir, fname)
            try:
                mode = os.stat(fpath).st_mode & 0o777
            except OSError:
                continue
            if mode & 0o004:  # other::r--
                leaks.append(f"{fpath} (mode {mode:04o})")
    if leaks:
        sample = ", ".join(leaks[:3])
        return CheckResult(
            status="warn",
            name="secrets hydrated restrictively",
            detail=f"{len(leaks)} secret(s) world-readable: {sample}",
            remediation="sandbox destroy && sandbox init && sandbox start",
            category="Workspace Bridge",
        )
    return CheckResult(
        status="pass",
        name="secrets hydrated restrictively",
        detail="no world-readable secrets in registered instances",
        category="Workspace Bridge",
    )


def _default_uid_for_path(path: str) -> int:
    """Return ``os.stat(path).st_uid``. Default ownership resolver injected
    into ``check_pre_existing_instance_layout``. Raises ``OSError`` for
    missing paths (the absent-leaf branch in the check relies on this)."""
    return os.stat(path).st_uid


def check_pre_existing_instance_layout(
    host_user: str,
    distro: str | None,
    uid_for_path: Callable[[str], int] | None = None,
) -> CheckResult:
    """Warn-only: detect cache/log leaves whose ownership is inconsistent with the
    post-Change-D scaffold-vs-helper boundary.

    Three-state semantics per ``cli-doctor``'s "Pre-Existing Instance Layout
    Check" requirement:

    - Leaf absent — pass silently. The expected state for a freshly-init'd
      instance that has not yet been started; the helper recipe creates the
      leaf on first start (per ``orchestrator-volumes``'s
      "Scaffold-vs-Helper Boundary").
    - Leaf present and consumer-subuid-owned — pass silently. The helper
      recipe ran successfully on a prior start.
    - Leaf present and not consumer-subuid-owned (typically dev-owned from
      a pre-Change-D scaffold) — warn with a per-leaf
      ``rm -rf <leaf>`` remediation. Re-running ``sandbox start`` after the
      operator removes the affected leaves lets the helper recipe recreate
      them as claude-sandbox-owned and chown to the consumer subuid.

    The remediation is per-leaf so a mixed-state instance (some leaves
    helper-owned, some still dev-owned from legacy state) reports only the
    affected leaves, not the entire inventory.

    The per-leaf ownership lookup is supplied via ``uid_for_path`` (default:
    ``_default_uid_for_path``, which wraps ``os.stat``). Tests inject a
    deterministic resolver to avoid monkeypatching ``os.stat`` — the resolver
    MUST raise ``OSError`` for absent paths so the absent-leaf branch is
    reached as in the production path.
    """
    del distro
    resolver = uid_for_path if uid_for_path is not None else _default_uid_for_path
    # Cache/log leaf inventory per orchestrator-volumes' "Cache/Log Leaf
    # Inventory" requirement. Stays in sync with that spec.
    cache_log_leaves = (
        "cache/core/.claude",
        "cache/admin/tmux_resurrect",
        "log/core",
        "log/admin",
    )
    try:
        consumer_subuid = host_id_for_in_container(1000, host_user)
    except NoSubuidRangeError:
        return CheckResult(
            status="skip",
            name="pre-existing instance layout",
            detail="cannot resolve subuid; see `subuid resolver` check",
            category="Workspace Bridge",
        )

    instances = _scan_instance_dirs()
    stale_paths: list[str] = []
    for inst in instances:
        for leaf in cache_log_leaves:
            leaf_path = os.path.join(inst, leaf)
            try:
                leaf_uid = resolver(leaf_path)
            except OSError:
                # Leaf absent — the expected post-Change-D state for an
                # instance that has not yet been started. Pass silently.
                continue
            if leaf_uid == consumer_subuid:
                # Helper recipe ran successfully on a prior start. Pass silently.
                continue
            # Leaf present but not consumer-owned — typically dev-owned from
            # pre-Change-D scaffold or a partial helper failure. Flag for
            # operator-targeted remediation.
            stale_paths.append(leaf_path)

    if stale_paths:
        sample = ", ".join(stale_paths[:3])
        remediation = "; ".join(f"rm -rf {p}" for p in stale_paths)
        return CheckResult(
            status="warn",
            name="pre-existing instance layout",
            detail=(
                f"{len(stale_paths)} cache/log leaf(s) present but not "
                f"consumer-subuid-owned: {sample}"
            ),
            remediation=remediation,
            category="Workspace Bridge",
        )
    return CheckResult(
        status="pass",
        name="pre-existing instance layout",
        detail="no stale cache/log leaf ownership detected",
        category="Workspace Bridge",
    )


def _read_registry_raw() -> dict[str, object]:
    """Return the raw parsed ``instances.json`` (for shape inspection).

    Returns an empty dict if missing or malformed. Used by checks that need to
    inspect keys directly (e.g., legacy_registry_shape).
    """
    state_path = sandbox_ai_home() / "state" / "instances.json"
    try:
        with open(state_path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _scan_instance_workspace_paths() -> list[tuple[str, str, str]]:
    """For each registered instance, parse sandbox.toml and return tuples of
    ``(instance_dir, workspace_name, workspace_path)`` for every workspace.

    Skips instances whose sandbox.toml is missing or malformed.
    """
    import tomllib

    out: list[tuple[str, str, str]] = []
    for inst_dir in _scan_instance_dirs():
        toml_path = os.path.join(inst_dir, "sandbox.toml")
        try:
            with open(toml_path, "rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        workspaces = data.get("workspaces", {})
        if not isinstance(workspaces, dict):
            continue
        for name, body in workspaces.items():
            if isinstance(body, dict) and isinstance(body.get("path"), str):
                out.append((inst_dir, name, body["path"]))
    return out


def check_backups_disk_pressure(host_user: str, distro: str | None) -> CheckResult:
    """Warn if ``_backups/`` exceeds 5 GB total or 50 entries."""
    del host_user, distro
    backups_root = sandbox_ai_home() / "workspaces" / "_backups"
    if not backups_root.is_dir():
        return CheckResult(
            status="pass",
            name="backups disk pressure",
            detail="no backups directory; nothing to measure",
            category="Workspace Bridge",
        )
    total_bytes = 0
    entry_count = 0
    for dirpath, _dn, filenames in os.walk(backups_root, followlinks=False):
        for name in filenames:
            try:
                total_bytes += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    for inst_dir in backups_root.iterdir():
        if not inst_dir.is_dir():
            continue
        for ws_dir in inst_dir.iterdir():
            if not ws_dir.is_dir():
                continue
            for ts_dir in ws_dir.iterdir():
                if ts_dir.is_dir() and not ts_dir.name.endswith(".partial"):
                    entry_count += 1
    threshold_bytes = 5 * 1024 * 1024 * 1024
    if total_bytes > threshold_bytes or entry_count > 50:
        size_gb = total_bytes / (1024**3)
        return CheckResult(
            status="warn",
            name="backups disk pressure",
            detail=f"_backups/ has {entry_count} entries totaling {size_gb:.2f} GB",
            remediation=f"Manually `rm -rf` stale entries under {backups_root}",
            category="Workspace Bridge",
        )
    return CheckResult(
        status="pass",
        name="backups disk pressure",
        detail=f"_backups/ has {entry_count} entries (under 5 GB / 50)",
        category="Workspace Bridge",
    )


def check_backups_partial_dirs_present(host_user: str, distro: str | None) -> CheckResult:
    """Warn if any ``*.partial/`` exists older than 1 hour under ``_backups/``."""
    del host_user, distro
    import time

    backups_root = sandbox_ai_home() / "workspaces" / "_backups"
    if not backups_root.is_dir():
        return CheckResult(
            status="pass",
            name="backups partial dirs",
            detail="no backups directory",
            category="Workspace Bridge",
        )
    now = time.time()
    stale: list[str] = []
    for dirpath, dirnames, _f in os.walk(backups_root, followlinks=False):
        for d in dirnames:
            if d.endswith(".partial"):
                full = os.path.join(dirpath, d)
                try:
                    mtime = os.lstat(full).st_mtime
                except OSError:
                    continue
                if now - mtime > 3600:
                    stale.append(full)
    if stale:
        sample = ", ".join(stale[:3])
        return CheckResult(
            status="warn",
            name="backups partial dirs",
            detail=f"{len(stale)} stale .partial dir(s) >1h old: {sample}",
            remediation="Manually `rm -rf` the .partial directories after confirming they are abandoned",
            category="Workspace Bridge",
        )
    return CheckResult(
        status="pass",
        name="backups partial dirs",
        detail="no stale .partial directories",
        category="Workspace Bridge",
    )


def check_dev_umask_workspace_friendly(host_user: str, distro: str | None) -> CheckResult:
    """Warn if at least one workspace is registered AND the dev umask is 0o022 or worse."""
    del host_user, distro
    if not _scan_instance_workspace_paths():
        return CheckResult(
            status="skip",
            name="dev umask workspace-friendly",
            detail="no workspaces registered; skipping umask check",
            category="Workspace Bridge",
        )
    saved = os.umask(0)
    os.umask(saved)
    if saved & 0o020 or saved & 0o002 == 0:
        # 0o022 (group write blocked) or stricter for "other" without group write.
        # The check: if "group write" is masked (saved & 0o020 != 0), warn.
        return CheckResult(
            status="warn",
            name="dev umask workspace-friendly",
            detail=f"dev umask {saved:04o} masks group-write; workspace files won't be group-writable for the agent",
            remediation="Add `umask 002` to your shell rc (~/.bashrc, ~/.zshrc) so files in workspaces land mode 0664",
            category="Workspace Bridge",
        )
    return CheckResult(
        status="pass",
        name="dev umask workspace-friendly",
        detail=f"dev umask {saved:04o} preserves group write",
        category="Workspace Bridge",
    )


def check_workspace_path_in_walker_boundary(host_user: str, distro: str | None) -> CheckResult:
    """Fail if any registered workspace.path matches the walker boundary list."""
    del host_user, distro
    from core.walker import BOUNDARY_PATHS

    offenders: list[str] = []
    for inst_dir, ws_name, ws_path in _scan_instance_workspace_paths():
        try:
            real = os.path.realpath(ws_path)
        except OSError:
            continue
        if real in BOUNDARY_PATHS:
            offenders.append(f"{os.path.basename(inst_dir)}/{ws_name} ({real})")
    if offenders:
        return CheckResult(
            status="fail",
            name="workspace path in walker boundary",
            detail=f"{len(offenders)} workspace(s) at boundary path: {', '.join(offenders[:3])}",
            remediation="`sandbox workspace remove --purge` and re-add at a safe path",
            category="Workspace Bridge",
        )
    return CheckResult(
        status="pass",
        name="workspace path in walker boundary",
        detail="all workspace paths are outside the walker boundary list",
        category="Workspace Bridge",
    )


def check_workspace_home_single_filesystem(host_user: str, distro: str | None) -> CheckResult:
    """Warn if ``<home>/`` and ``<home>/workspaces/`` live on different filesystems."""
    del host_user, distro
    home = sandbox_ai_home()
    workspaces = home / "workspaces"
    try:
        home_dev = os.stat(home).st_dev
        ws_dev = os.stat(workspaces).st_dev
    except FileNotFoundError:
        return CheckResult(
            status="pass",
            name="workspace home single filesystem",
            detail="home or workspaces dir absent; nothing to check",
            category="Workspace Bridge",
        )
    except OSError as exc:
        return CheckResult(
            status="skip",
            name="workspace home single filesystem",
            detail=f"stat failed: {exc}",
            category="Workspace Bridge",
        )
    if home_dev != ws_dev:
        return CheckResult(
            status="warn",
            name="workspace home single filesystem",
            detail=f"{home} and {workspaces} are on different filesystems",
            remediation=(
                "Workspace rename and atomic backup rename will fail with EXDEV. "
                "Consolidate the trees onto one filesystem."
            ),
            category="Workspace Bridge",
        )
    return CheckResult(
        status="pass",
        name="workspace home single filesystem",
        detail="home and workspaces share a filesystem",
        category="Workspace Bridge",
    )


def build_check_registry(auth_mode: MachinectlAuth = MachinectlAuth.SUDO) -> list[Check]:
    """Build the doctor check registry with auth-mode-aware machinectl checks.

    When ``auth_mode == MachinectlAuth.POLKIT``, the `sudo` binary check is
    omitted from the registry and the `machinectl_reachable` check no longer
    depends on `sudo`. The 7 machinectl-invoking checks are partial-bound with
    ``auth_mode`` so they construct command prefixes via ``machinectl_cmd()``.
    """
    is_sudo = auth_mode == MachinectlAuth.SUDO
    machinectl_reachable_deps = (
        ["sudo", "machinectl", "user_exists", "systemd_machined"]
        if is_sudo
        else ["machinectl", "user_exists", "systemd_machined"]
    )

    chain1: list[Check] = []
    if is_sudo:
        chain1.append(
            Check(
                id="sudo",
                name="sudo binary",
                category="Privilege Boundary",
                depends_on=[],
                run=check_sudo,
                remediation="",
            )
        )
    chain1.extend(
        [
            Check(
                id="machinectl",
                name="machinectl binary",
                category="Privilege Boundary",
                depends_on=[],
                run=check_machinectl,
                remediation="",
            ),
            Check(
                id="user_exists",
                name="unprivileged user",
                category="Privilege Boundary",
                depends_on=[],
                run=check_user_exists,
                remediation="",
            ),
            Check(
                id="systemd_machined",
                name="systemd-machined",
                category="Privilege Boundary",
                depends_on=["machinectl"],
                run=check_systemd_machined,
                remediation="",
            ),
            Check(
                id="machinectl_reachable",
                name="machinectl reachable",
                category="Privilege Boundary",
                depends_on=machinectl_reachable_deps,
                run=functools.partial(check_machinectl_reachable, auth_mode=auth_mode),
                remediation="",
            ),
            Check(
                id="docker_available",
                name="Docker available",
                category="Privilege Boundary",
                depends_on=["machinectl_reachable"],
                run=functools.partial(check_docker_available, auth_mode=auth_mode),
                remediation="",
            ),
            Check(
                id="docker_rootless",
                name="Docker rootless",
                category="Privilege Boundary",
                depends_on=["docker_available"],
                run=functools.partial(check_docker_rootless, auth_mode=auth_mode),
                remediation="",
            ),
            Check(
                id="runsc",
                name="gVisor runsc",
                category="Privilege Boundary",
                depends_on=["docker_available"],
                run=functools.partial(check_runsc_registered, auth_mode=auth_mode),
                remediation="",
            ),
            Check(
                id="runsc_runtimeargs",
                name="runsc runtimeArgs",
                category="Privilege Boundary",
                depends_on=["runsc"],
                run=functools.partial(check_runsc_runtimeargs, auth_mode=auth_mode),
                remediation="",
            ),
            Check(
                id="host_uds",
                name="--host-uds=none",
                category="Privilege Boundary",
                depends_on=["runsc"],
                run=functools.partial(check_host_uds, auth_mode=auth_mode),
                remediation="",
            ),
            Check(
                id="compose_project_name_collision",
                name="compose project name collision",
                category="Privilege Boundary",
                depends_on=["machinectl_reachable"],
                run=functools.partial(check_compose_project_name_collision, auth_mode=auth_mode),
                remediation="",
            ),
        ]
    )

    return [
        *chain1,
        # Chain 2: filesystem
        Check(
            id="setfacl",
            name="setfacl binary",
            category="Filesystem",
            depends_on=[],
            run=check_setfacl,
            remediation="",
        ),
        Check(
            id="acl_support",
            name="ACL support",
            category="Filesystem",
            depends_on=["setfacl"],
            run=check_acl_support,
            remediation="",
        ),
        Check(
            id="ancestor_traverse",
            name="ancestor traverse",
            category="Filesystem",
            depends_on=["acl_support"],
            run=check_ancestor_traverse,
            remediation="",
        ),
        # Chain 3: repo integrity
        Check(
            id="tooling_plane",
            name="tooling plane",
            category="Repo Integrity",
            depends_on=[],
            run=check_tooling_plane,
            remediation="",
        ),
        Check(
            id="state_dir",
            name="state dir writable",
            category="Repo Integrity",
            depends_on=[],
            run=check_state_dir_writable,
            remediation="",
        ),
        # Chain 4: supply chain
        Check(
            id="image_digests",
            name="image digests",
            category="Supply Chain",
            depends_on=["docker_available"],
            run=functools.partial(check_image_digests, auth_mode=auth_mode),
            remediation="",
        ),
        # Chain 5: per-user tree
        Check(
            id="per_user_tree_exists",
            name="per-user tree exists",
            category="Per-User Tree",
            depends_on=[],
            run=check_per_user_tree_exists,
            remediation="",
        ),
        Check(
            id="per_user_tree_mode",
            name="per-user tree mode",
            category="Per-User Tree",
            depends_on=["per_user_tree_exists"],
            run=check_per_user_tree_mode,
            remediation="",
        ),
        Check(
            id="legacy_cwd_files",
            name="legacy CWD files",
            category="Per-User Tree",
            depends_on=[],
            run=check_legacy_cwd_files,
            remediation="",
        ),
        # Chain 6: workspace bridge group + helper-recipe prereqs
        Check(
            id="workspace_bridge_group_exists",
            name="workspace bridge group",
            category="Workspace Bridge",
            depends_on=[],
            run=check_workspace_bridge_group_exists,
            remediation="",
        ),
        Check(
            id="dev_in_workspace_bridge_group",
            name="dev in workspace bridge group",
            category="Workspace Bridge",
            depends_on=["workspace_bridge_group_exists"],
            run=check_dev_in_workspace_bridge_group,
            remediation="",
        ),
        Check(
            id="subuid_resolver_works",
            name="subuid resolver",
            category="Workspace Bridge",
            depends_on=[],
            run=check_subuid_resolver_works,
            remediation="",
        ),
        Check(
            id="helper_image_pulled",
            name="helper image cached",
            category="Workspace Bridge",
            depends_on=[],
            run=check_helper_image_pulled,
            remediation="",
        ),
        Check(
            id="secrets_hydrated_restrictively",
            name="secrets hydrated restrictively",
            category="Workspace Bridge",
            depends_on=[],
            run=check_secrets_hydrated_restrictively,
            remediation="",
        ),
        Check(
            id="pre_existing_instance_layout",
            name="pre-existing instance layout",
            category="Workspace Bridge",
            depends_on=["subuid_resolver_works"],
            run=check_pre_existing_instance_layout,
            remediation="",
        ),
        Check(
            id="backups_disk_pressure",
            name="backups disk pressure",
            category="Workspace Bridge",
            depends_on=[],
            run=check_backups_disk_pressure,
            remediation="",
        ),
        Check(
            id="backups_partial_dirs_present",
            name="backups partial dirs",
            category="Workspace Bridge",
            depends_on=[],
            run=check_backups_partial_dirs_present,
            remediation="",
        ),
        Check(
            id="dev_umask_workspace_friendly",
            name="dev umask workspace-friendly",
            category="Workspace Bridge",
            depends_on=[],
            run=check_dev_umask_workspace_friendly,
            remediation="",
        ),
        Check(
            id="workspace_path_in_walker_boundary",
            name="workspace path in walker boundary",
            category="Workspace Bridge",
            depends_on=[],
            run=check_workspace_path_in_walker_boundary,
            remediation="",
        ),
        Check(
            id="workspace_home_single_filesystem",
            name="workspace home single filesystem",
            category="Workspace Bridge",
            depends_on=[],
            run=check_workspace_home_single_filesystem,
            remediation="",
        ),
        Check(
            id="legacy_sandboxes_dir_detected",
            name="legacy sandboxes dir detected",
            category="Per-User Tree",
            depends_on=[],
            run=check_legacy_sandboxes_dir_detected,
            remediation="",
        ),
        Check(
            id="legacy_workspace_in_user_project_root",
            name="legacy user_project_root field",
            category="Per-User Tree",
            depends_on=[],
            run=check_legacy_workspace_in_user_project_root,
            remediation="",
        ),
        Check(
            id="legacy_registry_shape",
            name="legacy registry shape",
            category="Per-User Tree",
            depends_on=[],
            run=check_legacy_registry_shape,
            remediation="",
        ),
    ]


def topological_sort(checks: list[Check]) -> list[Check]:
    """Topologically sort checks respecting depends_on declarations."""
    id_to_check = {c.id: c for c in checks}
    in_degree: dict[str, int] = {c.id: 0 for c in checks}
    adjacency: dict[str, list[str]] = defaultdict(list)

    for c in checks:
        for dep in c.depends_on:
            if dep in id_to_check:
                adjacency[dep].append(c.id)
                in_degree[c.id] += 1

    queue: deque[str] = deque(cid for cid, deg in in_degree.items() if deg == 0)
    sorted_ids: list[str] = []

    while queue:
        current = queue.popleft()
        sorted_ids.append(current)
        for neighbor in adjacency[current]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    return [id_to_check[cid] for cid in sorted_ids]


def run_checks(
    checks: list[Check],
    user: str,
    distro: str | None,
) -> list[CheckResult]:
    """Execute checks in topological order with cascading skip on failed deps."""
    ordered = topological_sort(checks)
    results: dict[str, CheckResult] = {}
    output: list[CheckResult] = []

    for check in ordered:
        # Check if any dependency failed
        failed_deps = [dep for dep in check.depends_on if dep in results and results[dep].status in ("fail", "skip")]

        if failed_deps:
            dep_names = ", ".join(failed_deps)
            result = CheckResult(
                status="skip",
                name=check.name,
                detail=f"skipped (requires: {dep_names})",
            )
        else:
            result = check.run(user, distro)

        results[check.id] = result
        output.append(result)

    return output


def run_check_subset(
    categories: list[str],
    user: str,
    distro: str | None,
    *,
    exclude_ids: set[str] | None = None,
    auth_mode: MachinectlAuth = MachinectlAuth.SUDO,
) -> list[CheckResult]:
    """Execute a filtered subset of doctor checks by category.

    Filters ``build_check_registry()`` by ``Check.category``, validates the
    cross-chain invariant (all ``depends_on`` references must resolve within
    the subset), then delegates to ``run_checks``.

    Args:
        categories: Category names to include.
        user: Unprivileged user to check.
        distro: Linux distribution name or None.
        exclude_ids: Optional set of check IDs to exclude from the subset.
            Excluded checks are removed *before* the cross-chain invariant
            validation. Checks that ``depends_on`` an excluded ID will be
            auto-skipped by the dependency engine.

    Raises:
        ValueError: If any ``depends_on`` reference in the filtered subset
            points to a check outside the subset.
    """
    if not categories:
        return []

    registry = build_check_registry(auth_mode)
    category_set = set(categories)
    excluded = exclude_ids or set()
    subset = [c for c in registry if c.category in category_set and c.id not in excluded]

    # Assert cross-chain invariant: every depends_on must resolve internally
    subset_ids = {c.id for c in subset}
    for check in subset:
        for dep in check.depends_on:
            if dep not in subset_ids and dep not in excluded:
                raise ValueError(
                    f"Check '{check.id}' depends on '{dep}' which is outside "
                    f"the subset (categories: {categories}). Cross-chain "
                    f"dependencies are not supported in subset execution."
                )

    return run_checks(subset, user, distro)


# ─── Section 9: Rich Output Renderer ────────────────────────────────────────


def render_results(
    results: list[CheckResult],
    *,
    console: Console | None = None,
) -> None:
    """Render check results using Rich with progressive disclosure."""
    from rich.console import Console as RichConsole
    from rich.text import Text

    if console is None:
        console = RichConsole()

    # Group by category
    grouped: dict[str, list[CheckResult]] = defaultdict(list)
    for r in results:
        cat = r.category or "General"
        grouped[cat].append(r)

    pass_count = sum(1 for r in results if r.status == "pass")
    fail_count = sum(1 for r in results if r.status == "fail")
    skip_count = sum(1 for r in results if r.status == "skip")
    warn_count = sum(1 for r in results if r.status == "warn")

    for category, checks in grouped.items():
        console.print(f"\n[bold]{category}[/bold]")
        for r in checks:
            if r.status == "pass":
                line = Text(f"  ✓ {r.name}", style="green")
                if r.detail:
                    line.append(f"  {r.detail}", style="dim")
                console.print(line)
            elif r.status == "fail":
                console.print(Text(f"  ✗ {r.name}", style="red bold"))
                console.print(f"    {r.detail}")
                if r.remediation:
                    console.print(f"    Fix: {r.remediation}", style="yellow")
                if r.doc_ref:
                    console.print(f"    Docs: {r.doc_ref}", style="dim")
            elif r.status == "warn":
                console.print(Text(f"  ⚠ {r.name}", style="yellow"))
                console.print(f"    {r.detail}")
                if r.remediation:
                    console.print(f"    Fix: {r.remediation}", style="yellow")
            elif r.status == "skip":
                console.print(Text(f"  ⊘ {r.name} — {r.detail}", style="dim"))

    # Summary line
    console.print()
    summary = f"{pass_count}/{len(results)} passed"
    if warn_count:
        summary += f" · {warn_count} warnings"
    if fail_count:
        summary += f" · {fail_count} failed"
    if skip_count:
        summary += f" · {skip_count} skipped"

    if fail_count > 0:
        style = "red bold"
    elif warn_count > 0:
        style = "yellow bold"
    else:
        style = "green bold"
    console.print(summary, style=style)
