"""Tests for ``ComposeUpAction`` (single-field; same string read by describe + execute)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

from core.actions.compose import ComposeUpAction
from core.actions.context import ActionContext
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


def test_describe_returns_inner_command_verbatim() -> None:
    inner = (
        "TERM=dumb NO_COLOR=1 BUILDKIT_PROGRESS=plain "
        "COMPOSE_PROJECT_NAME=myproj docker compose -f /inst/docker/compose.yml "
        "--ansi never --env-file /inst/.sandbox.env up -d --build --wait"
    )
    action = ComposeUpAction(inner_command=inner)
    assert action.describe() == inner


class _FakeExecutor:
    def __init__(self) -> None:
        self.invocations: list[tuple[list[str], dict[str, object]]] = []

    def run(self, cmd: list[str], **kw: object) -> object:
        self.invocations.append((cmd, dict(kw)))
        return None


def test_execute_wraps_inner_with_machinectl_sudo_prefix() -> None:
    fake = _FakeExecutor()
    ctx = ActionContext(
        host_user="claude-sandbox",
        auth=MachinectlAuth.SUDO,
        executor=cast("Executor", fake),
        instance_dir=Path("/inst"),
    )
    inner = "TERM=dumb docker compose up"
    ComposeUpAction(inner_command=inner).execute(ctx)
    assert len(fake.invocations) == 1
    cmd, kw = fake.invocations[0]
    assert cmd == [
        "sudo",
        "machinectl",
        "shell",
        "claude-sandbox@.host",
        "/bin/bash",
        "-c",
        inner,
    ]
    assert kw == {"sentinel": True}


def test_execute_wraps_inner_with_machinectl_polkit_prefix() -> None:
    fake = _FakeExecutor()
    ctx = ActionContext(
        host_user="claude-sandbox",
        auth=MachinectlAuth.POLKIT,
        executor=cast("Executor", fake),
        instance_dir=Path("/inst"),
    )
    inner = "docker compose up"
    ComposeUpAction(inner_command=inner).execute(ctx)
    cmd, _ = fake.invocations[0]
    assert cmd == ["machinectl", "shell", "claude-sandbox@.host", "/bin/bash", "-c", inner]


def test_describe_and_execute_share_the_same_inner_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """Structural byte-equivalence: both code paths read the same field."""
    inner = "docker compose up -d --build --wait"
    action = ComposeUpAction(inner_command=inner)

    fake = _FakeExecutor()
    ctx = ActionContext(
        host_user="claude-sandbox",
        auth=MachinectlAuth.SUDO,
        executor=cast("Executor", fake),
        instance_dir=Path("/inst"),
    )
    action.execute(ctx)
    cmd, _ = fake.invocations[0]
    # The bash payload is the very same string returned by describe()
    assert cmd[-1] == action.describe()
