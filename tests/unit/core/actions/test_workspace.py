"""Tests for ``WorkspaceSharedGroupAction`` (one Action per chgrp/chmod/setfacl step)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from core.actions.context import ActionContext
from core.actions.workspace import WorkspaceSharedGroupAction
from core.exceptions import SandboxExecutionError
from core.executor import Executor
from core.host_config import MachinectlAuth


def _ctx() -> ActionContext:
    return ActionContext(
        host_user="claude-sandbox",
        auth=MachinectlAuth.SUDO,
        executor=Executor(),
        instance_dir=Path("/inst"),
    )


class TestDescribeOrdering:
    """Per-step output ordering matches today's ``_workspace_shared_group_plan``."""

    def test_chgrp_describe_format(self) -> None:
        action = WorkspaceSharedGroupAction(
            workspace_path=Path("/ws"),
            bridge_gid=200500,
            step="chgrp",
            op="chgrp 200500",
        )
        assert action.describe() == "    workspace: chgrp 200500 /ws"

    def test_chmod_describe_format(self) -> None:
        action = WorkspaceSharedGroupAction(
            workspace_path=Path("/ws"),
            bridge_gid=200500,
            step="chmod_2770",
            op="chmod 2770",
        )
        assert action.describe() == "    workspace: chmod 2770 /ws"

    def test_setfacl_effective_describe_format(self) -> None:
        action = WorkspaceSharedGroupAction(
            workspace_path=Path("/ws"),
            bridge_gid=200500,
            step="setfacl_effective",
            op="setfacl -m u:claude-sandbox:rwx",
        )
        assert action.describe() == "    workspace: setfacl -m u:claude-sandbox:rwx /ws"

    def test_setfacl_default_describe_format(self) -> None:
        op = "setfacl -d -m u::rwx,g::rwx,o::---,m::rwx,u:claude-sandbox:rwx,u:dev:rwx"
        action = WorkspaceSharedGroupAction(
            workspace_path=Path("/ws"),
            bridge_gid=200500,
            step="setfacl_default",
            op=op,
        )
        assert action.describe() == f"    workspace: {op} /ws"


class TestExecuteChgrp:
    def test_chgrp_invokes_os_chown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[tuple[str, int, int, bool]] = []

        def _fake_chown(path: str, uid: int, gid: int, follow_symlinks: bool = True) -> None:
            captured.append((path, uid, gid, follow_symlinks))

        monkeypatch.setattr("os.chown", _fake_chown)
        action = WorkspaceSharedGroupAction(
            workspace_path=Path("/ws"),
            bridge_gid=200500,
            step="chgrp",
            op="chgrp 200500",
        )
        action.execute(_ctx())
        assert captured == [("/ws", -1, 200500, False)]

    def test_chgrp_wraps_oserror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_chown(path: str, uid: int, gid: int, follow_symlinks: bool = True) -> None:
            raise OSError("EPERM")

        monkeypatch.setattr("os.chown", _fake_chown)
        action = WorkspaceSharedGroupAction(
            workspace_path=Path("/ws"),
            bridge_gid=200500,
            step="chgrp",
            op="chgrp 200500",
        )
        with pytest.raises(SandboxExecutionError) as ei:
            action.execute(_ctx())
        assert "/ws" in str(ei.value)


class TestExecuteChmod:
    def test_chmod_invokes_os_chmod(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[tuple[str, int]] = []

        def _fake_chmod(path: str, mode: int) -> None:
            captured.append((path, mode))

        monkeypatch.setattr("os.chmod", _fake_chmod)
        action = WorkspaceSharedGroupAction(
            workspace_path=Path("/ws"),
            bridge_gid=200500,
            step="chmod_2770",
            op="chmod 2770",
        )
        action.execute(_ctx())
        assert captured == [("/ws", 0o2770)]

    def test_chmod_wraps_oserror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_chmod(path: str, mode: int) -> None:
            raise OSError("EPERM")

        monkeypatch.setattr("os.chmod", _fake_chmod)
        action = WorkspaceSharedGroupAction(
            workspace_path=Path("/ws"),
            bridge_gid=200500,
            step="chmod_2770",
            op="chmod 2770",
        )
        with pytest.raises(SandboxExecutionError):
            action.execute(_ctx())


class TestExecuteSetfacl:
    def test_setfacl_effective_invokes_subprocess(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[list[str]] = []

        def _fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            captured.append(argv)
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        monkeypatch.setattr("core.actions.workspace.subprocess.run", _fake_run)
        action = WorkspaceSharedGroupAction(
            workspace_path=Path("/ws"),
            bridge_gid=200500,
            step="setfacl_effective",
            op="setfacl -m u:claude-sandbox:rwx",
        )
        action.execute(_ctx())
        assert captured == [["setfacl", "-m", "u:claude-sandbox:rwx", "/ws"]]

    def test_setfacl_default_invokes_subprocess(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[list[str]] = []

        def _fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            captured.append(argv)
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        monkeypatch.setattr("core.actions.workspace.subprocess.run", _fake_run)
        op = "setfacl -d -m u::rwx,g::rwx,o::---,m::rwx,u:claude-sandbox:rwx"
        action = WorkspaceSharedGroupAction(
            workspace_path=Path("/ws"),
            bridge_gid=200500,
            step="setfacl_default",
            op=op,
        )
        action.execute(_ctx())
        assert captured == [["setfacl", "-d", "-m", "u::rwx,g::rwx,o::---,m::rwx,u:claude-sandbox:rwx", "/ws"]]

    def test_setfacl_failure_wrapped_with_label(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.CalledProcessError(returncode=1, cmd=argv, output="", stderr="boom")

        monkeypatch.setattr("core.actions.workspace.subprocess.run", _fake_run)
        action = WorkspaceSharedGroupAction(
            workspace_path=Path("/ws"),
            bridge_gid=200500,
            step="setfacl_default",
            op="setfacl -d -m u:dev:rwx",
        )
        with pytest.raises(SandboxExecutionError) as ei:
            action.execute(_ctx())
        assert "default" in str(ei.value)
        assert "boom" in str(ei.value)

    def test_setfacl_failure_uses_exit_code_when_stderr_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.CalledProcessError(returncode=7, cmd=argv, output="", stderr="")

        monkeypatch.setattr("core.actions.workspace.subprocess.run", _fake_run)
        action = WorkspaceSharedGroupAction(
            workspace_path=Path("/ws"),
            bridge_gid=200500,
            step="setfacl_effective",
            op="setfacl -m u:claude-sandbox:rwx",
        )
        with pytest.raises(SandboxExecutionError) as ei:
            action.execute(_ctx())
        assert "effective" in str(ei.value)
        assert "exit 7" in str(ei.value)
