"""Workspace shared-group Action — one per chgrp/chmod/setfacl step.

Replaces the ``(operation_summary, target)`` tuples emitted by
``_workspace_shared_group_plan`` (cli.main). One Action per step
(chgrp / chmod 2770 / setfacl_effective / setfacl_default) so the
dry-run output remains line-for-line identical to the pre-refactor
format.

The ``op`` field carries the precomputed operation-summary string —
the per-step interpolation of ``bridge_gid`` / ``host_user`` /
``dev_user`` is performed at plan-construction time (see design
Decision 1's "Why precompute ``op``" rationale: ``ActionContext``
carries ``host_user`` but not ``dev_user``, so recomputing in
``.describe()`` would require carrying ``dev_user`` as a field —
precomputing ``op`` is simpler and matches the precomputed-``command``
choice on the ACL Actions).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from core.actions.base import Action
from core.exceptions import SandboxExecutionError

if TYPE_CHECKING:
    from pathlib import Path

    from core.actions.context import ActionContext


WorkspaceSharedGroupStep = Literal["chgrp", "chmod_2770", "setfacl_effective", "setfacl_default"]


@dataclass(frozen=True)
class WorkspaceSharedGroupAction(Action):
    """One step (chgrp / chmod / setfacl) of the workspace shared-group recipe.

    ``op`` carries the precomputed operation-summary string used by
    ``.describe()`` (preserving the byte-for-byte dry-run output);
    ``command`` carries the precomputed setfacl argv used by ``.execute()``
    for the ``setfacl_*`` steps. Storing both fields removes the implicit
    coupling between ``op``'s textual format and execute's argv derivation
    (the previous ``op.split(" ")`` parsing) — chgrp/chmod steps populate
    ``command`` with an empty tuple since they bypass subprocess entirely.
    """

    workspace_path: Path
    bridge_gid: int
    step: WorkspaceSharedGroupStep
    op: str
    command: tuple[str, ...]

    def describe(self) -> str:
        return f"    workspace: {self.op} {self.workspace_path}"

    def execute(self, ctx: ActionContext) -> None:
        ws = str(self.workspace_path)
        if self.step == "chgrp":
            try:
                import os

                os.chown(ws, -1, self.bridge_gid, follow_symlinks=False)
            except OSError as exc:
                raise SandboxExecutionError(f"Workspace chgrp failed for {ws}: {exc}") from exc
            return
        if self.step == "chmod_2770":
            try:
                import os

                os.chmod(ws, 0o2770)
            except OSError as exc:
                raise SandboxExecutionError(f"Workspace chmod failed for {ws}: {exc}") from exc
            return
        # setfacl_effective / setfacl_default — use the precomputed argv directly
        # (no parsing of ``op``, which is now a description-only field).
        try:
            subprocess.run(list(self.command), check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip() if exc.stderr else f"exit {exc.returncode}"
            label = "effective" if self.step == "setfacl_effective" else "default"
            raise SandboxExecutionError(
                f"Workspace shared-group {label} ACL failed for {ws}: {stderr}"
            ) from exc
