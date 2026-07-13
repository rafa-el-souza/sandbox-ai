# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""ACL ancestor walker: planning-only pure function emitting ancestor paths.

Implements the seven safety rules from the ``instance-workspace-model`` spec:

1. **Realpath first.** ``os.path.realpath`` is run on the target; any symlink
   in any component (detected by realpath != abspath) raises
   :class:`SymlinkInChainError`.
2. **Boundary stop list.** The walk stops before any ancestor in
   :data:`BOUNDARY_PATHS`; no ACL grant is emitted for boundary paths.
3. **Reject targets in the boundary list.** A target whose resolved path is
   in :data:`BOUNDARY_PATHS` raises :class:`BoundaryPathError` (the walker
   surfaces this at planning time so callers can refuse at the init/add gate).
4. **Bounded depth.** Chains exceeding :data:`MAX_DEPTH` raise
   :class:`DepthExceededError`.
5. **lstat throughout.** Components are inspected via :func:`os.lstat`;
   symlink targets are never dereferenced during the walk.
6. **Per-target plans.** This function returns one plan per call; callers
   that need to deduplicate across multiple workspaces do so on the
   execution side.
7. **Fault isolation.** Grants are applied independently — see
   :mod:`core.helper_container` and the orchestrator-volumes capability.

The walker is pure: no setfacl, no filesystem mutation. Output is a list of
:class:`pathlib.Path` objects from the target up to (but excluding) the first
boundary path.
"""

from __future__ import annotations

import os
import stat as _stat
from pathlib import Path

MAX_DEPTH = 64

BOUNDARY_PATHS: frozenset[str] = frozenset(
    {
        "/",
        "/etc",
        "/usr",
        "/var",
        "/tmp",
        "/proc",
        "/sys",
        "/dev",
        "/boot",
        "/run",
        "/home",
        "/root",
        os.path.expanduser("~"),
    }
)


class WalkerError(Exception):
    """Base class for ancestor walker safety violations."""


class SymlinkInChainError(WalkerError):
    """A path component (or the target itself) is a symlink."""


class BoundaryPathError(WalkerError):
    """The target resolves to a path in :data:`BOUNDARY_PATHS`."""


class DepthExceededError(WalkerError):
    """The ancestor chain exceeds :data:`MAX_DEPTH` components."""


def _is_boundary(path: Path) -> bool:
    return str(path) in BOUNDARY_PATHS


def walk_ancestors(target_path: str) -> list[Path]:
    """Plan the ancestor chain for ACL grants on ``target_path``.

    Resolves the target via :func:`os.path.realpath`, validates the seven
    safety rules, and returns the chain of ancestor paths from the target
    upward, stopping before the first boundary path.

    Args:
        target_path: The workspace path (or other ACL target) to plan for.

    Returns:
        List of :class:`pathlib.Path` objects, target first, ascending toward
        but not including the first boundary path.

    Raises:
        SymlinkInChainError: If any component of the resolved path differs
            from the absolute path (i.e. a symlink was dereferenced) or if
            an in-walk :func:`os.lstat` reveals a symlink.
        BoundaryPathError: If the resolved target is itself in
            :data:`BOUNDARY_PATHS`.
        DepthExceededError: If the ancestor chain exceeds :data:`MAX_DEPTH`.
    """
    abs_path = os.path.abspath(target_path)
    resolved = os.path.realpath(target_path)
    if resolved != abs_path:
        raise SymlinkInChainError(f"symlink detected resolving {target_path!r}: {resolved!r} != {abs_path!r}")

    target = Path(resolved)
    if _is_boundary(target):
        raise BoundaryPathError(f"target {target_path!r} resolves to boundary path {resolved!r}")

    ancestors: list[Path] = []
    current = target
    while True:
        st = os.lstat(current)
        if _stat.S_ISLNK(st.st_mode):
            raise SymlinkInChainError(f"symlink detected at {current!s} during walk")
        ancestors.append(current)
        if len(ancestors) > MAX_DEPTH:
            raise DepthExceededError(f"ancestor chain of {target_path!r} exceeds MAX_DEPTH={MAX_DEPTH}")
        parent = current.parent
        if parent == current or _is_boundary(parent):
            break
        current = parent

    return ancestors
