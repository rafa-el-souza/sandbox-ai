# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``NamedAclGrantAction`` and ``NamedAclRevokeAction``."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from core.actions.acl import NamedAclGrantAction, NamedAclRevokeAction
from core.actions.context import ActionContext
from core.exceptions import SandboxExecutionError
from core.executor import Executor


def _ctx() -> ActionContext:
    return ActionContext(
        host_user="claude-sandbox",
        executor=Executor(),
        instance_dir=Path("/inst"),
    )


def _grant(
    *,
    command: tuple[str, ...] = ("setfacl", "-m", "u:dev:rwx", "/inst"),
    description: str = "instance root: /inst",
    target: Path = Path("/inst"),
    entry: str = "u:dev:rwx",
    default: bool = False,
    recursive: bool = False,
) -> NamedAclGrantAction:
    return NamedAclGrantAction(
        command=command,
        description=description,
        target=target,
        entry=entry,
        default=default,
        recursive=recursive,
    )


def _revoke(
    *,
    command: tuple[str, ...] = ("setfacl", "-x", "u:dev", "/inst"),
    description: str = "instance root: /inst",
    target: Path = Path("/inst"),
    entry: str = "u:dev",
    default: bool = False,
) -> NamedAclRevokeAction:
    return NamedAclRevokeAction(
        command=command,
        description=description,
        target=target,
        entry=entry,
        default=default,
    )


class TestGrantDescribe:
    def test_describe_renders_effective_entry(self) -> None:
        action = _grant()
        assert action.describe() == "    $ setfacl -m u:dev:rwx /inst  # instance root: /inst"

    def test_describe_renders_default_entry(self) -> None:
        action = _grant(
            command=("setfacl", "-d", "-m", "u:dev:r", "/inst/secrets/"),
            description="secrets default ACL: /inst/secrets/",
            target=Path("/inst/secrets/"),
            entry="u:dev:r",
            default=True,
        )
        assert (
            action.describe()
            == "    $ setfacl -d -m u:dev:r /inst/secrets/  # secrets default ACL: /inst/secrets/"
        )

    def test_describe_renders_recursive_entry(self) -> None:
        action = _grant(
            command=("setfacl", "-R", "-m", "u:dev:rX", "/inst/docker/"),
            description="docker config: /inst/docker/",
            target=Path("/inst/docker/"),
            entry="u:dev:rX",
            recursive=True,
        )
        assert (
            action.describe()
            == "    $ setfacl -R -m u:dev:rX /inst/docker/  # docker config: /inst/docker/"
        )


class TestGrantExecute:
    def test_execute_invokes_setfacl_via_subprocess(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[list[str]] = []

        def _fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            captured.append(argv)
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        monkeypatch.setattr("core.actions.acl.subprocess.run", _fake_run)
        _grant().execute(_ctx())
        assert captured == [["setfacl", "-m", "u:dev:rwx", "/inst"]]

    def test_execute_raises_with_description_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.CalledProcessError(returncode=1, cmd=argv, output="", stderr="boom")

        monkeypatch.setattr("core.actions.acl.subprocess.run", _fake_run)
        with pytest.raises(SandboxExecutionError) as ei:
            _grant().execute(_ctx())
        assert "instance root: /inst" in str(ei.value)
        assert "boom" in str(ei.value)


class TestRevoke:
    def test_describe_matches_grant_format(self) -> None:
        action = _revoke()
        assert action.describe() == "    $ setfacl -x u:dev /inst  # instance root: /inst"

    def test_execute_succeeds_with_zero_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        monkeypatch.setattr("core.actions.acl.subprocess.run", _fake_run)
        _revoke().execute(_ctx())  # no raise

    def test_execute_raises_on_nonzero_with_warning_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=argv, returncode=2, stdout="", stderr="nope")

        monkeypatch.setattr("core.actions.acl.subprocess.run", _fake_run)
        with pytest.raises(SandboxExecutionError) as ei:
            _revoke().execute(_ctx())
        assert "ACL revoke warning" in str(ei.value)
        assert "instance root: /inst" in str(ei.value)
        assert "nope" in str(ei.value)

    def test_execute_raises_on_oserror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            raise OSError("missing binary")

        monkeypatch.setattr("core.actions.acl.subprocess.run", _fake_run)
        with pytest.raises(SandboxExecutionError) as ei:
            _revoke().execute(_ctx())
        assert "missing binary" in str(ei.value)

    def test_execute_uses_exit_code_when_stderr_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=argv, returncode=3, stdout="", stderr="")

        monkeypatch.setattr("core.actions.acl.subprocess.run", _fake_run)
        with pytest.raises(SandboxExecutionError) as ei:
            _revoke().execute(_ctx())
        assert "exit 3" in str(ei.value)


