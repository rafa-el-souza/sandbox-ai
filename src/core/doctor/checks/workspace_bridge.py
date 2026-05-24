"""Workspace-bridge doctor checks.

Covers the 11 workspace-bridge / per-instance health checks plus the per-instance
scan helpers (``_scan_instance_dirs``, ``_scan_instance_workspace_paths``,
``_default_uid_for_path``, ``_read_registry_raw``, ``_load_host_settings_or_skip``).

Sole-caller locality keeps the helpers in this module: every helper is consumed
by check functions defined here, so co-locating them preserves cohesion. Two
helpers (``_scan_instance_dirs``, ``_read_registry_raw``) are also consumed by
``checks/per_user_tree.py`` for the legacy-shape checks; per_user_tree imports
them directly from this module.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from core.doctor.types import CheckResult
from core.host_config import (
    HostConfig,
    HostSettings,
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

    settings_or_skip = _load_host_settings_or_skip("operator in workspace bridge group")
    if isinstance(settings_or_skip, CheckResult):
        return settings_or_skip
    host = settings_or_skip
    try:
        bridge_gid = workspace_bridge_gid(host)
    except (WorkspaceBridgeGroupMissingError, SubgidOutOfRangeError, NoSubgidRangeError) as exc:
        return CheckResult(
            status="fail",
            name="operator in workspace bridge group",
            detail=str(exc),
            remediation="See `workspace bridge group` check",
            category="Workspace Bridge",
        )

    if bridge_gid in os.getgroups():
        return CheckResult(
            status="pass",
            name="operator in workspace bridge group",
            detail=f"current process supplementary groups include gid {bridge_gid}",
            category="Workspace Bridge",
        )

    current_user = pwd.getpwuid(os.getuid()).pw_name
    in_etc_group = bridge_gid in {g.gr_gid for g in grp.getgrall() if current_user in g.gr_mem}
    if in_etc_group:
        return CheckResult(
            status="fail",
            name="operator in workspace bridge group",
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
        name="operator in workspace bridge group",
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
        "log/core",
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
            name="operator umask workspace-friendly",
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
            name="operator umask workspace-friendly",
            detail=(
                f"operator umask {saved:04o} masks group-write; workspace files "
                f"won't be group-writable for the agent"
            ),
            remediation=(
                "Add `umask 007` to your shell rc (~/.bashrc, ~/.zshrc) so workspace "
                "files land mode 0660 (group rw, no access for others)"
            ),
            category="Workspace Bridge",
        )
    return CheckResult(
        status="pass",
        name="operator umask workspace-friendly",
        detail=f"operator umask {saved:04o} preserves group write",
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


__all__ = [
    "check_backups_disk_pressure",
    "check_backups_partial_dirs_present",
    "check_dev_in_workspace_bridge_group",
    "check_dev_umask_workspace_friendly",
    "check_pre_existing_instance_layout",
    "check_secrets_hydrated_restrictively",
    "check_subuid_resolver_works",
    "check_workspace_bridge_group_exists",
    "check_workspace_home_single_filesystem",
    "check_workspace_path_in_walker_boundary",
]
