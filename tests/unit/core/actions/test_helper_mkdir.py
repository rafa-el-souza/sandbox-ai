# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``HelperMkdirChownAction``."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from core.actions.context import ActionContext
from core.actions.helper_mkdir import HelperMkdirChownAction
from core.executor import Executor
from core.host_config import DockerExecutionMode

if TYPE_CHECKING:
    import pytest


def _ctx(
    mode: DockerExecutionMode = DockerExecutionMode.SEPARATE_USER,
) -> ActionContext:
    return ActionContext(
        host_user="claude-sandbox",
        executor=Executor(),
        instance_dir=Path("/inst"),
        docker_execution_mode=mode,
    )


def test_describe_renders_brace_expansion_format() -> None:
    action = HelperMkdirChownAction(
        parent=Path("/inst/cache/core"),
        leaves=(".claude",),
        owner_uid=166535,
        owner_gid=166535,
    )
    assert (
        action.describe()
        == "    helper-mkdir+chown /inst/cache/core/{.claude} → 166535:166535"
    )


def test_describe_handles_multiple_leaves() -> None:
    action = HelperMkdirChownAction(
        parent=Path("/inst/log"),
        leaves=("core", "admin"),
        owner_uid=166535,
        owner_gid=166535,
    )
    assert (
        action.describe()
        == "    helper-mkdir+chown /inst/log/{core, admin} → 166535:166535"
    )


def test_execute_delegates_to_helper_mkdir_chown_dirs(monkeypatch: pytest.MonkeyPatch) -> None:
    from collections.abc import Iterable

    invocations: list[tuple[object, ...]] = []

    def _fake(
        host_user: str,
        parent: str,
        leaves: Iterable[str],
        uid: int,
        gid: int,
        execution_mode: object = DockerExecutionMode.SEPARATE_USER,
    ) -> None:
        invocations.append((host_user, parent, tuple(leaves), uid, gid, execution_mode))

    # Replace via module-string per project convention so static analysis is happy.
    monkeypatch.setattr("core.actions.helper_mkdir.helper_mkdir_chown_dirs", _fake)
    action = HelperMkdirChownAction(
        parent=Path("/inst/log"),
        leaves=("core", "admin"),
        owner_uid=166535,
        owner_gid=166535,
    )
    action.execute(_ctx())
    assert invocations == [
        (
            "claude-sandbox",
            "/inst/log",
            ("core", "admin"),
            166535,
            166535,
            DockerExecutionMode.SEPARATE_USER,
        )
    ]


def test_execute_forwards_operator_rootless_execution_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from collections.abc import Iterable

    captured: dict[str, object] = {}

    def _fake(
        host_user: str,
        parent: str,
        leaves: Iterable[str],
        uid: int,
        gid: int,
        execution_mode: object = DockerExecutionMode.SEPARATE_USER,
    ) -> None:
        captured["execution_mode"] = execution_mode

    monkeypatch.setattr("core.actions.helper_mkdir.helper_mkdir_chown_dirs", _fake)
    action = HelperMkdirChownAction(
        parent=Path("/inst/log"),
        leaves=("core",),
        owner_uid=166535,
        owner_gid=166535,
    )
    action.execute(_ctx(mode=DockerExecutionMode.OPERATOR_ROOTLESS))
    assert captured["execution_mode"] == DockerExecutionMode.OPERATOR_ROOTLESS
