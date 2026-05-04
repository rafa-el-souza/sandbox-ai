"""Project-wide configuration: sandbox-ai.toml schema, loader, and machinectl command builder.

Defines the project-root configuration that holds host-level settings
(docker unprivileged user, machinectl authentication mode). Consumed by
CLI commands and the doctor module to determine privilege escalation
strategy for machinectl invocations.
"""

import os
import tomllib
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel


def sandbox_ai_user_home() -> Path:
    """Resolve the per-user sandbox-ai home directory.

    Honors ``SANDBOX_AI_USER_HOME`` (test-isolation override). Otherwise
    returns ``~/.sandbox-ai`` expanded for the current user.
    """
    return Path(os.environ.get("SANDBOX_AI_USER_HOME") or os.path.expanduser("~/.sandbox-ai"))


def state_lock_path() -> Path:
    """Canonical fcntl lock target serializing all per-user state mutations."""
    return sandbox_ai_user_home() / "state" / "state.lock"


class MachinectlAuth(StrEnum):
    """Machinectl privilege escalation mode."""

    SUDO = "sudo"
    POLKIT = "polkit"


class HostSettings(BaseModel):
    """[host] section of sandbox-ai.toml."""

    docker_unprivileged_user: str
    machinectl_authentication: MachinectlAuth = MachinectlAuth.SUDO


class HostConfig(BaseModel):
    """Top-level Pydantic model for sandbox-ai.toml."""

    host: HostSettings

    @classmethod
    def from_toml(cls) -> HostConfig:
        """Parse the canonical per-user ``sandbox-ai.toml``.

        Resolves ``<sandbox_ai_user_home()>/config/sandbox-ai.toml``.

        Raises:
            FileNotFoundError: If the canonical file does not exist.
            tomllib.TOMLDecodeError: If the file contains invalid TOML.
            pydantic.ValidationError: If the content fails schema validation.
        """
        path = sandbox_ai_user_home() / "config" / "sandbox-ai.toml"
        try:
            with open(path, "rb") as f:
                raw = tomllib.load(f)
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                f"No sandbox-ai.toml found at {path}. Run sandbox init to create one."
            ) from exc
        return cls.model_validate(raw)


def machinectl_cmd(user: str, auth: MachinectlAuth) -> list[str]:
    """Build the machinectl shell prefix for the given user and auth mode.

    Returns:
        ``["sudo", "machinectl", "shell", "<user>@.host"]`` when auth is SUDO,
        ``["machinectl", "shell", "<user>@.host"]`` when auth is POLKIT.
    """
    prefix = ["sudo"] if auth == MachinectlAuth.SUDO else []
    return [*prefix, "machinectl", "shell", f"{user}@.host"]
