"""ComposeUpAction — wraps the inner ``bash -c`` string for ``docker compose up``.

Single field (``inner_command: str``) per design Decision 1: the four
input parameters of ``_compose_up_cmd_plan`` (``instance_dir``,
``project_name``, ``compose_files``, ``env_file``) are NOT carried on
the Action — they are *inputs* to plan construction, not plan output.
The ``machinectl`` argv prefix is built at ``.execute()`` time from
``ActionContext.host_user`` + ``ActionContext.auth`` so the live and
dry-run paths derive their command from the same single carrier (per
``cli-start``'s "Live and dry-run derive compose up from a shared plan
helper" requirement).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.actions.base import Action
from core.host_config import machinectl_cmd

if TYPE_CHECKING:
    from core.actions.context import ActionContext


@dataclass(frozen=True)
class ComposeUpAction(Action):
    """``docker compose up`` invocation — single source of truth for live + dry-run."""

    inner_command: str

    def describe(self) -> str:
        # The dry-run line includes the resolved machinectl prefix; that resolution
        # is done by the caller (which has the ActionContext) so .describe() stays
        # pure. The caller renders ``$ <prefix> /bin/bash -c '<inner>'`` from
        # this string + the prefix it already computed.
        return self.inner_command

    def execute(self, ctx: ActionContext) -> None:
        ctx.executor.run(
            [
                *machinectl_cmd(ctx.host_user, ctx.auth),
                "/bin/bash",
                "-c",
                self.inner_command,
            ],
            sentinel=True,
        )
