# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``WorkspaceSharedGroupAction`` (one Action per chgrp/chmod/setfacl step)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from core.actions.context import ActionContext
from core.actions.workspace import WorkspaceSharedGroupAction, WorkspaceSharedGroupStep
from core.exceptions import SandboxExecutionError
from core.executor import Executor


def _ctx() -> ActionContext:
    return ActionContext(
        host_user="claude-sandbox",
        executor=Executor(),
        instance_dir=Path("/inst"),
    )


def _action(
    *,
    workspace_path: Path = Path("/ws"),
    bridge_gid: int = 200500,
    step: WorkspaceSharedGroupStep = "chgrp",
    op: str = "chgrp 200500",
    command: tuple[str, ...] = (),
) -> WorkspaceSharedGroupAction:
    return WorkspaceSharedGroupAction(
        workspace_path=workspace_path,
        bridge_gid=bridge_gid,
        step=step,
        op=op,
        command=command,
    )


class TestDescribeOrdering:
    """Per-step output ordering matches today's ``_workspace_shared_group_plan``."""

    def test_chgrp_describe_format(self) -> None:
        action = _action(step="chgrp", op="chgrp 200500")
        assert action.describe() == "    workspace: chgrp 200500 /ws"

    def test_chmod_describe_format(self) -> None:
        action = _action(step="chmod_2770", op="chmod 2770")
        assert action.describe() == "    workspace: chmod 2770 /ws"

    def test_setfacl_effective_describe_format(self) -> None:
        action = _action(
            step="setfacl_effective",
            op="setfacl -m u:claude-sandbox:rwx",
            command=("setfacl", "-m", "u:claude-sandbox:rwx", "/ws"),
        )
        assert action.describe() == "    workspace: setfacl -m u:claude-sandbox:rwx /ws"

    def test_setfacl_default_describe_format(self) -> None:
        op = "setfacl -d -m u::rwx,g::rwx,o::---,m::rwx,u:claude-sandbox:rwx,u:dev:rwx"
        action = _action(
            step="setfacl_default",
            op=op,
            command=("setfacl", "-d", "-m", "u::rwx,g::rwx,o::---,m::rwx,u:claude-sandbox:rwx,u:dev:rwx", "/ws"),
        )
        assert action.describe() == f"    workspace: {op} /ws"


class TestExecuteChgrp:
    def test_chgrp_invokes_os_chown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[tuple[str, int, int, bool]] = []

        def _fake_chown(path: str, uid: int, gid: int, follow_symlinks: bool = True) -> None:
            captured.append((path, uid, gid, follow_symlinks))

        monkeypatch.setattr("os.chown", _fake_chown)
        _action(step="chgrp", op="chgrp 200500").execute(_ctx())
        assert captured == [("/ws", -1, 200500, False)]

    def test_chgrp_wraps_oserror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_chown(path: str, uid: int, gid: int, follow_symlinks: bool = True) -> None:
            raise OSError("EPERM")

        monkeypatch.setattr("os.chown", _fake_chown)
        with pytest.raises(SandboxExecutionError) as ei:
            _action(step="chgrp", op="chgrp 200500").execute(_ctx())
        assert "/ws" in str(ei.value)


