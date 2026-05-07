"""Tests for core/helper_container.py — disposable helper-container primitives.

Mocking policy (per fix-helper-container-userns design D5): every
``Executor.run`` mock here covers **argv-shape only** — flag presence, image
pin, batching, machinectl prefix, chown-substring after host→in-container
translation. End-to-end ownership semantics (host-absolute uid/gid actually
landing on disk via the daemon's userns translation) are exercised in
``tests/integration/test_helper_container_userns.py``, which the helper-
container capability requires as a manually-invocable pre-merge gate.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from core.exceptions import SandboxExecutionError
from core.executor import Executor
from core.helper_container import helper_chown_files, helper_mkdir_chown_dirs
from core.host_config import MachinectlAuth, SubuidOutOfRangeError
from core.hydration import IMAGE_REGISTRY

# Standard fixture matching the change's spec scenarios:
# /etc/subuid and /etc/subgid both have ``claude-sandbox:165536:65536``.
# Host uid 166535 ↔ in-container uid 1000 (the consumer uid pattern).
_HOST_USER = "claude-sandbox"
_SUBID_BODY = f"{_HOST_USER}:165536:65536\n"
_HOST_UID = 166535
_HOST_GID = 166535
_IN_CONTAINER_UID = 1000
_IN_CONTAINER_GID = 1000


@pytest.fixture
def subid_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point /etc/subuid and /etc/subgid at the standard test fixture body."""
    subuid = tmp_path / "subuid"
    subuid.write_text(_SUBID_BODY)
    monkeypatch.setattr("core.host_config._SUBUID_PATH", subuid)
    subgid = tmp_path / "subgid"
    subgid.write_text(_SUBID_BODY)
    monkeypatch.setattr("core.host_config._SUBGID_PATH", subgid)


