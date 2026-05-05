"""Tests for core/helper_container.py — disposable helper-container primitives."""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import patch

import pytest
from core.exceptions import SandboxExecutionError
from core.helper_container import helper_chown_files, helper_mkdir_chown_dirs
from core.host_config import MachinectlAuth
from core.hydration import IMAGE_REGISTRY


def _capture_run(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []

    def _fake_run(self: object, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured.append({"cmd": cmd, **kwargs})
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("core.helper_container.Executor.run", _fake_run)
    return captured


# ─── helper_chown_files ────────────────────────────────────────────────────


class TestHelperChownFiles:
    def test_command_construction_includes_hardening(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _capture_run(monkeypatch)
        helper_chown_files(
            "claude-sandbox",
            "/inst/secrets",
            ["ipc_host_key", "authorized_keys"],
            owner_uid=1000,
            owner_gid=0,
            mode=0o600,
            machinectl_auth=MachinectlAuth.SUDO,
        )
        assert len(captured) == 1
        cmd = captured[0]["cmd"]
        assert isinstance(cmd, list)
        # machinectl prefix
        assert cmd[0] == "sudo"
        assert cmd[1] == "machinectl"
        # bash payload
        assert cmd[-2] == "-c"
        payload = cmd[-1]
        assert isinstance(payload, str)
        # hardening flags
        for flag in [
            "--runtime=runc",
            "--network=none",
            "--read-only",
            "--tmpfs /tmp",
            "--user 0:0",
            "--cap-drop ALL",
            "--cap-add CHOWN",
            "--cap-add DAC_OVERRIDE",
            "--security-opt no-new-privileges:true",
        ]:
            assert flag in payload
        # image is pinned via registry
        assert IMAGE_REGISTRY["busybox_musl"].pinned in payload
        # mount + per-file recipe
        assert "/inst/secrets:/p" in payload
        assert "ipc_host_key" in payload
        assert "authorized_keys" in payload
        assert "chown 1000:0" in payload
        assert "chmod 0600" in payload
        # sentinel + timeout passthrough
        assert captured[0]["sentinel"] is True
        assert captured[0]["timeout"] == 30

    def test_polkit_drops_sudo_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _capture_run(monkeypatch)
        helper_chown_files(
            "claude-sandbox",
            "/p",
            ["a"],
            owner_uid=1,
            owner_gid=0,
            mode=0o640,
            machinectl_auth=MachinectlAuth.POLKIT,
        )
        assert captured[0]["cmd"][0] == "machinectl"

    def test_empty_files_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _capture_run(monkeypatch)
        helper_chown_files(
            "claude-sandbox",
            "/p",
            [],
            owner_uid=1000,
            owner_gid=0,
            mode=0o640,
            machinectl_auth=MachinectlAuth.SUDO,
        )
        assert captured == []

    def test_batched_single_invocation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _capture_run(monkeypatch)
        helper_chown_files(
            "u",
            "/p",
            ["a", "b", "c", "d"],
            owner_uid=2,
            owner_gid=0,
            mode=0o640,
            machinectl_auth=MachinectlAuth.SUDO,
        )
        assert len(captured) == 1
        payload = captured[0]["cmd"][-1]
        for f in ["a", "b", "c", "d"]:
            assert f in payload

    def test_idempotent_re_invocation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _capture_run(monkeypatch)
        for _ in range(3):
            helper_chown_files(
                "u",
                "/p",
                ["x"],
                owner_uid=1,
                owner_gid=0,
                mode=0o640,
                machinectl_auth=MachinectlAuth.SUDO,
            )
        assert len(captured) == 3

    def test_custom_mode_octal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _capture_run(monkeypatch)
        helper_chown_files(
            "u",
            "/p",
            ["x"],
            owner_uid=1,
            owner_gid=0,
            mode=0o755,
            machinectl_auth=MachinectlAuth.SUDO,
        )
        payload = captured[0]["cmd"][-1]
        assert "chmod 0755" in payload

    def test_timeout_raises_diagnostic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(*a: object, **kw: object) -> None:
            raise SandboxExecutionError("timed out after 30s")

        monkeypatch.setattr("core.helper_container.Executor.run", _raise)
        with pytest.raises(SandboxExecutionError, match="timed out"):
            helper_chown_files(
                "u",
                "/p",
                ["x"],
                owner_uid=1,
                owner_gid=0,
                mode=0o640,
                machinectl_auth=MachinectlAuth.SUDO,
            )

    def test_custom_timeout_passes_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _capture_run(monkeypatch)
        helper_chown_files(
            "u",
            "/p",
            ["x"],
            owner_uid=1,
            owner_gid=0,
            mode=0o640,
            machinectl_auth=MachinectlAuth.SUDO,
            timeout=5,
        )
        assert captured[0]["timeout"] == 5


# ─── helper_mkdir_chown_dirs ──────────────────────────────────────────────


class TestHelperMkdirChownDirs:
    def test_command_construction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _capture_run(monkeypatch)
        helper_mkdir_chown_dirs(
            "claude-sandbox",
            "/inst/cache/core",
            [".claude"],
            owner_uid=100999,
            owner_gid=200999,
            machinectl_auth=MachinectlAuth.SUDO,
        )
        payload = captured[0]["cmd"][-1]
        for flag in [
            "--runtime=runc",
            "--network=none",
            "--read-only",
            "--tmpfs /tmp",
            "--user 0:0",
            "--cap-drop ALL",
            "--cap-add CHOWN",
            "--cap-add DAC_OVERRIDE",
            "--security-opt no-new-privileges:true",
        ]:
            assert flag in payload
        assert IMAGE_REGISTRY["busybox_musl"].pinned in payload
        assert "mkdir -p" in payload
        assert "chown 100999:200999" in payload
        # No chmod per Decision 14
        assert "chmod" not in payload

    def test_empty_leaves_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _capture_run(monkeypatch)
        helper_mkdir_chown_dirs(
            "u", "/p", [], owner_uid=1, owner_gid=2, machinectl_auth=MachinectlAuth.SUDO
        )
        assert captured == []

    def test_batched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _capture_run(monkeypatch)
        helper_mkdir_chown_dirs(
            "u",
            "/p",
            [".claude", "tmux_resurrect"],
            owner_uid=1,
            owner_gid=2,
            machinectl_auth=MachinectlAuth.SUDO,
        )
        assert len(captured) == 1
        payload = captured[0]["cmd"][-1]
        assert ".claude" in payload
        assert "tmux_resurrect" in payload

    def test_machinectl_wrapper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _capture_run(monkeypatch)
        helper_mkdir_chown_dirs(
            "claude-sandbox",
            "/p",
            ["x"],
            owner_uid=1,
            owner_gid=2,
            machinectl_auth=MachinectlAuth.POLKIT,
        )
        cmd = captured[0]["cmd"]
        assert cmd[:3] == ["machinectl", "shell", "claude-sandbox@.host"]


# ─── Executor timeout integration ─────────────────────────────────────────


class TestExecutorTimeout:
    """Executor.run accepts a timeout and converts TimeoutExpired to SandboxExecutionError."""

    def test_timeout_converted(self) -> None:
        from core.executor import Executor

        with patch("core.executor.subprocess.run") as run_mock:
            run_mock.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=1)
            with pytest.raises(SandboxExecutionError, match="timed out"):
                Executor().run(["/bin/true"], timeout=1)
