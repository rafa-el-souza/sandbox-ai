# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for core.walker — ACL ancestor walker with safety rules."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest
from core import walker
from core.walker import (
    BOUNDARY_PATHS,
    MAX_DEPTH,
    BoundaryPathError,
    DepthExceededError,
    SymlinkInChainError,
    WalkerError,
    walk_ancestors,
)


class TestModuleConstants:
    def test_max_depth_is_64(self) -> None:
        assert MAX_DEPTH == 64

    def test_boundary_paths_includes_required_entries(self) -> None:
        required = {
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
        }
        assert required.issubset(BOUNDARY_PATHS)

    def test_boundary_paths_includes_user_home(self) -> None:
        assert os.path.expanduser("~") in BOUNDARY_PATHS

    def test_walker_error_hierarchy(self) -> None:
        assert issubclass(SymlinkInChainError, WalkerError)
        assert issubclass(BoundaryPathError, WalkerError)
        assert issubclass(DepthExceededError, WalkerError)


class TestCleanWalk:
    def test_returns_ancestors_with_target_first(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "c"
        nested.mkdir(parents=True)
        ancestors = walk_ancestors(str(nested))
        assert ancestors[0] == nested.resolve()

    def test_chain_ascends_via_parent(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "c"
        nested.mkdir(parents=True)
        ancestors = walk_ancestors(str(nested))
        for i in range(len(ancestors) - 1):
            assert ancestors[i + 1] == ancestors[i].parent

    def test_stops_before_boundary(self, tmp_path: Path) -> None:
        """tmp_path is under /tmp (a boundary); walk must not include /tmp itself."""
        nested = tmp_path / "a"
        nested.mkdir()
        ancestors = walk_ancestors(str(nested))
        assert all(str(p) not in BOUNDARY_PATHS for p in ancestors)


class TestSymlinkInChain:
    def test_target_via_symlink_rejected(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "via-link"
        link.symlink_to(real)
        with pytest.raises(SymlinkInChainError):
            walk_ancestors(str(link))

    def test_ancestor_symlink_rejected(self, tmp_path: Path) -> None:
        real_parent = tmp_path / "real_parent"
        real_parent.mkdir()
        (real_parent / "child").mkdir()
        link_parent = tmp_path / "link_parent"
        link_parent.symlink_to(real_parent)
        # Walk through the symlinked parent — realpath dereferences, abspath does not.
        target = link_parent / "child"
        with pytest.raises(SymlinkInChainError):
            walk_ancestors(str(target))

    def test_in_walk_lstat_detects_symlink(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Defense against TOCTOU mutation between realpath and the per-component lstat."""
        target = tmp_path / "ws"
        target.mkdir()
        target_resolved = str(target.resolve())
        real_lstat = os.lstat

        class _FakeStat:
            st_mode = stat.S_IFLNK | 0o777

        def fake_lstat(path: Any, *args: Any, **kwargs: Any) -> Any:
            if str(path) == target_resolved:
                return _FakeStat()
            return real_lstat(path, *args, **kwargs)

        monkeypatch.setattr(os, "lstat", fake_lstat)
        with pytest.raises(SymlinkInChainError):
            walk_ancestors(str(target))


class TestBoundaryRejection:
    @pytest.mark.parametrize("boundary", ["/etc", "/usr", "/var", "/tmp", "/home"])
    def test_target_in_boundary_rejected(self, boundary: str) -> None:
        with pytest.raises(BoundaryPathError):
            walk_ancestors(boundary)

    def test_user_home_rejected(self) -> None:
        with pytest.raises(BoundaryPathError):
            walk_ancestors(os.path.expanduser("~"))


class TestDepthBound:
    def test_depth_exceeded_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(walker, "MAX_DEPTH", 2)
        deep = tmp_path / "a" / "b" / "c" / "d" / "e"
        deep.mkdir(parents=True)
        with pytest.raises(DepthExceededError):
            walk_ancestors(str(deep))


class TestPerTargetIndependentWalks:
    def test_no_implicit_dedup_across_calls(self, tmp_path: Path) -> None:
        """Two siblings sharing an ancestor each get a complete walk; dedup is the caller's job."""
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        plan_a = walk_ancestors(str(a))
        plan_b = walk_ancestors(str(b))
        # Both plans contain the shared parent (tmp_path) — no planning-time dedup.
        assert tmp_path.resolve() in plan_a
        assert tmp_path.resolve() in plan_b
