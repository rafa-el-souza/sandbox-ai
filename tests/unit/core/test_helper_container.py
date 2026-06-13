# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for core/helper_container.py — disposable helper-container primitives.

Mocking policy: post-``runtime-dispatcher`` (C-001) the helper primitives are
thin wrappers that translate host→in-container uid/gid and route through
``core.dispatch.invoke`` (the single boundary-crossing entry point). These tests
mock ``core.dispatch.invoke`` at the helper boundary and cover **call-shape
only** — op name, the already-userns-translated uid/gid, parent/mode/leaf args,
the raise-on-failure contract, and the timeout pass-through. The byte-faithful
hardened ``docker run`` target argv is produced once by ``core.dispatch``'s
op-builder (which reuses ``helper_container.hardened_docker_run``) and is
covered by the dispatch fixture-parity suite; end-to-end ownership semantics
(host-absolute uid/gid actually landing on disk via the daemon's userns
translation) are exercised in
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
from core import dispatch
from core.exceptions import SandboxExecutionError
from core.executor import Executor
from core.helper_container import helper_chown_files, helper_mkdir_chown_dirs
from core.host_config import DockerExecutionMode, MachinectlAuth, SubuidOutOfRangeError

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
def captured_invoke() -> Iterator[list[dict[str, Any]]]:
    """Patch ``core.dispatch.invoke`` with autospec — signature drift fails loudly.

    Call-shape mock only; the hardened ``docker run`` target argv is produced by
    ``core.dispatch``'s op-builder and covered by the dispatch fixture-parity
    suite; ownership is exercised in
    ``tests/integration/test_helper_container_userns.py``.
    """
    captured: list[dict[str, Any]] = []

    def _capture(
        op: Any,
        args: Any,
        host_config: Any,
        *,
        timeout: Any = None,
    ) -> subprocess.CompletedProcess[str]:
        captured.append(
            {"op": op, "args": args, "host_config": host_config, "timeout": timeout}
        )
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    with patch.object(dispatch, "invoke", autospec=True, side_effect=_capture):
        yield captured


# ─── helper_chown_files ────────────────────────────────────────────────────


