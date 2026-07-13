# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""Workspace copy recipe (init / workspace add ``--copy``).

Pre-copy lstat scan refuses by default when external symlinks (links pointing
outside the source tree) are detected. Operators can opt in to safe-link
stripping with ``strip_unsafe_links=True``, which adds rsync ``--safe-links``.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

# Default-excludes shared by ``--copy`` and the workspace backup recipe.
# Source of truth: cli-workspace's "Copy Default-Excludes List".
# `.git/` is intentionally NOT excluded (portable history).
COPY_DEFAULT_EXCLUDES: tuple[str, ...] = (
    # Node
    "node_modules",
    ".pnpm-store",
    ".yarn/cache",
    ".yarn/install-state.gz",
    # Python
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    # Build outputs (multi-language)
    "target",
    "bin",
    "dist",
    "build",
    "out",
    ".next",
    ".nuxt",
    ".tsbuildinfo",
    ".turbo",
    # Ruby / PHP / vendored deps
    "vendor",
    "vendor/bundle",
    ".bundle",
    # Java / C++
    ".gradle",
    "cmake-build-*",
    # IDE / editor / OS
    ".idea",
    ".vscode",
    ".DS_Store",
    "*.swp",
    "*.log",
    # Coverage / cache
    "coverage",
    ".nyc_output",
    ".cache",
)


class WorkspaceCopyError(Exception):
    """Base class for workspace-copy failures."""


class UnsafeSymlinkError(WorkspaceCopyError):
    """Source tree contains symlinks that escape the tree and the caller did
    not opt in to ``strip_unsafe_links``."""


@dataclass(frozen=True)
class CopyResult:
    """Outcome of a successful copy."""

    bytes_total: int
    excludes_applied: tuple[str, ...]
    safe_links: bool


def _is_external_symlink(link_path: str, source_root: str) -> bool:
    target = os.readlink(link_path)
    if os.path.isabs(target):
        return not os.path.realpath(link_path).startswith(os.path.realpath(source_root))
    resolved = os.path.realpath(os.path.join(os.path.dirname(link_path), target))
    return not resolved.startswith(os.path.realpath(source_root))


def scan_unsafe_symlinks(source_root: str) -> list[str]:
    """Return a list of symlink paths within ``source_root`` whose target
    escapes the tree."""
    unsafe: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(source_root, followlinks=False):
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full) and _is_external_symlink(full, source_root):
                unsafe.append(full)
    return unsafe


def copy_workspace(
    source: str,
    dest: str,
    *,
    strip_unsafe_links: bool = False,
    excludes: tuple[str, ...] = COPY_DEFAULT_EXCLUDES,
) -> CopyResult:
    """Copy ``source`` into ``dest`` via ``rsync -a --no-owner --no-group``.

    Pre-copy: scans for external symlinks; refuses unless ``strip_unsafe_links``
    is True. With ``strip_unsafe_links`` we pass ``--safe-links`` to rsync so
    it omits external links from the destination tree.
    """
    if not strip_unsafe_links:
        unsafe = scan_unsafe_symlinks(source)
        if unsafe:
            raise UnsafeSymlinkError(
                f"{len(unsafe)} symlink(s) in {source!r} escape the source tree; "
                "re-run with strip_unsafe_links=True to drop them, or fix the source."
            )

    cmd: list[str] = ["rsync", "-a", "--no-owner", "--no-group"]
    for exc in excludes:
        cmd.extend(["--exclude", exc])
    if strip_unsafe_links:
        cmd.append("--safe-links")
    cmd.extend([f"{source.rstrip('/')}/", f"{dest.rstrip('/')}/"])

    os.makedirs(dest, exist_ok=True)
    subprocess.run(cmd, check=True)
    bytes_total = sum(
        os.path.getsize(os.path.join(dirpath, f))
        for dirpath, _dn, files in os.walk(dest)
        for f in files
        if not os.path.islink(os.path.join(dirpath, f))
    )
    return CopyResult(bytes_total=bytes_total, excludes_applied=excludes, safe_links=strip_unsafe_links)
