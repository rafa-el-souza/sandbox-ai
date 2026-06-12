# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Instance registry: maps instance names to their on-disk state.

Keyed by ``instance_name`` (globally unique per-user). Values record the
absolute ``instance_dir`` and a UTC ``created_at`` timestamp. Concurrent
access is serialized via the dedicated ``instances.json.lock``
(fcntl LOCK_EX), distinct from ``state.lock`` so registry mutations
called from inside a held ``state.lock`` context (e.g. ``sandbox destroy``)
do not self-deadlock on a second FD aliasing the same lock file.
Persisted as JSON at ``<sandbox_ai_home()>/state/instances.json``.
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os
from dataclasses import asdict, dataclass

from core.host_config import registry_lock_path, sandbox_ai_home


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with ``Z`` suffix."""
    return _dt.datetime.now(tz=_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class RegistryEntry:
    """A single registry record."""

    instance_dir: str
    created_at: str


def _default_registry_path() -> str:
    return str(sandbox_ai_home() / "state" / "instances.json")


def is_path_keyed(data: dict[str, object]) -> bool:
    """Detect a legacy path-keyed registry shape.

    The legacy shape used absolute path strings (starting with ``/``) as keys
    mapping to an ``instance_id`` string. The new shape keys by instance name
    (no leading ``/``) mapping to a record dict.
    """
    return any(isinstance(k, str) and k.startswith("/") for k in data)


class InstanceNameInUseError(Exception):
    """Raised when a register call would shadow an existing instance name."""


class InstanceRegistry:
    """File-backed instance registry keyed by instance name."""

    def __init__(self, registry_path: str | None = None) -> None:
        self._path = registry_path if registry_path is not None else _default_registry_path()

    def _load_raw(self) -> dict[str, dict[str, str]]:
        if not os.path.exists(self._path):
            return {}
        with open(self._path) as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                return {}
        if not isinstance(data, dict):
            return {}
        if is_path_keyed(data):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)}

    def _open_lock(self) -> int:
        lock_path = str(registry_lock_path())
        os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
        return os.open(lock_path, os.O_CREAT | os.O_RDWR)

    def _write_locked(self, data: dict[str, dict[str, str]]) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with open(self._path, "w") as f:
            json.dump(data, f, indent=2)

    def get(self, name: str) -> RegistryEntry | None:
        raw = self._load_raw().get(name)
        if raw is None:
            return None
        try:
            return RegistryEntry(instance_dir=raw["instance_dir"], created_at=raw["created_at"])
        except KeyError:
            return None

    def all(self) -> dict[str, RegistryEntry]:
        return {
            name: RegistryEntry(instance_dir=v["instance_dir"], created_at=v["created_at"])
            for name, v in self._load_raw().items()
            if "instance_dir" in v and "created_at" in v
        }

    def register(self, name: str, instance_dir: str, *, allow_overwrite: bool = False) -> RegistryEntry:
        """Register ``name`` → ``instance_dir`` and return the new entry.

        Raises:
            InstanceNameInUseError: ``name`` is already registered and
                ``allow_overwrite`` is False.
        """
        lock_fd = self._open_lock()
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            data = self._load_raw()
            if name in data and not allow_overwrite:
                raise InstanceNameInUseError(
                    f"instance name {name!r} is already registered at {data[name].get('instance_dir')!r}"
                )
            entry = RegistryEntry(instance_dir=instance_dir, created_at=_utcnow_iso())
            data[name] = asdict(entry)
            self._write_locked(data)
            return entry
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def remove(self, name: str) -> None:
        lock_fd = self._open_lock()
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            data = self._load_raw()
            data.pop(name, None)
            self._write_locked(data)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
