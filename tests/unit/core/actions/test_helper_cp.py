# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``HelperCpChownAction`` covering all three source classes."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from core.actions.context import ActionContext
from core.actions.helper_cp import HelperCpChownAction
from core.executor import Executor
from core.host_config import DockerExecutionMode, MachinectlAuth

if TYPE_CHECKING:
    import pytest


def _ctx(
    auth: MachinectlAuth = MachinectlAuth.SUDO,
    mode: DockerExecutionMode = DockerExecutionMode.SEPARATE_USER,
) -> ActionContext:
    return ActionContext(
        host_user="claude-sandbox",
        auth=auth,
        executor=Executor(),
        instance_dir=Path("/inst"),
        docker_execution_mode=mode,
    )


class TestDescribeRendersAllModes:
    def test_describe_ro_files_mode_0640(self) -> None:
        action = HelperCpChownAction(
            parent=Path("/inst/config/proxy"),
            files=("squid.conf",),
            owner_uid=165549,
            owner_gid=165549,
            mode=0o640,
        )
        assert (
            action.describe()
            == "    helper-cp+chown /inst/config/proxy/{squid.conf} → 165549:165549 640"
        )

    def test_describe_exec_files_mode_0500(self) -> None:
        action = HelperCpChownAction(
            parent=Path("/inst/docker/core"),
            files=("entrypoint.sh",),
            owner_uid=166535,
            owner_gid=166535,
            mode=0o500,
        )
        assert (
            action.describe()
            == "    helper-cp+chown /inst/docker/core/{entrypoint.sh} → 166535:166535 500"
        )

    def test_describe_rw_files_mode_0660(self) -> None:
        action = HelperCpChownAction(
            parent=Path("/inst/config/core"),
            files=(".claude.json",),
            owner_uid=166535,
            owner_gid=166535,
            mode=0o660,
        )
        assert (
            action.describe()
            == "    helper-cp+chown /inst/config/core/{.claude.json} → 166535:166535 660"
        )

    def test_describe_secrets_mode_0600(self) -> None:
        action = HelperCpChownAction(
            parent=Path("/inst/secrets"),
            files=("authorized_keys", "ipc_host_key"),
            owner_uid=166535,
            owner_gid=166535,
            mode=0o600,
        )
        assert (
            action.describe()
            == "    helper-cp+chown /inst/secrets/{authorized_keys, ipc_host_key} → 166535:166535 600"
        )


def test_execute_delegates_to_helper_chown_files(monkeypatch: pytest.MonkeyPatch) -> None:
    invocations: list[tuple[object, ...]] = []

    def _fake(
        host_user: str,
        parent: str,
        files: Iterable[str],
        uid: int,
        gid: int,
        mode: int,
        auth: object,
        execution_mode: object = DockerExecutionMode.SEPARATE_USER,
    ) -> None:
        invocations.append((host_user, parent, tuple(files), uid, gid, mode, auth, execution_mode))

    monkeypatch.setattr("core.actions.helper_cp.helper_chown_files", _fake)
    action = HelperCpChownAction(
        parent=Path("/inst/secrets"),
        files=("ipc_host_key",),
        owner_uid=166535,
        owner_gid=166535,
        mode=0o600,
    )
    action.execute(_ctx(MachinectlAuth.SUDO))
    assert invocations == [
        (
            "claude-sandbox",
            "/inst/secrets",
            ("ipc_host_key",),
            166535,
            166535,
            0o600,
            MachinectlAuth.SUDO,
            DockerExecutionMode.SEPARATE_USER,
        )
    ]


def test_execute_forwards_operator_rootless_execution_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _fake(
        host_user: str,
        parent: str,
        files: Iterable[str],
        uid: int,
        gid: int,
        mode: int,
        auth: object,
        execution_mode: object = DockerExecutionMode.SEPARATE_USER,
    ) -> None:
        captured["execution_mode"] = execution_mode

    monkeypatch.setattr("core.actions.helper_cp.helper_chown_files", _fake)
    action = HelperCpChownAction(
        parent=Path("/inst/secrets"),
        files=("ipc_host_key",),
        owner_uid=166535,
        owner_gid=166535,
        mode=0o600,
    )
    action.execute(_ctx(mode=DockerExecutionMode.OPERATOR_ROOTLESS))
    assert captured["execution_mode"] == DockerExecutionMode.OPERATOR_ROOTLESS


def test_typed_field_access_for_uid_gid_mode() -> None:
    """Carve-out: numeric/permission-bit assertions read typed fields, not .describe()."""
    action = HelperCpChownAction(
        parent=Path("/inst/secrets"),
        files=("ipc_host_key",),
        owner_uid=166535,
        owner_gid=166540,
        mode=0o600,
    )
    assert action.owner_uid == 166535
    assert action.owner_gid == 166540
    assert action.mode == 0o600
