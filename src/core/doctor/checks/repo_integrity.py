# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Repo-integrity doctor checks: tooling plane file presence, state dir writable."""

from __future__ import annotations

import tempfile
from importlib.resources import files

from core.doctor.types import CheckResult
from core.host_config import sandbox_ai_home

# Module-level alias preserved so tests rebinding
# ``core.doctor.checks.repo_integrity._resource_files`` continue to work
# (the alias used to live in ``core.doctor`` pre-refactor).
_resource_files = files

# 14 unconditional source files in the packaged templates module
_UNCONDITIONAL_FILES: list[str] = [
    "docker/compose.yml",
    "docker/core/entrypoint.sh",
    "docker/admin/Dockerfile.admin",
    "docker/admin/fwd.go",
    "docker/coredns/Dockerfile.coredns",
    "config/coredns/Corefile",
    "config/dnsdist/dnsdist.conf",
    "config/proxy/squid.conf",
    "config/proxy/ERR_SANDBOX_403",
    "config/core/.bashrc",
    "config/core/.npmrc",
    "config/core/.gitconfig",
    "config/core/sshd_config",
    "config/core/CLAUDE.md",
]


def check_tooling_plane(user: str, distro: str | None) -> CheckResult:
    """Check that all 14 unconditional tooling plane files exist."""
    templates_root = _resource_files("templates")
    missing: list[str] = []
    for rel_path in _UNCONDITIONAL_FILES:
        if not templates_root.joinpath(rel_path).is_file():
            missing.append(rel_path)

    if not missing:
        return CheckResult(
            status="pass",
            name="tooling plane",
            detail="All 14 unconditional files present",
        )
    return CheckResult(
        status="fail",
        name="tooling plane",
        detail=f"Missing files: {', '.join(missing)}",
        remediation="Restore missing files from the repository or re-clone",
    )


def check_state_dir_writable(user: str, distro: str | None) -> CheckResult:
    """Check that the per-user ``<home>/state/`` directory is writable."""
    del user, distro
    state_dir = sandbox_ai_home() / "state"
    try:
        with tempfile.NamedTemporaryFile(dir=str(state_dir), delete=True):
            pass
        return CheckResult(
            status="pass",
            name="state dir writable",
            detail=f"{state_dir} is writable",
        )
    except OSError:
        return CheckResult(
            status="fail",
            name="state dir writable",
            detail=f"{state_dir} is not writable",
            remediation=f"Fix permissions: chmod 0700 {state_dir}",
        )


__all__ = [
    "check_state_dir_writable",
    "check_tooling_plane",
]
