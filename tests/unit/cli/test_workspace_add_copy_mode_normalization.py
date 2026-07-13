# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for `sandbox workspace add --copy`'s 0700 mode-normalization contract.

`--empty` already produces 0700 via `mkdir(..., mode=0o700)`. Pre-fix
`--copy` inherited the source mode through rsync's `-a` flag, so a `0775`
source produced a `0775` workspace, undermining the privacy default.
The fix calls `os.chmod(<ws-root>, 0o700)` after rsync returns.

Shared fixtures (``register``, ``user_home``, autouse ``seed_registry``,
``runner``, etc.) live in ``tests/unit/cli/conftest.py``.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from typer.testing import CliRunner


def _mode_bits(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def _simulate_rsync_inheriting_source_mode(src: str, dst: str) -> None:
    """Stand-in for `core.workspace_copy.copy_workspace`.

    The real rsync invocation uses `-a` which preserves source mode. The
    pre-fix bug class is that this same mode lands on the workspace
    directory unchanged. We simulate by chmod'ing the dst to the src mode
    after the fixture mkdir(0o700) — exactly what rsync would do.
    """
    src_mode = _mode_bits(Path(src))
    os.chmod(dst, src_mode)


class TestCopyModeNormalization:
    def test_copy_from_0775_source_produces_0700_workspace(
        self,
        runner: CliRunner,
        register: Callable[..., Path],
        user_home: Path,
        tmp_path: Path,
    ) -> None:
        from cli.main import app

        register("foo", workspaces=[("main", "empty", None)])
        src = tmp_path / "src"
        src.mkdir(mode=0o775)
        os.chmod(src, 0o775)  # mkdir's mode is umask-filtered; force 0775 explicitly.
        (src / "file.txt").write_text("data")

        with patch("cli.main.copy_workspace", side_effect=_simulate_rsync_inheriting_source_mode):
            result = runner.invoke(
                app,
                ["workspace", "add", "foo", "--copy", f"backend={src}"],
            )

        assert result.exit_code == 0, result.output
        ws_path = user_home / "workspaces" / "foo" / "backend"
        assert ws_path.is_dir()
        # Source was 0775, but the workspace MUST be normalized to 0700.
        assert _mode_bits(ws_path) == 0o700, (
            f"copy-mode inherited source mode: {oct(_mode_bits(ws_path))}"
        )

    def test_empty_mode_produces_0700_workspace(
        self,
        runner: CliRunner,
        register: Callable[..., Path],
        user_home: Path,
    ) -> None:
        """Parity / regression guard: --empty has always produced 0700 via mkdir."""
        from cli.main import app

        register("foo", workspaces=[("main", "empty", None)])

        result = runner.invoke(app, ["workspace", "add", "foo", "--empty", "extra"])

        assert result.exit_code == 0, result.output
        ws_path = user_home / "workspaces" / "foo" / "extra"
        assert ws_path.is_dir()
        assert _mode_bits(ws_path) == 0o700
