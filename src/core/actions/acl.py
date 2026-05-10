"""ACL grant/revoke Actions — wraps a precomputed ``setfacl`` argv.

Both Actions are frozen dataclasses carrying the precomputed ``command``
as a ``tuple[str, ...]`` (immutable; the producer at the ``_acl_*_plan``
site casts ``list[str]`` argv via ``tuple(argv)`` at append time per
design Decision 1).

``.describe()`` renders the tuple-era ``"$ <argv>  # <description>"``
preview line; ``.execute()`` runs the command via ``subprocess.run``
mirroring today's grant/revoke phase semantics:

- Grant: ``check=True`` — failure raises
  :class:`core.exceptions.SandboxExecutionError` with the original
  description as context.
- Revoke: ``check=False`` — failure is captured by the caller
  (``_revoke_acls``) into a warnings list; this Action's ``.execute()``
  raises only on ``OSError`` (binary missing / fork failure).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.actions.base import Action
from core.exceptions import SandboxExecutionError

if TYPE_CHECKING:
    from pathlib import Path

    from core.actions.context import ActionContext


@dataclass(frozen=True)
class NamedAclGrantAction(Action):
    """Single ``setfacl -m`` (or ``-d -m``) entry from ``_acl_grant_plan``.

    Per design Decision 1's table the Action carries the structured
    ``target``/``entry``/``default``/``recursive`` fields in addition to
    the precomputed ``command`` argv. ``.describe()`` and ``.execute()``
    consume ``description``/``command`` today; the structured fields are
    stored to enable Refactor C dispatch (Strategy classes) without
    re-parsing the argv.
    """

    command: tuple[str, ...]
    description: str
    target: Path
    entry: str
    default: bool
    recursive: bool

    def describe(self) -> str:
        return f"    $ {' '.join(self.command)}  # {self.description}"

    def execute(self, ctx: ActionContext) -> None:
        try:
            subprocess.run(list(self.command), check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip() if exc.stderr else f"exit {exc.returncode}"
            raise SandboxExecutionError(f"ACL grant failed for {self.description}: {stderr}") from exc


@dataclass(frozen=True)
class NamedAclRevokeAction(Action):
    """Single ``setfacl -x`` (or ``-d -x``) entry from ``_acl_revoke_plan``.

    Per design Decision 1's table the Action carries the structured
    ``target``/``entry``/``default`` fields in addition to the precomputed
    ``command`` argv. The asymmetry with grant — no ``recursive`` — matches
    the design table: the revoke plan is a strict subset of revertible
    operations and never emits ``setfacl -R -x``.
    """

    command: tuple[str, ...]
    description: str
    target: Path
    entry: str
    default: bool

    def describe(self) -> str:
        return f"    $ {' '.join(self.command)}  # {self.description}"

    def execute(self, ctx: ActionContext) -> None:
        """Run revoke with ``check=False``; caller collects warnings.

        Raises :class:`core.exceptions.SandboxExecutionError` only when the
        underlying ``subprocess.run`` itself fails (binary missing, etc).
        Non-zero exit codes are surfaced via the returned
        :class:`subprocess.CompletedProcess` for the caller to inspect — the
        revoke phase wraps each ``execute()`` and converts non-zero exits
        into warning strings.
        """
        try:
            result = subprocess.run(list(self.command), check=False, capture_output=True, text=True)
        except OSError as exc:
            raise SandboxExecutionError(f"ACL revoke OS error for {self.description}: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.strip() if result.stderr else f"exit {result.returncode}"
            raise SandboxExecutionError(f"ACL revoke warning for {self.description}: {detail}")
