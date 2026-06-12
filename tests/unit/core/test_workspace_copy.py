# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for core.workspace_copy — pre-copy lstat scan + rsync invocation."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from core.workspace_copy import (
    COPY_DEFAULT_EXCLUDES,
    UnsafeSymlinkError,
    copy_workspace,
    scan_unsafe_symlinks,
)


class TestScanUnsafeSymlinks:
    def test_returns_empty_for_clean_tree(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("hi")
        assert scan_unsafe_symlinks(str(tmp_path)) == []

    def test_internal_symlink_is_safe(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("hi")
        (tmp_path / "link").symlink_to("a.txt")
        assert scan_unsafe_symlinks(str(tmp_path)) == []

    def test_absolute_external_symlink_is_unsafe(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside-target"
        outside.write_text("x")
        (tmp_path / "evil").symlink_to(outside)
        unsafe = scan_unsafe_symlinks(str(tmp_path))
        assert unsafe == [str(tmp_path / "evil")]

    def test_relative_external_symlink_is_unsafe(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "rel-outside"
        outside.write_text("x")
        (tmp_path / "evil").symlink_to(os.path.join("..", outside.name))
        unsafe = scan_unsafe_symlinks(str(tmp_path))
        assert unsafe == [str(tmp_path / "evil")]


class TestCopyWorkspace:
    def test_refuses_unsafe_symlinks_by_default(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        outside = tmp_path / "outside"
        outside.write_text("x")
        (src / "bad").symlink_to(outside)
        dest = tmp_path / "dest"
        with pytest.raises(UnsafeSymlinkError):
            copy_workspace(str(src), str(dest))

    @patch("core.workspace_copy.subprocess.run")
    def test_invokes_rsync_with_default_excludes(self, mock_run: MagicMock, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("hi")
        dest = tmp_path / "dest"

        copy_workspace(str(src), str(dest))

        mock_run.assert_called_once()
        cmd = mock_run.call_args.args[0]
        assert cmd[:4] == ["rsync", "-a", "--no-owner", "--no-group"]
        for exc in COPY_DEFAULT_EXCLUDES:
            assert exc in cmd

    @patch("core.workspace_copy.subprocess.run")
    def test_strip_unsafe_links_adds_safe_links_flag(self, mock_run: MagicMock, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        outside = tmp_path / "outside"
        outside.write_text("x")
        (src / "bad").symlink_to(outside)
        dest = tmp_path / "dest"

        result = copy_workspace(str(src), str(dest), strip_unsafe_links=True)

        cmd = mock_run.call_args.args[0]
        assert "--safe-links" in cmd
        assert result.safe_links is True

    @patch("core.workspace_copy.subprocess.run")
    def test_returns_copy_result_with_excludes(self, mock_run: MagicMock, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "a").write_text("x")
        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / "a").write_text("x")  # Pretend rsync wrote something.

        result = copy_workspace(str(src), str(dest))

        assert result.excludes_applied == COPY_DEFAULT_EXCLUDES
        assert result.bytes_total >= 1
        assert mock_run.called

    @patch(
        "core.workspace_copy.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, ["rsync"]),
    )
    def test_propagates_rsync_failure(self, _mock_run: MagicMock, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        with pytest.raises(subprocess.CalledProcessError):
            copy_workspace(str(src), str(tmp_path / "dest"))
