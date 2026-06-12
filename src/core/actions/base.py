# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Action ABC — typed Command-pattern carrier for plan items.

The ``Action`` hierarchy formalizes the Command pattern that the project's
anti-hack rule 7 mandates ("Plan and execute share data — Dry-run/preview
and real-run derive from one generator"). Each concrete subclass replaces
a previously-untyped tuple emitted by an ``_*_plan`` function in
``cli.main``.

Three methods make up the contract:

- :meth:`describe` — pure, takes no context, returns the identity line for
  the dry-run preview. Safe to call without privileges or a live executor.
- :meth:`render_command` — the dry-run preview line, given the resolved
  :class:`~core.host_config.HostConfig`. The base default delegates to
  :meth:`describe` (ignoring ``host_config``); subclasses whose dry-run line
  is HostConfig-dependent (e.g. the compose-up wire form) override it so the
  hierarchy stays uniform and the dry-run loop never special-cases a type.
- :meth:`execute` — runs the live operation, given an :class:`ActionContext`
  bundling per-invocation plumbing (host user, machinectl auth mode,
  executor, instance directory).

These methods share the underlying state stored on the dataclass, so
the dry-run preview and the live phase are guaranteed to operate on the
same data — no parallel reconstruction is permitted.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.actions.context import ActionContext
    from core.host_config import HostConfig


class Action(ABC):
    """Abstract base for every plan item.

    Concrete subclasses live under ``core.actions.*`` and are constructed
    by the ``_*_plan`` functions in ``cli.main``.
    """

    @abstractmethod
    def describe(self) -> str:
        """Return the identity line for the dry-run preview."""

    def render_command(self, host_config: HostConfig) -> str:
        """Return the dry-run preview line for this action.

        The default ignores ``host_config`` and delegates to
        :meth:`describe`; subclasses whose dry-run line is
        HostConfig-dependent override this.
        """
        return self.describe()

    @abstractmethod
    def execute(self, ctx: ActionContext) -> None:
        """Run the live operation against ``ctx``."""
