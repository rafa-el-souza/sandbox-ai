# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-instance backup lock: serializes long-running backup rsyncs without
holding the per-user ``state.lock`` for their full duration.

The backup lock is a per-instance fcntl ``LOCK_EX | LOCK_NB`` lock at
``<home>/state/<inst>.backup.lock``. The lockfile contains a JSON record
``{"pid": ..., "started_at_utc": ...}`` so callers can detect stale locks
abandoned by a crashed process. Stale detection uses a 60-second grace
window from ``started_at_utc``.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import errno
import fcntl
import json
import os
from typing import TYPE_CHECKING

from core.host_config import sandbox_ai_home

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from core.json_types import JsonValue

STALE_GRACE_SECONDS = 60


class BackupLockHeldError(Exception):
    """Raised when the per-instance backup lock is held by another process."""


def backup_lock_path(instance_name: str) -> Path:
    """Resolve ``<home>/state/<inst>.backup.lock`` for the current user."""
    return sandbox_ai_home() / "state" / f"{instance_name}.backup.lock"


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


def _read_lock_metadata(path: Path) -> dict[str, JsonValue] | None:
    try:
        with open(path) as f:
            data: JsonValue = json.load(f)
    except OSError:
        return None
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    # ``data`` is now a known ``dict[str, JsonValue]`` (JSON object keys are
    # always ``str``). Values stay structurally typed but opaque to this layer —
    # the caller (:func:`is_lock_stale`) re-narrows ``started_at_utc`` with its
    # own isinstance gate.
    return data


def is_lock_stale(path: Path, *, now: _dt.datetime | None = None) -> bool:
    """Return True iff the lock metadata's ``started_at_utc`` is older than
    :data:`STALE_GRACE_SECONDS` (or the file is unreadable / malformed)."""
    meta = _read_lock_metadata(path)
    if meta is None:
        return True
    started = meta.get("started_at_utc")
    if not isinstance(started, str):
        return True
    try:
        ts = _dt.datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.UTC)
    except ValueError:
        return True
    current = now if now is not None else _utcnow()
    return (current - ts).total_seconds() > STALE_GRACE_SECONDS


def is_backup_lock_held(instance_name: str) -> bool:
    """Return True iff the per-instance backup lock is held by a live process.

    Stale locks (per :func:`is_lock_stale`) and missing lockfiles return False.
    Used by lifecycle commands (`start`, `stop`, `attach`, `destroy`) to refuse
    fast when a backup is in flight on the same instance.

    Other ``OSError`` subclasses raised by ``os.open``/``flock`` (EBADF, ENOLCK,
    permission errors) propagate — they signal filesystem corruption, not lock
    contention, and the caller should not silently treat them as "not held."
    """
    path = backup_lock_path(instance_name)
    if not path.exists():
        return False
    if is_lock_stale(path):
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        # TOCTOU: lockfile removed between `path.exists()` and `os.open`.
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


@contextlib.contextmanager
def acquire_backup_lock(instance_name: str) -> Generator[Path]:
    """Acquire the per-instance backup lock with ``LOCK_EX | LOCK_NB``.

    Writes ``{"pid": <pid>, "started_at_utc": <iso>}`` to the lockfile so a
    parallel command can detect stale locks via :func:`is_lock_stale`.
    Releases the lock and clears the metadata on context exit.

    Raises:
        BackupLockHeldError: The lock is held by another process and the
            existing metadata is not stale.
    """
    path = backup_lock_path(instance_name)
    os.makedirs(path.parent, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise
            if not is_lock_stale(path):
                raise BackupLockHeldError(f"backup lock for {instance_name!r} is held by another process") from exc
            # Stale lock: the prior holder is gone but did not release.
            # Forcibly take it — flock(LOCK_EX) blocks indefinitely so we
            # cannot retry without unblocking. Truncate-and-retry is safe
            # because the prior owner is, by stale definition, gone.
            os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        meta = json.dumps({"pid": os.getpid(), "started_at_utc": _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}).encode(
            "utf-8"
        )
        os.write(fd, meta)
        try:
            yield path
        finally:
            os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
