"""Tests for `sandbox workspace remove`'s refuse-last-workspace contract.

The refusal MUST precede the `--backup`/`--purge` branching: no rsync,
no rmtree, no `mutate_workspaces` call, no backup directory created.
The error message MUST surface both supported replacement paths
(add-then-remove for swap workflows; destroy for instance removal).

Shared fixtures (``register``, ``user_home``, autouse ``seed_registry``,
``runner``, etc.) live in ``tests/unit/cli/conftest.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from typer.testing import CliRunner


_REFUSAL_FRAGMENT = "Cannot remove the last workspace from"
_REPLACEMENT_HINT = "Add a replacement workspace first"
_DESTROY_HINT = "use 'sandbox destroy"


class TestRefuseLastWorkspace:
    def test_purge_on_only_workspace_exits_with_documented_message(
        self,
        runner: CliRunner,
        register: Callable[..., Path],
        user_home: Path,
    ) -> None:
        from cli.main import app

        instance_dir = register("foo", workspaces=[("main", "empty", None)])
        ws_path = user_home / "workspaces" / "foo" / "main"
        toml_before = (instance_dir / "sandbox.toml").read_text()

        # Mocks must register zero calls — the refusal precedes them.
        with (
            patch("cli.main.create_backup") as mock_backup,
            patch("cli.main.mutate_workspaces") as mock_mutate,
            patch("cli.main.shutil.rmtree") as mock_rmtree,
        ):
            result = runner.invoke(app, ["workspace", "remove", "foo", "main", "--purge"])

        assert result.exit_code == 1
        # Documented message contents — the three load-bearing fragments.
        # Flatten Rich's soft-wrap whitespace so substring checks survive a narrow capture width.
        flat = " ".join(result.output.split())
        assert _REFUSAL_FRAGMENT in flat
        assert "'foo'" in flat
        assert _REPLACEMENT_HINT in flat
        assert _DESTROY_HINT in flat
        # No state mutation observable.
        mock_backup.assert_not_called()
        mock_mutate.assert_not_called()
        mock_rmtree.assert_not_called()
        assert ws_path.is_dir()
        assert (instance_dir / "sandbox.toml").read_text() == toml_before

    def test_backup_on_only_workspace_refuses_without_creating_backup(
        self,
        runner: CliRunner,
        register: Callable[..., Path],
        user_home: Path,
    ) -> None:
        from cli.main import app

        instance_dir = register("foo", workspaces=[("main", "empty", None)])
        ws_path = user_home / "workspaces" / "foo" / "main"
        toml_before = (instance_dir / "sandbox.toml").read_text()
        backups_root = user_home / "workspaces" / "_backups"

        with (
            patch("cli.main.create_backup") as mock_backup,
            patch("cli.main.mutate_workspaces") as mock_mutate,
            patch("cli.main.shutil.rmtree") as mock_rmtree,
        ):
            result = runner.invoke(app, ["workspace", "remove", "foo", "main", "--backup"])

        assert result.exit_code == 1
        flat = " ".join(result.output.split())
        assert _REFUSAL_FRAGMENT in flat
        # The refusal precedes the backup path — no backup invoked.
        mock_backup.assert_not_called()
        mock_mutate.assert_not_called()
        mock_rmtree.assert_not_called()
        # No backup directory exists for this instance/workspace.
        assert not backups_root.exists() or not (backups_root / "foo").exists()
        # Workspace tree still on disk.
        assert ws_path.is_dir()
        assert (instance_dir / "sandbox.toml").read_text() == toml_before
