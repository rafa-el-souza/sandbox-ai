# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""ComposeUpAction — the typed ``compose-up`` instance intent.

The Action carries only the typed instance name (design Q6 / D2): the
``--project`` / ``--env-file`` / ``--compose-file`` operands are NOT plan
output — they are operator-side dev-context state that
``core.dispatch.build_invocation`` resolves internally from the instance
name (the single compose-state resolver ``_resolve_compose_state``). The
live ``.execute()`` path routes through ``core.dispatch.invoke``; the
dry-run ``.render_command(host_config)`` path routes through
``core.dispatch.build_invocation`` — the same seam ``invoke()`` consumes,
so the live and dry-run commands cannot drift (anti-hack rules 4 + 7 — one
seam). ``.describe()`` is a pure identity on the typed instance name (it
cannot resolve the HostConfig-dependent command); the caller uses
``.render_command(host_config)`` for the dry-run line.

``host_config`` is read from :class:`~core.actions.context.ActionContext`
(the optional field only the compose-up construction site supplies).
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.actions.base import Action
from core.dispatch import Op, build_invocation, invoke

if TYPE_CHECKING:
    from core.actions.context import ActionContext
    from core.host_config import HostConfig


@dataclass(frozen=True)
class ComposeUpAction(Action):
    """``compose-up`` invocation — single source of truth for live + dry-run.

    Carries the typed instance name; the boundary-crossing command (Q6 wire
    form) is built by ``core.dispatch.build_invocation`` so the live and
    dry-run paths cannot diverge.
    """

    instance_name: str

    def describe(self) -> str:
        # The full dry-run line needs the resolved HostConfig (operator-side
        # compose-state resolution); that resolution is the caller's (it holds
        # the config). .describe() stays a pure identity on the typed intent;
        # the caller renders the command via .render_command(host_config),
        # which goes through the SAME build_invocation seam invoke() uses.
        return self.instance_name

    def render_command(self, host_config: HostConfig) -> str:
        """Render the would-be boundary-crossing command (NOT executed).

        Derives from ``core.dispatch.build_invocation`` — the SAME seam
        :func:`core.dispatch.invoke` (and thus :meth:`execute`) consumes — so
        the dry-run line is byte-identical to the live invocation.
        """
        return shlex.join(build_invocation(Op.COMPOSE_UP, [self.instance_name], host_config))

    def execute(self, ctx: ActionContext) -> None:
        if ctx.host_config is None:
            raise ValueError(
                "ComposeUpAction requires ActionContext.host_config; the "
                "compose-up construction site must supply the resolved HostConfig"
            )
        # invoke() raises SandboxExecutionError on a non-zero exit — the same
        # raise-on-failure / abort behavior the previous sentinel=True path had.
        invoke(Op.COMPOSE_UP, [self.instance_name], ctx.host_config)
