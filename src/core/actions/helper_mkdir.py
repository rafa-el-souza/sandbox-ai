"""Helper-mkdir+chown Action — wraps a (parent, leaves, uid, gid) tuple.

Replaces the ``(parent_abs, leaves, owner_uid, owner_gid)`` tuple
emitted by ``_helper_mkdir_chown_plan`` (cli.main). On ``.execute()``
delegates to :func:`core.helper_container.helper_mkdir_chown_dirs` —
the helper-container primitive that mkdirs and chowns each leaf to
the host-absolute uid/gid carried by this Action (per the
``helper-container`` capability's host-absolute uid/gid contract).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.actions.base import Action
from core.helper_container import helper_mkdir_chown_dirs

if TYPE_CHECKING:
    from pathlib import Path

    from core.actions.context import ActionContext


@dataclass(frozen=True)
class HelperMkdirChownAction(Action):
    """One per (parent, leaves) group from ``_helper_mkdir_chown_plan``."""

    parent: Path
    leaves: tuple[str, ...]
    owner_uid: int
    owner_gid: int

    def describe(self) -> str:
        leaves_str = ", ".join(self.leaves)
        return f"    helper-mkdir+chown {self.parent}/{{{leaves_str}}} → {self.owner_uid}:{self.owner_gid}"

    def execute(self, ctx: ActionContext) -> None:
        helper_mkdir_chown_dirs(
            ctx.host_user,
            str(self.parent),
            self.leaves,
            self.owner_uid,
            self.owner_gid,
            ctx.auth,
        )
