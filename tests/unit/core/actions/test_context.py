"""Tests for ``ActionContext`` — frozen dataclass bundling per-phase plumbing."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from core.actions.context import ActionContext
from core.executor import Executor
from core.host_config import DockerExecutionMode, MachinectlAuth


def _make_ctx(auth: MachinectlAuth = MachinectlAuth.SUDO) -> ActionContext:
    return ActionContext(
        host_user="claude-sandbox",
        auth=auth,
        executor=Executor(),
        instance_dir=Path("/inst"),
    )


def test_context_carries_all_four_fields() -> None:
    ctx = _make_ctx()
    assert ctx.host_user == "claude-sandbox"
    assert ctx.auth == MachinectlAuth.SUDO
    assert isinstance(ctx.executor, Executor)
    assert ctx.instance_dir == Path("/inst")


def test_context_is_frozen() -> None:
    ctx = _make_ctx()
    # Use setattr through a name held as a variable so the lint check for
    # "setattr with constant attribute" doesn't trigger; the dataclass
    # FrozenInstanceError is what we actually want to assert at runtime.
    field_name = "host_user"
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(ctx, field_name, "other")


def test_docker_execution_mode_defaults_to_operator_rootless() -> None:
    ctx = _make_ctx()
    assert ctx.docker_execution_mode == DockerExecutionMode.OPERATOR_ROOTLESS


def test_docker_execution_mode_round_trips() -> None:
    ctx = ActionContext(
        host_user="claude-sandbox",
        auth=MachinectlAuth.SUDO,
        executor=Executor(),
        instance_dir=Path("/inst"),
        docker_execution_mode=DockerExecutionMode.OPERATOR_ROOTLESS,
    )
    assert ctx.docker_execution_mode == DockerExecutionMode.OPERATOR_ROOTLESS