class TestExecuteChmod:
    def test_chmod_invokes_os_chmod(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[tuple[str, int]] = []

        def _fake_chmod(path: str, mode: int) -> None:
            captured.append((path, mode))

        monkeypatch.setattr("os.chmod", _fake_chmod)
        _action(step="chmod_2770", op="chmod 2770").execute(_ctx())
        assert captured == [("/ws", 0o2770)]

    def test_chmod_wraps_oserror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_chmod(path: str, mode: int) -> None:
            raise OSError("EPERM")

        monkeypatch.setattr("os.chmod", _fake_chmod)
        with pytest.raises(SandboxExecutionError):
            _action(step="chmod_2770", op="chmod 2770").execute(_ctx())


class TestExecuteSetfacl:
    def test_setfacl_effective_invokes_subprocess(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[list[str]] = []

        def _fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            captured.append(argv)
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        monkeypatch.setattr("core.actions.workspace.subprocess.run", _fake_run)
        _action(
            step="setfacl_effective",
            op="setfacl -m u:claude-sandbox:rwx",
            command=("setfacl", "-m", "u:claude-sandbox:rwx", "/ws"),
        ).execute(_ctx())
        assert captured == [["setfacl", "-m", "u:claude-sandbox:rwx", "/ws"]]

    def test_setfacl_default_invokes_subprocess(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[list[str]] = []

        def _fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            captured.append(argv)
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        monkeypatch.setattr("core.actions.workspace.subprocess.run", _fake_run)
        op = "setfacl -d -m u::rwx,g::rwx,o::---,m::rwx,u:claude-sandbox:rwx"
        _action(
            step="setfacl_default",
            op=op,
            command=("setfacl", "-d", "-m", "u::rwx,g::rwx,o::---,m::rwx,u:claude-sandbox:rwx", "/ws"),
        ).execute(_ctx())
        assert captured == [["setfacl", "-d", "-m", "u::rwx,g::rwx,o::---,m::rwx,u:claude-sandbox:rwx", "/ws"]]

    def test_setfacl_failure_wrapped_with_label(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.CalledProcessError(returncode=1, cmd=argv, output="", stderr="boom")

        monkeypatch.setattr("core.actions.workspace.subprocess.run", _fake_run)
        with pytest.raises(SandboxExecutionError) as ei:
            _action(
                step="setfacl_default",
                op="setfacl -d -m u:dev:rwx",
                command=("setfacl", "-d", "-m", "u:dev:rwx", "/ws"),
            ).execute(_ctx())
        assert "default" in str(ei.value)
        assert "boom" in str(ei.value)

    def test_setfacl_failure_uses_exit_code_when_stderr_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.CalledProcessError(returncode=7, cmd=argv, output="", stderr="")

        monkeypatch.setattr("core.actions.workspace.subprocess.run", _fake_run)
        with pytest.raises(SandboxExecutionError) as ei:
            _action(
                step="setfacl_effective",
                op="setfacl -m u:claude-sandbox:rwx",
                command=("setfacl", "-m", "u:claude-sandbox:rwx", "/ws"),
            ).execute(_ctx())
        assert "effective" in str(ei.value)
        assert "exit 7" in str(ei.value)


class TestPlanCommandField:
    """Verify ``_workspace_shared_group_plan`` precomputes the ``command`` argv.

    Replaces the prior ``op.split(" ")`` parsing in ``.execute()``: the argv is
    now produced at plan-construction time so the textual ``op`` field is
    description-only.
    """

    def test_chgrp_step_has_empty_command(self) -> None:
        from cli.main import _workspace_shared_group_plan

        plan = _workspace_shared_group_plan("/ws", 200500, None, "claude-sandbox")
        chgrp = next(a for a in plan if a.step == "chgrp")
        assert chgrp.command == ()

    def test_chmod_step_has_empty_command(self) -> None:
        from cli.main import _workspace_shared_group_plan

        plan = _workspace_shared_group_plan("/ws", 200500, None, "claude-sandbox")
        chmod = next(a for a in plan if a.step == "chmod_2770")
        assert chmod.command == ()

    def test_setfacl_effective_command_matches_op_split(self) -> None:
        from cli.main import _workspace_shared_group_plan

        plan = _workspace_shared_group_plan("/ws", 200500, None, "claude-sandbox")
        eff = next(a for a in plan if a.step == "setfacl_effective")
        # The new command field carries the same argv that the old op.split + ws
        # append produced.
        assert eff.command == ("setfacl", "-m", "u:claude-sandbox:rwx", "/ws")
        assert tuple(eff.command) == (*eff.op.split(" "), "/ws")

    def test_setfacl_default_command_matches_op_split_with_dev(self) -> None:
        from cli.main import _workspace_shared_group_plan

        plan = _workspace_shared_group_plan("/ws", 200500, "dev", "claude-sandbox")
        default = next(a for a in plan if a.step == "setfacl_default")
        expected_entry = "u::rwx,g::rwx,o::---,m::rwx,u:claude-sandbox:rwx,u:dev:rwx"
        assert default.command == ("setfacl", "-d", "-m", expected_entry, "/ws")
        assert tuple(default.command) == (*default.op.split(" "), "/ws")
