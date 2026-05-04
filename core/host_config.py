"""Project-wide configuration: sandbox-ai.toml schema, loader, and machinectl command builder.

Defines the project-root configuration that holds host-level settings
(docker unprivileged user, machinectl authentication mode). Consumed by
CLI commands and the doctor module to determine privilege escalation
strategy for machinectl invocations.
"""

import os
import tomllib
from enum import StrEnum

from pydantic import BaseModel


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
    def from_toml(cls, project_dir: str) -> HostConfig:
        """Parse sandbox-ai.toml from the given project directory.

        Args:
            project_dir: Absolute path to the project root. The loader
                reads ``<project_dir>/sandbox-ai.toml``.

        Raises:
            FileNotFoundError: If sandbox-ai.toml does not exist.
            tomllib.TOMLDecodeError: If the file contains invalid TOML.
            pydantic.ValidationError: If the content fails schema validation.
        """
        path = os.path.join(project_dir, "sandbox-ai.toml")
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        return cls.model_validate(raw)


def machinectl_cmd(user: str, auth: MachinectlAuth) -> list[str]:
    """Build the machinectl shell prefix for the given user and auth mode.

    Returns:
        ``["sudo", "machinectl", "shell", "<user>@.host"]`` when auth is SUDO,
        ``["machinectl", "shell", "<user>@.host"]`` when auth is POLKIT.
    """
    prefix = ["sudo"] if auth == MachinectlAuth.SUDO else []
    return [*prefix, "machinectl", "shell", f"{user}@.host"]
