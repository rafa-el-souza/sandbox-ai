"""Typed Action hierarchy — Command-pattern carriers for plan items.

Each ``_*_plan`` function in :mod:`cli.main` returns a list of typed
:class:`Action` objects (subclasses of :class:`core.actions.base.Action`).
The dry-run preview calls :meth:`Action.describe` on each item; the live
phase calls :meth:`Action.execute` on the same items. The ``Action`` is
the single carrier of both semantics — there is no parallel
reconstruction of the underlying argv anywhere in the codebase.

Public surface re-exported here for convenience; concrete classes live
one-per-module under ``core.actions.*``.
"""

from __future__ import annotations

from core.actions.acl import NamedAclGrantAction, NamedAclRevokeAction
from core.actions.base import Action
from core.actions.compose import ComposeUpAction
from core.actions.context import ActionContext
from core.actions.helper_cp import HelperCpChownAction
from core.actions.helper_mkdir import HelperMkdirChownAction
from core.actions.workspace import WorkspaceSharedGroupAction, WorkspaceSharedGroupStep

__all__ = [
    "Action",
    "ActionContext",
    "ComposeUpAction",
    "HelperCpChownAction",
    "HelperMkdirChownAction",
    "NamedAclGrantAction",
    "NamedAclRevokeAction",
    "WorkspaceSharedGroupAction",
    "WorkspaceSharedGroupStep",
]
