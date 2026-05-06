"""Instance registry: maps absolute project directories to sandbox instance IDs.

Uses fcntl LOCK_EX for concurrent write safety. The registry is persisted
as a JSON file at ``<sandbox_ai_home()>/state/instances.json``.
"""

import fcntl
import hashlib
import json
import os

from core.host_config import sandbox_ai_home, state_lock_path


def generate_instance_id(project_dir: str) -> str:
    """Generate a deterministic instance ID from an absolute project path.

    Format: <basename>-<md5(abs_path)[:6]>
    """
    abs_path = os.path.abspath(project_dir)
    hash_hex = hashlib.md5(abs_path.encode("utf-8")).hexdigest()[:6]
    basename = os.path.basename(abs_path)
    return f"{basename}-{hash_hex}"


def _default_registry_path() -> str:
    """Resolve ``<home>/state/instances.json`` for the current user."""
    return str(sandbox_ai_home() / "state" / "instances.json")


class InstanceRegistry:
    """File-backed instance registry with fcntl locking for concurrent access."""

    def __init__(self, registry_path: str | None = None) -> None:
        self._path = registry_path if registry_path is not None else _default_registry_path()

    def _load(self) -> dict[str, str]:
        """Load the registry from disk. Returns empty dict if file missing or corrupt."""
        if not os.path.exists(self._path):
            return {}
        with open(self._path) as f:
            try:
                data: dict[str, str] = json.load(f)
                return data
            except json.JSONDecodeError:
                return {}

    def _save(self, data: dict[str, str]) -> None:
        """Atomically write the registry under an exclusive fcntl lock."""
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        lock_path = str(state_lock_path())
        os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            # Re-read under lock to avoid lost updates
            current = self._load()
            current.update(data)
            with open(self._path, "w") as f:
                json.dump(current, f, indent=2)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def register(self, project_dir: str, instance_id: str) -> None:
        """Register a project directory to an instance ID."""
        self._save({project_dir: instance_id})

    def lookup(self, project_dir: str) -> str | None:
        """Look up the instance ID for a project directory. Returns None if not found."""
        data = self._load()
        return data.get(project_dir)

    def remove(self, project_dir: str) -> None:
        """Remove a project directory entry from the registry."""
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        lock_path = str(state_lock_path())
        os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            data = self._load()
            data.pop(project_dir, None)
            with open(self._path, "w") as f:
                json.dump(data, f, indent=2)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
