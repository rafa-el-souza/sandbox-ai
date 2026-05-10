"""Tests for ``NamedAclGrantAction`` and ``NamedAclRevokeAction``."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from core.actions.acl import NamedAclGrantAction, NamedAclRevokeAction
from core.actions.context import ActionContext
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


class TestGrantDescribe:
    def test_describe_renders_effective_entry(self) -> None:
        action = NamedAclGrantAction(
            command=("setfacl", "-m", "u:dev:rwx", "/inst"),
            description="instance root: /inst",
        )
        assert action.describe() == "    $ setfacl -m u:dev:rwx /inst  # instance root: /inst"

    def test_describe_renders_default_entry(self) -> None:
        action = NamedAclGrantAction(
            command=("setfacl", "-d", "-m", "u:dev:r", "/inst/secrets/"),
            description="secrets default ACL: /inst/secrets/",
        )
        assert (
            action.describe()
            == "    $ setfacl -d -m u:dev:r /inst/secrets/  # secrets default ACL: /inst/secrets/"
        )

    def test_describe_renders_recursive_entry(self) -> None:
        action = NamedAclGrantAction(
            command=("setfacl", "-R", "-m", "u:dev:rX", "/inst/docker/"),
            description="docker config: /inst/docker/",
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
        action = NamedAclGrantAction(
            command=("setfacl", "-m", "u:dev:rwx", "/inst"),
            description="instance root: /inst",
        )
        action.execute(_ctx())
        assert captured == [["setfacl", "-m", "u:dev:rwx", "/inst"]]

    def test_execute_raises_with_description_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.CalledProcessError(returncode=1, cmd=argv, output="", stderr="boom")

        monkeypatch.setattr("core.actions.acl.subprocess.run", _fake_run)
        action = NamedAclGrantAction(
            command=("setfacl", "-m", "u:dev:rwx", "/inst"),
            description="instance root: /inst",
        )
        with pytest.raises(SandboxExecutionError) as ei:
            action.execute(_ctx())
        assert "instance root: /inst" in str(ei.value)
        assert "boom" in str(ei.value)


class TestRevoke:
    def test_describe_matches_grant_format(self) -> None:
        action = NamedAclRevokeAction(
            command=("setfacl", "-x", "u:dev", "/inst"),
            description="instance root: /inst",
        )
        assert action.describe() == "    $ setfacl -x u:dev /inst  # instance root: /inst"

    def test_execute_succeeds_with_zero_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        monkeypatch.setattr("core.actions.acl.subprocess.run", _fake_run)
        action = NamedAclRevokeAction(
            command=("setfacl", "-x", "u:dev", "/inst"),
            description="instance root: /inst",
        )
        action.execute(_ctx())  # no raise

    def test_execute_raises_on_nonzero_with_warning_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=argv, returncode=2, stdout="", stderr="nope")

        monkeypatch.setattr("core.actions.acl.subprocess.run", _fake_run)
        action = NamedAclRevokeAction(
            command=("setfacl", "-x", "u:dev", "/inst"),
            description="instance root: /inst",
        )
        with pytest.raises(SandboxExecutionError) as ei:
            action.execute(_ctx())
        assert "ACL revoke warning" in str(ei.value)
        assert "instance root: /inst" in str(ei.value)
        assert "nope" in str(ei.value)

    def test_execute_raises_on_oserror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            raise OSError("missing binary")

        monkeypatch.setattr("core.actions.acl.subprocess.run", _fake_run)
        action = NamedAclRevokeAction(
            command=("setfacl", "-x", "u:dev", "/inst"),
            description="instance root: /inst",
        )
        with pytest.raises(SandboxExecutionError) as ei:
            action.execute(_ctx())
        assert "missing binary" in str(ei.value)

    def test_execute_uses_exit_code_when_stderr_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=argv, returncode=3, stdout="", stderr="")

        monkeypatch.setattr("core.actions.acl.subprocess.run", _fake_run)
        action = NamedAclRevokeAction(
            command=("setfacl", "-x", "u:dev", "/inst"),
            description="instance root: /inst",
        )
        with pytest.raises(SandboxExecutionError) as ei:
            action.execute(_ctx())
        assert "exit 3" in str(ei.value)


class TestGrantExitCodeFallback:
    def test_grant_uses_exit_code_when_stderr_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.CalledProcessError(returncode=4, cmd=argv, output="", stderr="")

        monkeypatch.setattr("core.actions.acl.subprocess.run", _fake_run)
        action = NamedAclGrantAction(
            command=("setfacl", "-m", "u:dev:rwx", "/inst"),
            description="instance root: /inst",
        )
        with pytest.raises(SandboxExecutionError) as ei:
            action.execute(_ctx())
        assert "exit 4" in str(ei.value)
