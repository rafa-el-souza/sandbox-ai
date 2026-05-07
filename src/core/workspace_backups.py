"""Workspace backup mechanism (cli-workspace's "Workspace Backup Recipe").

A backup is an rsync-mirrored copy of a live workspace tree under
``<home>/workspaces/_backups/<inst>/<ws>/<UTC-timestamp>/`` that intentionally
strips runtime ACL/group/setgid state (per D8). The recipe is shared by
``workspace remove --backup``, ``workspace restore``, and ``destroy
--backup-workspaces``.

Atomic creation: rsync writes into ``<ts>.partial/``; metadata is written;
``os.rename(.partial, final)`` finalizes. A partway failure leaves
``<ts>.partial/`` for diagnosis (the doctor's ``backups_partial_dirs_present``
check flags stale partials).

State.lock orchestration is the caller's responsibility (per D10):
``state.lock`` is released around the long rsync phase. This module owns
``<inst>.backup.lock`` for the duration of ``create_backup``.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import TYPE_CHECKING, Any

from core.exceptions import SandboxExecutionError
from core.executor import Executor
from core.host_config import sandbox_ai_home
from core.locks import BackupLockHeldError, acquire_backup_lock
from core.workspace_copy import COPY_DEFAULT_EXCLUDES, scan_unsafe_symlinks

if TYPE_CHECKING:
    from pathlib import Path

SCHEMA_VERSION = 1
TIMESTAMP_FORMAT = "%Y-%m-%d-%H-%M-%S"
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}$")
BACKUP_INFO_FILENAME = ".backup-info.json"


class BackupError(Exception):
    """Base class for backup failures."""


class BackupRsyncError(BackupError):
    """rsync exited non-zero during backup."""


class BackupPathError(BackupError):
    """Source path fails a structural gate (missing, cycle, cross-fs)."""


class BackupSpecAmbiguousError(BackupError):
    """A `<src-inst>/<src-ws>` (or omitted) spec resolves to >1 candidate."""


class BackupSpecNotFoundError(BackupError):
    """A spec resolves to zero candidates."""


@dataclass(frozen=True)
class BackupInfo:
    """Locator + parsed metadata for an existing backup tree."""

    path: Path
    source_instance: str
    source_workspace: str
    timestamp: str  # YYYY-MM-DD-HH-MM-SS (UTC)
    size_bytes: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BackupFilter:
    """Optional filter for :func:`list_backups`."""

    source_instance: str | None = None
    source_workspace: str | None = None


# ─── rsync probe ───────────────────────────────────────────────────────────

_xattrs_supported_cache: bool | None = None
_rsync_version_cache: str | None = None


def _query_rsync_version() -> str:
    """Return the first line of ``rsync --version`` (cached)."""
    global _rsync_version_cache
    if _rsync_version_cache is not None:
        return _rsync_version_cache
    executor = Executor()
    try:
        result = executor.run(["rsync", "--version"], sentinel=False)
    except SandboxExecutionError:
        _rsync_version_cache = ""
        return ""
    first_line = (result.stdout or "").splitlines()[:1]
    _rsync_version_cache = first_line[0] if first_line else ""
    return _rsync_version_cache


def rsync_supports_xattrs() -> bool:
    """Probe ``rsync --version`` for the ``xattrs`` capability tag."""
    global _xattrs_supported_cache
    if _xattrs_supported_cache is not None:
        return _xattrs_supported_cache
    executor = Executor()
    try:
        result = executor.run(["rsync", "--version"], sentinel=False)
    except SandboxExecutionError:
        _xattrs_supported_cache = False
        return False
    text = (result.stdout or "")
    # rsync prints a "Capabilities:" / feature line containing "xattrs" when
    # built with xattr support. Distros vary on phrasing ("xattrs" vs
    # "no xattrs") so a positive substring check is the portable test.
    has_xattrs = " xattrs" in text and "no xattrs" not in text
    _xattrs_supported_cache = has_xattrs
    return has_xattrs


def _reset_rsync_caches() -> None:
    """Test seam: clear the rsync probe caches so a monkeypatched ``Executor``
    is exercised on the next call."""
    global _xattrs_supported_cache, _rsync_version_cache
    _xattrs_supported_cache = None
    _rsync_version_cache = None


# ─── path helpers ──────────────────────────────────────────────────────────


def backups_root() -> Path:
    """Return ``<home>/workspaces/_backups/`` (lazy-created on first backup)."""
    return sandbox_ai_home() / "workspaces" / "_backups"


def backup_dir(instance_name: str, workspace_name: str, timestamp: str) -> Path:
    return backups_root() / instance_name / workspace_name / timestamp


def _utc_timestamp(now: _dt.datetime | None = None) -> str:
    current = now if now is not None else _dt.datetime.now(tz=_dt.UTC)
    return current.strftime(TIMESTAMP_FORMAT)


def _sandbox_ai_version() -> str:
    try:
        return _pkg_version("sandbox-ai")
    except PackageNotFoundError:
        return "unknown"


# ─── rsync recipe ──────────────────────────────────────────────────────────


def _build_rsync_cmd(
    source: str,
    dest_partial: str,
    *,
    excludes: tuple[str, ...],
    extra_excludes: tuple[str, ...],
    dev_primary_gid: int,
    safe_links: bool,
    use_xattrs: bool,
) -> list[str]:
    """Build the backup-recipe rsync invocation.

    Flag set per cli-workspace's "Workspace Backup Recipe":
    ``rsync -aHXS --no-owner --no-group --group=<gid> --chmod=...
    [<excludes>] <src>/ <dest>.partial/``.
    """
    flags = ["rsync", "-aHS"]
    if use_xattrs:
        flags.append("-X")
    flags.extend(
        [
            "--no-owner",
            "--no-group",
            f"--group={dev_primary_gid}",
            "--chmod=Du+rwx,Dg-s,Dgo-rwx,Fu+rw,Fgo-rwx,Fa-st",
        ]
    )
    if safe_links:
        flags.append("--safe-links")
    for exc in excludes:
        flags.extend(["--exclude", exc])
    for exc in extra_excludes:
        flags.extend(["--exclude", exc])
    flags.extend([f"{source.rstrip('/')}/", f"{dest_partial.rstrip('/')}/"])
    return flags


def _tree_size_and_count(root: str) -> tuple[int, int]:
    """Sum file sizes under ``root`` (lstat: do not deref symlinks); count files."""
    total = 0
    files = 0
    for dirpath, _dn, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            files += 1
            total += st.st_size
    return total, files


def _write_backup_info(
    partial_dir: Path,
    *,
    source_instance: str,
    source_workspace: str,
    source_bootstrap_mode: str,
    source_path: str,
    excludes_applied: tuple[str, ...],
    stripped_unsafe_links_count: int,
    rsync_xattrs_supported: bool,
    size_bytes: int,
    file_count: int,
    timestamp: str,
) -> None:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source_instance": source_instance,
        "source_workspace": source_workspace,
        "source_bootstrap_mode": source_bootstrap_mode,
        "source_path": source_path,
        "created_at_utc": (
            _dt.datetime.strptime(timestamp, TIMESTAMP_FORMAT)
            .replace(tzinfo=_dt.UTC)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        ),
        "size_bytes": size_bytes,
        "file_count": file_count,
        "sandbox_ai_version": _sandbox_ai_version(),
        "rsync_excludes_applied": list(excludes_applied),
        "stripped_unsafe_links_count": stripped_unsafe_links_count,
        "tooling": {
            "rsync_version": _query_rsync_version(),
            "rsync_xattrs_supported": rsync_xattrs_supported,
        },
    }
    out = partial_dir / BACKUP_INFO_FILENAME
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


# ─── public surface ────────────────────────────────────────────────────────


def create_backup(
    *,
    instance_name: str,
    workspace_name: str,
    source_path: str,
    source_bootstrap_mode: str,
    dev_primary_gid: int,
    excludes: tuple[str, ...] = COPY_DEFAULT_EXCLUDES,
    extra_excludes: tuple[str, ...] = (),
    now: _dt.datetime | None = None,
) -> BackupInfo:
    """Create a backup of ``source_path`` for ``<instance>/<workspace>``.

    Acquires ``<instance_name>.backup.lock`` for the duration of the rsync.
    The caller is expected to release the per-user ``state.lock`` before
    invoking this and reacquire it after, per D10's phase ordering.

    Raises:
        BackupPathError: ``source_path`` is missing or not a directory.
        BackupLockHeldError: another process holds the backup lock.
        BackupRsyncError: rsync exited non-zero.
    """
    src = os.path.realpath(source_path)
    if not os.path.isdir(src):
        raise BackupPathError(f"backup source {source_path!r} is not a directory")

    timestamp = _utc_timestamp(now)
    final_dir = backup_dir(instance_name, workspace_name, timestamp)
    partial_dir = final_dir.with_name(final_dir.name + ".partial")

    os.makedirs(partial_dir.parent, mode=0o700, exist_ok=True)

    unsafe = scan_unsafe_symlinks(src)
    stripped_count = len(unsafe)
    use_xattrs = rsync_supports_xattrs()

    cmd = _build_rsync_cmd(
        src,
        str(partial_dir),
        excludes=excludes,
        extra_excludes=extra_excludes,
        dev_primary_gid=dev_primary_gid,
        safe_links=stripped_count > 0,
        use_xattrs=use_xattrs,
    )

    with acquire_backup_lock(instance_name):
        os.makedirs(partial_dir, mode=0o700, exist_ok=True)
        executor = Executor()
        try:
            executor.run(cmd, sentinel=False)
        except SandboxExecutionError as exc:
            raise BackupRsyncError(f"rsync failed for {instance_name}/{workspace_name}: {exc}") from exc

        size_bytes, file_count = _tree_size_and_count(str(partial_dir))
        _write_backup_info(
            partial_dir,
            source_instance=instance_name,
            source_workspace=workspace_name,
            source_bootstrap_mode=source_bootstrap_mode,
            source_path=src,
            excludes_applied=excludes,
            stripped_unsafe_links_count=stripped_count,
            rsync_xattrs_supported=use_xattrs,
            size_bytes=size_bytes,
            file_count=file_count,
            timestamp=timestamp,
        )

        os.rename(partial_dir, final_dir)

    metadata = _read_backup_info(final_dir)
    return BackupInfo(
        path=final_dir,
        source_instance=instance_name,
        source_workspace=workspace_name,
        timestamp=timestamp,
        size_bytes=size_bytes,
        metadata=metadata,
    )


def _read_backup_info(directory: Path) -> dict[str, Any]:
    info_path = directory / BACKUP_INFO_FILENAME
    try:
        with open(info_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def list_backups(filt: BackupFilter | None = None) -> list[BackupInfo]:
    """Enumerate completed backups under ``<home>/workspaces/_backups/``.

    Skips ``*.partial`` directories. Result is sorted by (instance, workspace,
    timestamp) for deterministic iteration.
    """
    root = backups_root()
    if not root.exists():
        return []
    out: list[BackupInfo] = []
    for inst_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        inst_name = inst_dir.name
        if filt and filt.source_instance and filt.source_instance != inst_name:
            continue
        for ws_dir in sorted(p for p in inst_dir.iterdir() if p.is_dir()):
            ws_name = ws_dir.name
            if filt and filt.source_workspace and filt.source_workspace != ws_name:
                continue
            for ts_dir in sorted(p for p in ws_dir.iterdir() if p.is_dir()):
                if ts_dir.name.endswith(".partial"):
                    continue
                if not _TIMESTAMP_RE.match(ts_dir.name):
                    continue
                size_bytes, _ = _tree_size_and_count(str(ts_dir))
                out.append(
                    BackupInfo(
                        path=ts_dir,
                        source_instance=inst_name,
                        source_workspace=ws_name,
                        timestamp=ts_dir.name,
                        size_bytes=size_bytes,
                        metadata=_read_backup_info(ts_dir),
                    )
                )
    return out


def resolve_backup_spec(spec: str | None, dest_ws_name: str) -> BackupInfo:
    """Resolve a ``--from`` value to exactly one :class:`BackupInfo`.

    Three forms accepted (per `cli-workspace` "workspace restore Command"):

    * ``None`` — pick latest backup whose source workspace matches
      ``dest_ws_name``. Refuse if multiple instances have such a backup.
    * ``"<src-inst>/<src-ws>"`` — pick latest backup of that source pair.
    * ``"<src-inst>/<src-ws>/<ts>"`` — fully qualified specification.

    Raises:
        BackupSpecNotFoundError: zero matching backups.
        BackupSpecAmbiguousError: omitted form matches >1 source instance.
    """
    if spec is None:
        candidates = list_backups(BackupFilter(source_workspace=dest_ws_name))
        if not candidates:
            raise BackupSpecNotFoundError(
                f"no backups found with source workspace {dest_ws_name!r}; specify --from"
            )
        instances = {c.source_instance for c in candidates}
        if len(instances) > 1:
            joined = ", ".join(sorted(instances))
            raise BackupSpecAmbiguousError(
                f"multiple source instances have backups for workspace {dest_ws_name!r}: "
                f"{joined}. Disambiguate with --from <src-inst>/<src-ws>."
            )
        return candidates[-1]  # list_backups sorts by timestamp ascending

    parts = spec.split("/")
    if len(parts) == 2:
        src_inst, src_ws = parts
        candidates = list_backups(BackupFilter(source_instance=src_inst, source_workspace=src_ws))
        if not candidates:
            raise BackupSpecNotFoundError(f"no backups found for {src_inst}/{src_ws}")
        return candidates[-1]
    if len(parts) == 3:
        src_inst, src_ws, ts = parts
        if not _TIMESTAMP_RE.match(ts):
            raise BackupSpecNotFoundError(f"timestamp {ts!r} does not match YYYY-MM-DD-HH-MM-SS")
        target = backup_dir(src_inst, src_ws, ts)
        if not target.is_dir():
            raise BackupSpecNotFoundError(f"backup {spec!r} not found at {target}")
        size_bytes, _ = _tree_size_and_count(str(target))
        return BackupInfo(
            path=target,
            source_instance=src_inst,
            source_workspace=src_ws,
            timestamp=ts,
            size_bytes=size_bytes,
            metadata=_read_backup_info(target),
        )
    raise BackupSpecNotFoundError(
        f"--from value {spec!r} must be '<src-inst>/<src-ws>' or '<src-inst>/<src-ws>/<ts>'"
    )


def restore_backup(backup: BackupInfo, dest_inst: str, dest_ws: str) -> Path:
    """Copy ``backup.path`` into the destination instance's workspace tree.

    The caller (cli ``workspace restore``) is responsible for gating
    (instance must be stopped, dest workspace must not exist) and for the
    sandbox.toml mutation. This function performs only the filesystem copy
    and returns the new workspace path.

    Backups are already stripped of unsafe links and runtime state, so the
    restore copy is a straight ``shutil.copytree`` (no rsync needed).
    """
    dest_path = sandbox_ai_home() / "workspaces" / dest_inst / dest_ws
    if dest_path.exists():
        raise BackupPathError(f"destination workspace {dest_path} already exists")
    os.makedirs(dest_path.parent, mode=0o700, exist_ok=True)
    shutil.copytree(backup.path, dest_path, symlinks=True, ignore=_ignore_backup_info)
    return dest_path


def _ignore_backup_info(_dir: str, names: list[str]) -> list[str]:
    return [n for n in names if n == BACKUP_INFO_FILENAME]


# Re-export for callers that orchestrate the lock externally.
__all__ = [
    "BACKUP_INFO_FILENAME",
    "SCHEMA_VERSION",
    "TIMESTAMP_FORMAT",
    "BackupError",
    "BackupFilter",
    "BackupInfo",
    "BackupLockHeldError",
    "BackupPathError",
    "BackupRsyncError",
    "BackupSpecAmbiguousError",
    "BackupSpecNotFoundError",
    "backup_dir",
    "backups_root",
    "create_backup",
    "list_backups",
    "resolve_backup_spec",
    "restore_backup",
    "rsync_supports_xattrs",
]
