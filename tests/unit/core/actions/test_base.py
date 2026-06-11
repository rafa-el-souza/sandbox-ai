"""Tests for the ``Action`` ABC contract.

Asserts the abstract methods are enforced (``Action`` cannot be
instantiated; subclasses missing either method also cannot be
instantiated).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from core.actions.base import Action
from core.actions.context import ActionContext
from core.executor import Executor
from core.host_config import MachinectlAuth, minimal_host_config


def _instantiate(cls: type) -> object:
    """Instantiate via a ``type`` indirection so static type checkers stay
    out of the abstract-instantiation check that Python enforces at runtime.
    """
    return cls()


def test_action_is_abstract_and_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        _instantiate(Action)


def test_subclass_missing_describe_cannot_be_instantiated() -> None:
    class _MissingDescribe(Action):
        def execute(self, ctx: ActionContext) -> None:
            return None

    with pytest.raises(TypeError):
        _instantiate(_MissingDescribe)


def test_subclass_missing_execute_cannot_be_instantiated() -> None:
    class _MissingExecute(Action):
        def describe(self) -> str:
            return ""

    with pytest.raises(TypeError):
        _instantiate(_MissingExecute)


def test_complete_subclass_can_be_instantiated() -> None:
    class _Complete(Action):
        def describe(self) -> str:
            return "ok"

        def execute(self, ctx: ActionContext) -> None:
            return None

    inst = _Complete()
    assert inst.describe() == "ok"
    ctx = ActionContext(
        host_user="claude-sandbox",
        auth=MachinectlAuth.SUDO,
        executor=Executor(),
        instance_dir=Path("/inst"),
    )
    inst.execute(ctx)  # returns None per Action protocol


def test_render_command_base_default_delegates_to_describe_ignoring_host_config() -> None:
    """The base ``render_command`` default returns ``describe()`` and does
    not vary with ``host_config`` — the uniform-contract guarantee the
    dry-run loop relies on for non-overriding subclasses."""

    class _Complete(Action):
        def describe(self) -> str:
            return "identity-line"

        def execute(self, ctx: ActionContext) -> None:
            return None

    inst = _Complete()
    hc_alice = minimal_host_config("alice", MachinectlAuth.SUDO)
    hc_bob = minimal_host_config("bob", MachinectlAuth.SUDO)
    assert inst.render_command(hc_alice) == "identity-line"
    assert inst.render_command(hc_bob) == "identity-line"
    assert inst.render_command(hc_alice) == inst.describe()