class TestGrantExitCodeFallback:
    def test_grant_uses_exit_code_when_stderr_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.CalledProcessError(returncode=4, cmd=argv, output="", stderr="")

        monkeypatch.setattr("core.actions.acl.subprocess.run", _fake_run)
        with pytest.raises(SandboxExecutionError) as ei:
            _grant().execute(_ctx())
        assert "exit 4" in str(ei.value)


class TestGrantStructuredFields:
    """Verify that ``_acl_grant_plan`` populates the typed fields per Decision 1."""

    def test_instance_root_grant_fields(self) -> None:
        from cli.main import _acl_grant_plan

        plan = _acl_grant_plan("/inst", "claude-sandbox")
        instance_root = next(a for a in plan if a.description == "instance root: /inst")
        assert instance_root.target == Path("/inst")
        assert instance_root.entry == "u:claude-sandbox:r-x"
        assert instance_root.default is False
        assert instance_root.recursive is False

    def test_recursive_grant_marks_recursive_field(self) -> None:
        from cli.main import _acl_grant_plan

        plan = _acl_grant_plan("/inst", "claude-sandbox")
        docker = next(a for a in plan if a.description == "docker config: /inst/docker/")
        assert docker.recursive is True
        assert docker.default is False
        assert docker.target == Path("/inst/docker/")
        assert docker.entry == "u:claude-sandbox:rX"

    def test_default_acl_grant_marks_default_field(self) -> None:
        from cli.main import _acl_grant_plan

        plan = _acl_grant_plan("/inst", "claude-sandbox")
        secrets_default = next(a for a in plan if a.description == "secrets default ACL: /inst/secrets/")
        assert secrets_default.default is True
        assert secrets_default.recursive is False
        assert secrets_default.target == Path("/inst/secrets/")
        assert "u:claude-sandbox:r" in secrets_default.entry

    def test_workspace_effective_grant_fields(self) -> None:
        from cli.main import _acl_grant_plan

        plan = _acl_grant_plan("/inst", "claude-sandbox", workspace_paths=["/ws"])
        ws_eff = next(a for a in plan if a.description == "workspace named-ACL: /ws")
        assert ws_eff.target == Path("/ws")
        assert ws_eff.entry == "u:claude-sandbox:rwx"
        assert ws_eff.default is False
        assert ws_eff.recursive is False

    def test_workspace_default_grant_fields(self) -> None:
        from cli.main import _acl_grant_plan

        plan = _acl_grant_plan("/inst", "claude-sandbox", workspace_paths=["/ws"], dev_user="dev")
        ws_default = next(a for a in plan if a.description == "workspace default ACL: /ws")
        assert ws_default.target == Path("/ws")
        assert ws_default.default is True
        assert ws_default.recursive is False
        assert "u:claude-sandbox:rwx" in ws_default.entry
        assert "u:dev:rwx" in ws_default.entry


class TestRevokeStructuredFields:
    """Verify that ``_acl_revoke_plan`` populates the typed fields per Decision 1."""

    def test_instance_root_revoke_fields(self) -> None:
        from cli.main import _acl_revoke_plan

        plan = _acl_revoke_plan("/inst", "claude-sandbox")
        instance_root = next(a for a in plan if a.description == "instance root: /inst")
        assert instance_root.target == Path("/inst")
        assert instance_root.entry == "u:claude-sandbox"
        assert instance_root.default is False

    def test_secrets_default_revoke_marks_default_field(self) -> None:
        from cli.main import _acl_revoke_plan

        plan = _acl_revoke_plan("/inst", "claude-sandbox")
        secrets_default = next(a for a in plan if a.description == "secrets default ACL: /inst/secrets/")
        assert secrets_default.default is True
        assert secrets_default.target == Path("/inst/secrets/")
        assert secrets_default.entry == "u:claude-sandbox"

    def test_workspace_default_revoke_marks_default_field(self) -> None:
        from cli.main import _acl_revoke_plan

        plan = _acl_revoke_plan("/inst", "claude-sandbox", workspace_paths=["/ws"])
        ws_default = next(a for a in plan if a.description == "workspace default named entry: /ws")
        assert ws_default.default is True
        assert ws_default.target == Path("/ws")
        assert ws_default.entry == "u:claude-sandbox"

    def test_workspace_effective_revoke_fields(self) -> None:
        from cli.main import _acl_revoke_plan

        plan = _acl_revoke_plan("/inst", "claude-sandbox", workspace_paths=["/ws"])
        ws_eff = next(a for a in plan if a.description == "workspace named-ACL: /ws")
        assert ws_eff.target == Path("/ws")
        assert ws_eff.entry == "u:claude-sandbox"
        assert ws_eff.default is False
