# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Helper-cp+chown Action — wraps a (parent, files, uid, gid, mode) tuple.

Replaces the ``(parent_abs, files, owner_uid, owner_gid, mode)`` tuple
emitted by ``_helper_cp_chown_plan`` (cli.main). On ``.execute()``
delegates to :func:`core.helper_container.helper_chown_files` — the
helper-container primitive that copies, chmods, and chowns each file
to the host-absolute uid/gid + mode carried by this Action (per the
``helper-container`` capability's host-absolute uid/gid contract).

Numeric fields (``owner_uid``, ``owner_gid``, ``mode``) stay numeric
so tests like ``test_plan_uid_and_gid_both_in_subid_range`` can read
them via attribute access — the ``mode`` is rendered as octal in
``.describe()`` for parity with the pre-refactor dry-run line format.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.actions.base import Action
from core.helper_container import helper_chown_files

if TYPE_CHECKING:
    from pathlib import Path

    from core.actions.context import ActionContext


@dataclass(frozen=True)
class HelperCpChownAction(Action):
    """One per (parent, files) group from ``_helper_cp_chown_plan``."""

    parent: Path
    files: tuple[str, ...]
    owner_uid: int
    owner_gid: int
    mode: int

    def describe(self) -> str:
        files_str = ", ".join(self.files)
        return (
            f"    helper-cp+chown {self.parent}/{{{files_str}}} "
            f"→ {self.owner_uid}:{self.owner_gid} {self.mode:o}"
        )

    def execute(self, ctx: ActionContext) -> None:
        helper_chown_files(
            ctx.host_user,
            str(self.parent),
            self.files,
            self.owner_uid,
            self.owner_gid,
            self.mode,
            ctx.auth,
            execution_mode=ctx.docker_execution_mode,
        )
