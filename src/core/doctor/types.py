"""Doctor data types and distro / install-cmd helpers.

This module is a leaf within the ``core.doctor`` package: it has no
intra-package imports. The data types (``Check``, ``CheckResult``) are
consumed by every ``checks/*`` module and by ``registry.py``.
``get_install_cmd`` is a public helper consumed by ``check_sudo``,
``check_machinectl``, and ``check_setfacl`` via the per-topic check
modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class CheckResult:
    """Result of a single diagnostic check."""

    status: Literal["pass", "fail", "skip", "warn"]
    name: str
    detail: str
    remediation: str | None = None
    doc_ref: str | None = None
    category: str = ""


@dataclass
class Check:
    """Declarative diagnostic check with dependency graph support."""

    id: str
    name: str
    category: str
    depends_on: list[str]
    run: Callable[[str, str | None], CheckResult]
    remediation: str
    doc_ref: str | None = None


_DISTRO_MAP: dict[str, str] = {
    "debian": "debian",
    "ubuntu": "debian",
    "fedora": "fedora",
    "rhel": "fedora",
    "centos": "fedora",
    "arch": "arch",
    "manjaro": "arch",
}

_INSTALL_CMD: dict[str, str] = {
    "debian": "sudo apt install",
    "fedora": "sudo dnf install",
    "arch": "sudo pacman -S",
}


_BINARY_PACKAGES: dict[str, str] = {
    "sudo": "sudo",
    "machinectl": "systemd-container",
    "setfacl": "acl",
    "tlog": "tlog",
}


def detect_distro() -> str | None:
    """Detect the host Linux distribution by parsing /etc/os-release.

    Returns a normalized distro family ('debian', 'fedora', 'arch') or None.
    """
    try:
        with open("/etc/os-release") as f:
            content = f.read()
    except FileNotFoundError:
        return None

    fields: dict[str, str] = {}
    for line in content.splitlines():
        if "=" in line:
            key, _, val = line.partition("=")
            fields[key.strip()] = val.strip().strip('"')

    # Check ID first, then ID_LIKE
    distro_id = fields.get("ID", "")
    if distro_id in _DISTRO_MAP:
        return _DISTRO_MAP[distro_id]

    for like in fields.get("ID_LIKE", "").split():
        if like in _DISTRO_MAP:
            return _DISTRO_MAP[like]

    return None


def get_install_cmd(distro: str | None, package: str) -> str:
    """Return a distro-aware install command for the given package."""
    if distro and distro in _INSTALL_CMD:
        return f"{_INSTALL_CMD[distro]} {package}"
    return f"Install the '{package}' package using your system package manager"
