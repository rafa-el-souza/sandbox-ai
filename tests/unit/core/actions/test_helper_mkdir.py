"""Tests for ``HelperMkdirChownAction``."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from core.actions.context import ActionContext
from core.actions.helper_mkdir import HelperMkdirChownAction
from core.executor import Executor
from core.host_config import MachinectlAuth

if TYPE_CHECKING:
    import pytest


def _ctx(auth: MachinectlAuth = MachinectlAuth.SUDO) -> ActionContext:
    return ActionContext(
        host_user="claude-sandbox",
        auth=auth,
        executor=Executor(),
        instance_dir=Path("/inst"),
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

    def _fake(host_user: str, parent: str, leaves: Iterable[str], uid: int, gid: int, auth: object) -> None:
        invocations.append((host_user, parent, tuple(leaves), uid, gid, auth))

    # Replace via module-string per project convention so static analysis is happy.
    monkeypatch.setattr("core.actions.helper_mkdir.helper_mkdir_chown_dirs", _fake)
    action = HelperMkdirChownAction(
        parent=Path("/inst/log"),
        leaves=("core", "admin"),
        owner_uid=166535,
        owner_gid=166535,
    )
    action.execute(_ctx(MachinectlAuth.POLKIT))
    assert invocations == [
        ("claude-sandbox", "/inst/log", ("core", "admin"), 166535, 166535, MachinectlAuth.POLKIT)
    ]