class TestHelperChownFiles:
    def test_routes_through_dispatch_invoke_with_translated_ids(
        self, subid_fixture: None, captured_invoke: list[dict[str, Any]]
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
        assert len(captured_invoke) == 1
        call = captured_invoke[0]
        assert call["op"] is dispatch.Op.HELPER_CHOWN_FILES
        # <parent> <mode-octal> <translated-uid> <translated-gid> <file...>
        assert call["args"] == [
            "/inst/secrets",
            "0600",
            str(_IN_CONTAINER_UID),
            str(_IN_CONTAINER_GID),
            "ipc_host_key",
            "authorized_keys",
        ]
        # Translated, NOT host-absolute (the op validator expects translated ids).
        assert str(_HOST_UID) not in call["args"]
        hc = call["host_config"]
        assert hc.host.docker_unprivileged_user == _HOST_USER
        assert hc.host.machinectl_authentication == MachinectlAuth.SUDO

    def test_default_execution_mode_is_operator_rootless(
        self, subid_fixture: None, captured_invoke: list[dict[str, Any]]
    ) -> None:
        helper_chown_files(
            _HOST_USER,
            "/p",
            ["a"],
            owner_uid=_HOST_UID,
            owner_gid=_HOST_GID,
            mode=0o640,
            machinectl_auth=MachinectlAuth.SUDO,
        )
        hc = captured_invoke[0]["host_config"]
        assert hc.host.docker_execution_mode == DockerExecutionMode.OPERATOR_ROOTLESS

    def test_operator_rootless_execution_mode_propagates(
        self, subid_fixture: None, captured_invoke: list[dict[str, Any]]
    ) -> None:
        helper_chown_files(
            _HOST_USER,
            "/p",
            ["a"],
            owner_uid=_HOST_UID,
            owner_gid=_HOST_GID,
            mode=0o640,
            machinectl_auth=MachinectlAuth.SUDO,
            execution_mode=DockerExecutionMode.OPERATOR_ROOTLESS,
        )
        hc = captured_invoke[0]["host_config"]
        assert hc.host.docker_execution_mode == DockerExecutionMode.OPERATOR_ROOTLESS

    def test_empty_files_is_noop_skips_translation(
        self, captured_invoke: list[dict[str, Any]]
    ) -> None:
        # Empty input short-circuits before any subuid lookup — passing
        # out-of-range values should NOT raise (no dispatch happens either).
        helper_chown_files(
            _HOST_USER,
            "/p",
            [],
            owner_uid=99999999,
            owner_gid=99999999,
            mode=0o640,
            machinectl_auth=MachinectlAuth.SUDO,
        )
        assert captured_invoke == []

    def test_batched_single_invocation(
        self, subid_fixture: None, captured_invoke: list[dict[str, Any]]
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
        assert len(captured_invoke) == 1
        assert captured_invoke[0]["args"][4:] == ["a", "b", "c", "d"]

    def test_idempotent_re_invocation(
        self, subid_fixture: None, captured_invoke: list[dict[str, Any]]
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
        assert len(captured_invoke) == 3

    def test_custom_mode_octal(
        self, subid_fixture: None, captured_invoke: list[dict[str, Any]]
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
        # mode arg is the 4-digit octal string the op validator expects.
        assert captured_invoke[0]["args"][1] == "0755"

    def test_translation_host_absolute_to_in_container(
        self, subid_fixture: None, captured_invoke: list[dict[str, Any]]
    ) -> None:
        # Spec scenario: owner_uid=166535, owner_gid=166535 → "1000"/"1000".
        helper_chown_files(
            _HOST_USER,
            "/p",
            ["x"],
            owner_uid=_HOST_UID,
            owner_gid=_HOST_GID,
            mode=0o640,
            machinectl_auth=MachinectlAuth.SUDO,
        )
        args = captured_invoke[0]["args"]
        assert args[2] == str(_IN_CONTAINER_UID)
        assert args[3] == str(_IN_CONTAINER_GID)

    def test_out_of_range_owner_uid_raises_before_dispatch(
        self, subid_fixture: None
    ) -> None:
        with patch.object(dispatch, "invoke", autospec=True) as invoke_mock:
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
            invoke_mock.assert_not_called()

    def test_dispatch_failure_propagates(self, subid_fixture: None) -> None:
        """invoke() raising SandboxExecutionError ('timed out …') propagates.

        A failed helper chown must abort provisioning exactly as before the
        refactor — :func:`core.dispatch.invoke` raises on non-zero/timeout and
        the wrapper does not swallow it.
        """

        def _raise(
            op: Any, args: Any, host_config: Any, *, timeout: Any = None
        ) -> subprocess.CompletedProcess[str]:
            raise SandboxExecutionError("timed out after 30s")

        with (
            patch.object(dispatch, "invoke", autospec=True, side_effect=_raise),
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

    def test_default_timeout_passes_through(
        self, subid_fixture: None, captured_invoke: list[dict[str, Any]]
    ) -> None:
        helper_chown_files(
            _HOST_USER,
            "/p",
            ["x"],
            owner_uid=_HOST_UID,
            owner_gid=_HOST_GID,
            mode=0o640,
            machinectl_auth=MachinectlAuth.SUDO,
        )
        assert captured_invoke[0]["timeout"] == 30

    def test_custom_timeout_passes_through(
        self, subid_fixture: None, captured_invoke: list[dict[str, Any]]
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
        assert captured_invoke[0]["timeout"] == 5


# ─── helper_mkdir_chown_dirs ──────────────────────────────────────────────


class TestHelperMkdirChownDirs:
    def test_routes_through_dispatch_invoke_with_translated_ids(
        self, subid_fixture: None, captured_invoke: list[dict[str, Any]]
    ) -> None:
        helper_mkdir_chown_dirs(
            _HOST_USER,
            "/inst/cache/core",
            [".claude"],
            owner_uid=_HOST_UID,
            owner_gid=_HOST_GID,
            machinectl_auth=MachinectlAuth.SUDO,
        )
        assert len(captured_invoke) == 1
        call = captured_invoke[0]
        assert call["op"] is dispatch.Op.HELPER_MKDIR_CHOWN_DIRS
        # <parent> <translated-uid> <translated-gid> <leaf...> (no mode arg)
        assert call["args"] == [
            "/inst/cache/core",
            str(_IN_CONTAINER_UID),
            str(_IN_CONTAINER_GID),
            ".claude",
        ]
        assert str(_HOST_UID) not in call["args"]

    def test_empty_leaves_is_noop_skips_translation(
        self, captured_invoke: list[dict[str, Any]]
    ) -> None:
        helper_mkdir_chown_dirs(
            _HOST_USER,
            "/p",
            [],
            owner_uid=99999999,
            owner_gid=99999999,
            machinectl_auth=MachinectlAuth.SUDO,
        )
        assert captured_invoke == []

    def test_batched(
        self, subid_fixture: None, captured_invoke: list[dict[str, Any]]
    ) -> None:
        helper_mkdir_chown_dirs(
            _HOST_USER,
            "/p",
            [".claude", "tmux_resurrect"],
            owner_uid=_HOST_UID,
            owner_gid=_HOST_GID,
            machinectl_auth=MachinectlAuth.SUDO,
        )
        assert len(captured_invoke) == 1
        assert captured_invoke[0]["args"][3:] == [".claude", "tmux_resurrect"]

    def test_default_execution_mode_is_operator_rootless(
        self, subid_fixture: None, captured_invoke: list[dict[str, Any]]
    ) -> None:
        helper_mkdir_chown_dirs(
            _HOST_USER,
            "/p",
            ["x"],
            owner_uid=_HOST_UID,
            owner_gid=_HOST_GID,
            machinectl_auth=MachinectlAuth.SUDO,
        )
        hc = captured_invoke[0]["host_config"]
        assert hc.host.docker_execution_mode == DockerExecutionMode.OPERATOR_ROOTLESS

    def test_operator_rootless_execution_mode_propagates(
        self, subid_fixture: None, captured_invoke: list[dict[str, Any]]
    ) -> None:
        helper_mkdir_chown_dirs(
            _HOST_USER,
            "/p",
            ["x"],
            owner_uid=_HOST_UID,
            owner_gid=_HOST_GID,
            machinectl_auth=MachinectlAuth.SUDO,
            execution_mode=DockerExecutionMode.OPERATOR_ROOTLESS,
        )
        hc = captured_invoke[0]["host_config"]
        assert hc.host.docker_execution_mode == DockerExecutionMode.OPERATOR_ROOTLESS

    def test_translation_host_absolute_to_in_container(
        self, subid_fixture: None, captured_invoke: list[dict[str, Any]]
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
        args = captured_invoke[0]["args"]
        assert args[1] == str(_IN_CONTAINER_UID)
        assert args[2] == str(_IN_CONTAINER_GID)

    def test_dispatch_failure_propagates(self, subid_fixture: None) -> None:
        def _raise(
            op: Any, args: Any, host_config: Any, *, timeout: Any = None
        ) -> subprocess.CompletedProcess[str]:
            raise SandboxExecutionError("timed out after 30s")

        with (
            patch.object(dispatch, "invoke", autospec=True, side_effect=_raise),
            pytest.raises(SandboxExecutionError, match="timed out"),
        ):
            helper_mkdir_chown_dirs(
                _HOST_USER,
                "/p",
                ["x"],
                owner_uid=_HOST_UID,
                owner_gid=_HOST_GID,
                machinectl_auth=MachinectlAuth.SUDO,
            )

    def test_custom_timeout_passes_through(
        self, subid_fixture: None, captured_invoke: list[dict[str, Any]]
    ) -> None:
        helper_mkdir_chown_dirs(
            _HOST_USER,
            "/p",
            ["x"],
            owner_uid=_HOST_UID,
            owner_gid=_HOST_GID,
            machinectl_auth=MachinectlAuth.SUDO,
            timeout=5,
        )
        assert captured_invoke[0]["timeout"] == 5

    def test_out_of_range_owner_uid_raises_before_dispatch(
        self, subid_fixture: None
    ) -> None:
        with patch.object(dispatch, "invoke", autospec=True) as invoke_mock:
            with pytest.raises(SubuidOutOfRangeError):
                helper_mkdir_chown_dirs(
                    _HOST_USER,
                    "/p",
                    ["x"],
                    owner_uid=10,
                    owner_gid=_HOST_GID,
                    machinectl_auth=MachinectlAuth.SUDO,
                )
            invoke_mock.assert_not_called()


# ─── Executor timeout integration ─────────────────────────────────────────


class TestExecutorTimeout:
    """Executor.run accepts a timeout and converts TimeoutExpired to SandboxExecutionError.

    The helper primitives forward their ``timeout`` through
    ``core.dispatch.invoke`` to the sterile :class:`core.executor.Executor`,
    whose ``subprocess.TimeoutExpired`` → :class:`SandboxExecutionError`
    conversion is the load-bearing reason a hung helper aborts provisioning. The
    raise-on-failure contract is exercised end-to-end at the helper boundary in
    ``test_dispatch_failure_propagates`` above; this pins the underlying
    Executor conversion that produces the propagated error.
    """

    def test_timeout_converted(self) -> None:
        with patch("core.executor.subprocess.run") as run_mock:
            run_mock.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=1)
            with pytest.raises(SandboxExecutionError, match="timed out"):
                Executor().run(["/bin/true"], timeout=1)
