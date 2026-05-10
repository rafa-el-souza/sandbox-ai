"""Action ABC — typed Command-pattern carrier for plan items.

The ``Action`` hierarchy formalizes the Command pattern that the project's
anti-hack rule 7 mandates ("Plan and execute share data — Dry-run/preview
and real-run derive from one generator"). Each concrete subclass replaces
a previously-untyped tuple emitted by an ``_*_plan`` function in
``cli.main``.

Two methods make up the contract:

- :meth:`describe` — pure, takes no context, returns the line printed by
  the dry-run preview. Safe to call without privileges or a live executor.
- :meth:`execute` — runs the live operation, given an :class:`ActionContext`
  bundling per-invocation plumbing (host user, machinectl auth mode,
  executor, instance directory).

The two methods share the underlying state stored on the dataclass, so
the dry-run preview and the live phase are guaranteed to operate on the
same data — no parallel reconstruction is permitted.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.actions.context import ActionContext


class Action(ABC):
    """Abstract base for every plan item.

    Concrete subclasses live under ``core.actions.*`` and are constructed
    by the ``_*_plan`` functions in ``cli.main``.
    """

    @abstractmethod
    def describe(self) -> str:
        """Return the line rendered by the dry-run preview."""

    @abstractmethod
    def execute(self, ctx: ActionContext) -> None:
        """Run the live operation against ``ctx``."""
