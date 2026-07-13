# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""Opt-in setup *extras* registry + sticky-opt-in inclusion predicate.

The base ceremony is the L0..L8 phases auto-discovered by
:func:`core.setup.phase_runner.discover_phases` (their module names match the
``^l\\d+a?_.+$`` pattern). The optional integration phases — fapolicyd trust
drop-in, AIDE config drop-in — are **deliberately NOT** placed where that
discovery would pick them up: they live in the ``core.setup.extras``
sub-package, whose module names (``fapolicyd``, ``aide``) do not match the
phase-module regex, so they are *opt-in only* and never part of the base
L0..L8 run.

Sticky opt-in (design D11). An extra is included in a given ``sandbox setup``
invocation iff EITHER

1. its ``--enable-<name>-integration`` flag was passed on this invocation, OR
2. its owned drop-in file already exists on disk (a prior run enabled it; the
   operator never has to re-pass the flag — the integration "sticks").

:func:`selected_extras` implements exactly that predicate. The filesystem
check is an *injected parameter* (``fs_check``), not module state, so tests
drive the sticky branch with a fake without monkeypatching ``os.path``
(anti-hack rule 5: a test seam is a parameter, never hidden module state).

Contract the extra modules satisfy
-----------------------------------

Each extra is a module ``core.setup.extras.<name>`` (``fapolicyd`` / ``aide``,
both shipped in-tree) that exports a single module-level

    PHASE: core.setup.phase_runner.Phase

with ``PHASE.depends_on == ("l8",)`` (extras run *after* the entire base
ceremony, never interleaved with it) and whose ``probe``/``act``/``reverify``
callbacks take the same :class:`~core.setup.phase_runner.SetupContext` every
base phase receives. :class:`ExtraSpec` references that ``PHASE`` *lazily*
(:meth:`ExtraSpec.load_phase` imports the module on demand) so this registry
imports cleanly even when an extra is not exercised, and a stale registry
entry whose module is absent raises a clear :class:`ModuleNotFoundError`
rather than failing silently.
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from core.setup.phase_runner import Phase


@dataclass(frozen=True)
class ExtraSpec:
    """Immutable descriptor for one opt-in integration extra.

    Attributes:
        name: The extra's stable key (also its ``core.setup.extras.<name>``
            module name and its ``Phase.id`` by convention) — ``"fapolicyd"``
            / ``"aide"``.
        flag: The CLI flag that explicitly enables the extra on an invocation
            (``"--enable-fapolicyd-integration"`` /
            ``"--enable-aide-integration"``).
        dropin_path: Absolute path of the drop-in file the extra owns. Its
            presence on disk is the *sticky* opt-in signal: once written by a
            prior run, the extra auto-includes itself on every subsequent
            ``sandbox setup`` without the flag (design D11). These paths are
            the spec's "Reserved Namespace File Ownership" entries for the
            optional integrations.
        module: The fully-qualified module from which :meth:`load_phase`
            imports the ``PHASE`` object.
    """

    name: str
    flag: str
    dropin_path: str
    module: str

    def load_phase(self) -> Phase:
        """Import the extra's module on demand and return its ``PHASE``.

        Lazy by design: the import is deferred to call time so a base-ceremony
        run that touches no extra never imports those modules, and a stale
        registry entry fails loudly (``ModuleNotFoundError`` /
        ``AttributeError``) rather than silently.
        """
        mod = importlib.import_module(self.module)
        return cast("Phase", mod.PHASE)


# The two opt-in integrations. Keyed by :attr:`ExtraSpec.name`. The drop-in
# paths are the spec's owned-namespace entries for the optional integrations
# ("Reserved Namespace File Ownership" requirement).
EXTRAS: Mapping[str, ExtraSpec] = {
    "fapolicyd": ExtraSpec(
        name="fapolicyd",
        flag="--enable-fapolicyd-integration",
        dropin_path="/etc/fapolicyd/trust.d/sandbox-ai.trust",
        module="core.setup.extras.fapolicyd",
    ),
    "aide": ExtraSpec(
        name="aide",
        flag="--enable-aide-integration",
        dropin_path="/etc/aide/aide.conf.d/sandbox-ai.conf",
        module="core.setup.extras.aide",
    ),
}


def selected_extras(
    enabled_flags: Mapping[str, bool],
    *,
    fs_check: Callable[[str], bool] = os.path.exists,
) -> list[str]:
    """Return the names of the extras active for this invocation (design D11).

    An extra is selected iff its ``--enable-<name>-integration`` flag is
    truthy in ``enabled_flags`` (mapping :attr:`ExtraSpec.name` → bool) OR its
    :attr:`ExtraSpec.dropin_path` already exists per ``fs_check`` (the sticky
    branch — a prior run wrote it, so the integration persists without the
    flag).

    ``fs_check`` is the injected filesystem seam: production passes the
    default :func:`os.path.exists`; tests pass a fake mapping path→bool so the
    sticky branch is exercised without any monkeypatching of ``os`` (anti-hack
    rule 5). Determinism: iterates :data:`EXTRAS` in registry order, so the
    returned list ordering is stable.
    """
    out: list[str] = []
    for spec in EXTRAS.values():
        flagged = bool(enabled_flags.get(spec.name, False))
        sticky = fs_check(spec.dropin_path)
        if flagged or sticky:
            out.append(spec.name)
    return out


__all__ = ["EXTRAS", "ExtraSpec", "selected_extras"]