@pytest.fixture
def captured_executor() -> Iterator[list[dict[str, Any]]]:
    """Patch ``Executor.run`` with autospec — signature drift fails the test loudly.

    Argv-shape mock only; ownership exercised in
    ``tests/integration/test_helper_container_userns.py``.
    """
    captured: list[dict[str, Any]] = []

    def _capture(self: Executor, cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured.append({"cmd": cmd, **kwargs})
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with patch.object(Executor, "run", autospec=True, side_effect=_capture):
        yield captured


# ─── helper_chown_files ────────────────────────────────────────────────────


class TestHelperChownFiles:
    def test_command_construction_includes_hardening(
        self, subid_fixture: None, captured_executor: list[dict[str, Any]]
    ) -> None:
        helper_chown_files(
            _HOST_USER,
            "/inst/secrets",
            ["ipc_host_key", "authorized_keys"],
            owner_uid=_HOST_UID,
            owner_gid=_HOST_GID,
            mode=0o600,
            machinectl_auth=MachinectlAuth.SUDO,
        )
        assert len(captured_executor) == 1
        cmd = captured_executor[0]["cmd"]
        assert isinstance(cmd, list)
        assert cmd[0] == "sudo"
        assert cmd[1] == "machinectl"
        assert cmd[-2] == "-c"
        payload = cmd[-1]
        assert isinstance(payload, str)
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
        # --userns=host MUST NOT appear (D1: translation, not bypass).
        assert "--userns=host" not in payload
        assert IMAGE_REGISTRY["busybox_musl"].pinned in payload
        assert "/inst/secrets:/p" in payload
        assert "ipc_host_key" in payload
        assert "authorized_keys" in payload
        # Translated in-container values, not host-absolute.
        assert f"chown {_IN_CONTAINER_UID}:{_IN_CONTAINER_GID}" in payload
        assert str(_HOST_UID) not in payload
        assert "chmod 0600" in payload
        assert captured_executor[0]["sentinel"] is True
        assert captured_executor[0]["timeout"] == 30

    def test_polkit_drops_sudo_prefix(
        self, subid_fixture: None, captured_executor: list[dict[str, Any]]
    ) -> None:
        helper_chown_files(
            _HOST_USER,
            "/p",
            ["a"],
            owner_uid=_HOST_UID,
            owner_gid=_HOST_GID,
            mode=0o640,
            machinectl_auth=MachinectlAuth.POLKIT,
        )
        assert captured_executor[0]["cmd"][0] == "machinectl"

    def test_empty_files_is_noop_skips_translation(
        self, captured_executor: list[dict[str, Any]]
    ) -> None:
        # Empty input short-circuits before any subuid lookup — passing
        # out-of-range values should NOT raise (no docker run happens either).
        helper_chown_files(
            _HOST_USER,
            "/p",
            [],
            owner_uid=99999999,
            owner_gid=99999999,
            mode=0o640,
            machinectl_auth=MachinectlAuth.SUDO,
        )
        assert captured_executor == []

    def test_batched_single_invocation(
        self, subid_fixture: None, captured_executor: list[dict[str, Any]]
    ) -> None:
        helper_chown_files(
            _HOST_USER,
            "/p",
            ["a", "b", "c", "d"],
            owner_uid=_HOST_UID,
            owner_gid=_HOST_GID,
            mode=0o640,
            machinectl_auth=MachinectlAuth.SUDO,
        )
        assert len(captured_executor) == 1
        payload = captured_executor[0]["cmd"][-1]
        for f in ["a", "b", "c", "d"]:
            assert f in payload

    def test_idempotent_re_invocation(
        self, subid_fixture: None, captured_executor: list[dict[str, Any]]
    ) -> None:
        for _ in range(3):
            helper_chown_files(
                _HOST_USER,
                "/p",
                ["x"],
                owner_uid=_HOST_UID,
                owner_gid=_HOST_GID,
                mode=0o640,
                machinectl_auth=MachinectlAuth.SUDO,
            )
        assert len(captured_executor) == 3

    def test_custom_mode_octal(
        self, subid_fixture: None, captured_executor: list[dict[str, Any]]
    ) -> None:
        helper_chown_files(
            _HOST_USER,
            "/p",
            ["x"],
            owner_uid=_HOST_UID,
            owner_gid=_HOST_GID,
            mode=0o755,
            machinectl_auth=MachinectlAuth.SUDO,
        )
        payload = captured_executor[0]["cmd"][-1]
        assert "chmod 0755" in payload

    def test_translation_host_absolute_to_in_container(
        self, subid_fixture: None, captured_executor: list[dict[str, Any]]
    ) -> None:
        # Spec scenario: owner_uid=166535, owner_gid=166535 → "chown 1000:1000".
        helper_chown_files(
            _HOST_USER,
            "/p",
            ["x"],
            owner_uid=_HOST_UID,
            owner_gid=_HOST_GID,
            mode=0o640,
            machinectl_auth=MachinectlAuth.SUDO,
        )
        payload = captured_executor[0]["cmd"][-1]
        assert f"chown {_IN_CONTAINER_UID}:{_IN_CONTAINER_GID}" in payload

    def test_out_of_range_owner_uid_raises_before_docker_run(
        self, subid_fixture: None
    ) -> None:
        with patch.object(Executor, "run", autospec=True) as run_mock:
            with pytest.raises(SubuidOutOfRangeError):
                helper_chown_files(
                    _HOST_USER,
                    "/p",
                    ["x"],
                    owner_uid=10,  # below the 165536..231071 range
                    owner_gid=_HOST_GID,
                    mode=0o640,
                    machinectl_auth=MachinectlAuth.SUDO,
                )
            run_mock.assert_not_called()

    def test_timeout_raises_diagnostic(self, subid_fixture: None) -> None:
        """Executor.run raising SandboxExecutionError ('timed out …') propagates."""

        def _raise(self: Executor, cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            raise SandboxExecutionError("timed out after 30s")

        with (
            patch.object(Executor, "run", autospec=True, side_effect=_raise),
            pytest.raises(SandboxExecutionError, match="timed out"),
        ):
            helper_chown_files(
                _HOST_USER,
                "/p",
                ["x"],
                owner_uid=_HOST_UID,
                owner_gid=_HOST_GID,
                mode=0o640,
                machinectl_auth=MachinectlAuth.SUDO,
            )

    def test_custom_timeout_passes_through(
        self, subid_fixture: None, captured_executor: list[dict[str, Any]]
    ) -> None:
        helper_chown_files(
            _HOST_USER,
            "/p",
            ["x"],
            owner_uid=_HOST_UID,
            owner_gid=_HOST_GID,
            mode=0o640,
            machinectl_auth=MachinectlAuth.SUDO,
            timeout=5,
        )
        assert captured_executor[0]["timeout"] == 5


# ─── helper_mkdir_chown_dirs ──────────────────────────────────────────────


class TestHelperMkdirChownDirs:
    def test_command_construction(
        self, subid_fixture: None, captured_executor: list[dict[str, Any]]
    ) -> None:
        helper_mkdir_chown_dirs(
            _HOST_USER,
            "/inst/cache/core",
            [".claude"],
            owner_uid=_HOST_UID,
            owner_gid=_HOST_GID,
            machinectl_auth=MachinectlAuth.SUDO,
        )
        payload = captured_executor[0]["cmd"][-1]
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
        assert "--userns=host" not in payload
        assert IMAGE_REGISTRY["busybox_musl"].pinned in payload
        assert "mkdir -p" in payload
        # Translated, not host-absolute.
        assert f"chown {_IN_CONTAINER_UID}:{_IN_CONTAINER_GID}" in payload
        assert str(_HOST_UID) not in payload
        # No chmod per Decision 14.
        assert "chmod" not in payload

    def test_empty_leaves_is_noop_skips_translation(
        self, captured_executor: list[dict[str, Any]]
    ) -> None:
        helper_mkdir_chown_dirs(
            _HOST_USER,
            "/p",
            [],
            owner_uid=99999999,
            owner_gid=99999999,
            machinectl_auth=MachinectlAuth.SUDO,
        )
        assert captured_executor == []

    def test_batched(self, subid_fixture: None, captured_executor: list[dict[str, Any]]) -> None:
        helper_mkdir_chown_dirs(
            _HOST_USER,
            "/p",
            [".claude", "tmux_resurrect"],
            owner_uid=_HOST_UID,
            owner_gid=_HOST_GID,
            machinectl_auth=MachinectlAuth.SUDO,
        )
        assert len(captured_executor) == 1
        payload = captured_executor[0]["cmd"][-1]
        assert ".claude" in payload
        assert "tmux_resurrect" in payload

    def test_machinectl_wrapper(
        self, subid_fixture: None, captured_executor: list[dict[str, Any]]
    ) -> None:
        helper_mkdir_chown_dirs(
            _HOST_USER,
            "/p",
            ["x"],
            owner_uid=_HOST_UID,
            owner_gid=_HOST_GID,
            machinectl_auth=MachinectlAuth.POLKIT,
        )
        cmd = captured_executor[0]["cmd"]
        assert cmd[:3] == ["machinectl", "shell", f"{_HOST_USER}@.host"]

    def test_translation_host_absolute_to_in_container(
        self, subid_fixture: None, captured_executor: list[dict[str, Any]]
    ) -> None:
        # Spec scenario for the dirs primitive — same expectation as files.
        helper_mkdir_chown_dirs(
            _HOST_USER,
            "/p",
            ["d"],
            owner_uid=_HOST_UID,
            owner_gid=_HOST_GID,
            machinectl_auth=MachinectlAuth.SUDO,
        )
        payload = captured_executor[0]["cmd"][-1]
        assert f"chown {_IN_CONTAINER_UID}:{_IN_CONTAINER_GID}" in payload

    def test_out_of_range_owner_uid_raises_before_docker_run(
        self, subid_fixture: None
    ) -> None:
        with patch.object(Executor, "run", autospec=True) as run_mock:
            with pytest.raises(SubuidOutOfRangeError):
                helper_mkdir_chown_dirs(
                    _HOST_USER,
                    "/p",
                    ["x"],
                    owner_uid=10,
                    owner_gid=_HOST_GID,
                    machinectl_auth=MachinectlAuth.SUDO,
                )
            run_mock.assert_not_called()


# ─── Executor timeout integration ─────────────────────────────────────────


class TestExecutorTimeout:
    """Executor.run accepts a timeout and converts TimeoutExpired to SandboxExecutionError."""

    def test_timeout_converted(self) -> None:
        with patch("core.executor.subprocess.run") as run_mock:
            run_mock.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=1)
            with pytest.raises(SandboxExecutionError, match="timed out"):
                Executor().run(["/bin/true"], timeout=1)
