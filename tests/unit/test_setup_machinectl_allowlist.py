# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""Verify the setup-phase package is correctly permitted by the pre-existing
``machinectl_cmd`` allowlist — WITHOUT touching the allowlist definition.

This is the Group-10 verification artifact for ``sandbox-setup`` (C-002). The
three-category allowlist (``src/core/host_config.py``, ``src/core/dispatch.py``,
and the bounded ``src/core/setup/*.py`` glob) is shipped and owned by
``runtime-dispatcher`` (C-001) in :mod:`tests.unit.test_conventions`; C-002 must
NOT modify it (phase-3 review B-4). These tests instead *import* C-001's checker
seam and assert two complementary facts against the post-setup codebase:

1. **Positive** — every ``src/core/setup/*.py`` module that references
   ``machinectl_cmd`` is permitted by the pre-existing glob category, i.e. the
   live restriction gate is green now that the setup package exists. (We also
   assert at least one setup module actually references the symbol, so this is
   a real positive, not a vacuous pass.)
2. **Negative** — a SYNTHETIC caller written OUTSIDE the three categories that
   imports ``machinectl_cmd`` STILL fails the restriction. This proves the
   allowlist did not silently widen to ``src/**`` to accommodate the setup
   package: it discriminates by exact path membership, so an out-of-category
   caller is still caught even with the setup glob populated.

The detector logic is the SAME :func:`_machinectl_cmd_violations` seam the live
gate uses (imported, not re-implemented — single source of truth, anti-hack
rule 5). Importing helper functions from a peer *test module* (not a
``conftest.py``) is explicitly allowed by the conventions gate; the
``test_no_runtime_imports_from_conftest`` rule only forbids conftest imports.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.unit.test_conventions import (
    _MACHINECTL_CMD_LITERAL_ALLOWLIST,
    _SRC_ROOT,
    _machinectl_cmd_allowlist,
    _machinectl_cmd_references,
    _machinectl_cmd_violations,
    _python_files,
)

_SETUP_PKG_DIR = _SRC_ROOT / "core" / "setup"


def _setup_modules() -> list[Path]:
    """Every ``src/core/setup/*.py`` (the bounded glob the allowlist permits) —
    non-recursive, matching the allowlist's own ``glob("*.py")`` exactly."""
    return sorted(_SETUP_PKG_DIR.glob("*.py"))


def test_setup_modules_exist() -> None:
    """Sanity gate: the setup package is populated (post Groups 5-9), so the
    positive assertion below is exercising real modules, not an empty glob."""
    modules = _setup_modules()
    assert modules, (
        "src/core/setup/*.py is empty — the positive allowlist assertion would "
        "be vacuous. Group 10 must run AFTER the setup package exists."
    )


def test_at_least_one_setup_module_references_machinectl_cmd() -> None:
    """The positive case is non-trivial: at least one ``src/core/setup/*.py``
    module genuinely imports/references ``machinectl_cmd`` (the setup phases
    cross the boundary as root before the dispatcher exists, so they CANNOT
    route through ``core.dispatch`` — they match the pre-existing setup
    category instead). If none did, the positive test below would pass
    vacuously and prove nothing about the allowlist."""
    referencing = [
        m
        for m in _setup_modules()
        if _machinectl_cmd_references(
            ast.parse(m.read_text(), filename=str(m))
        )
    ]
    assert referencing, (
        "no src/core/setup/*.py module references machinectl_cmd — the "
        "positive allowlist assertion would be vacuous"
    )


def test_setup_modules_pass_the_pre_existing_machinectl_allowlist() -> None:
    """Every ``src/core/setup/*.py`` module is permitted by the live allowlist.

    Drives the SAME detector the live gate
    (:func:`tests.unit.test_conventions.test_machinectl_cmd_callers_restricted`)
    uses, scoped to just the setup package, against the real (unmodified)
    allowlist. Zero offenders ⇒ the setup phases are correctly covered by the
    pre-existing ``src/core/setup/*.py`` category and ``sandbox-setup`` needed
    no allowlist amendment.
    """
    allowlist = _machinectl_cmd_allowlist()
    offenders = _machinectl_cmd_violations(iter(_setup_modules()), allowlist)

    assert not offenders, (
        "setup module(s) reference machinectl_cmd but are NOT covered by the "
        "pre-existing src/core/setup/*.py allowlist category:\n"
        + "\n".join(
            f"  {p.relative_to(_SRC_ROOT.parent.parent)}: line(s) {linenos}"
            for p, linenos in offenders
        )
    )


def test_full_src_machinectl_gate_is_green_with_setup_present() -> None:
    """End-to-end (task 10.3): the whole-``src/`` restriction is green now that
    the setup package exists — no module references ``machinectl_cmd`` outside
    the three documented categories. Equivalent to the live gate, asserted here
    explicitly as the Group-10 post-setup confirmation."""
    allowlist = _machinectl_cmd_allowlist()
    offenders = _machinectl_cmd_violations(_python_files(_SRC_ROOT), allowlist)

    assert not offenders, (
        "machinectl_cmd referenced outside the 3-category allowlist:\n"
        + "\n".join(
            f"  {p.relative_to(_SRC_ROOT.parent.parent)}: line(s) {linenos}"
            for p, linenos in offenders
        )
    )


def test_synthetic_out_of_category_caller_still_fails(tmp_path: Path) -> None:
    """A caller OUTSIDE the three categories that imports ``machinectl_cmd``
    STILL fails the restriction, even with the setup glob populated.

    This is the load-bearing negative: it would fail (i.e. ``offenders`` would
    be empty) if the allowlist had been over-broadened to ``src/**`` to absorb
    the setup package. Because the real allowlist keys off exact repo-relative
    path membership, a synthetic file living elsewhere is never a member and is
    correctly flagged — proving the allowlist still discriminates correctly
    with ``src/core/setup/*.py`` present.
    """
    rogue = tmp_path / "rogue_setup_lookalike.py"
    rogue.write_text(
        "from core.host_config import machinectl_cmd\n\n"
        "machinectl_cmd('sandbox')\n"
    )

    # Use the full (setup-populated) allowlist, NOT the two-literal subset —
    # the point is that the synthetic caller fails even though the setup glob
    # is non-empty. tmp_path is outside the repo root, so it is never a member.
    allowlist = _machinectl_cmd_allowlist()
    assert any(p.name == "l5_dockerd.py" for p in _setup_modules()), (
        "setup glob expected to be non-empty for this negative to be meaningful"
    )

    offenders = _machinectl_cmd_violations(iter([rogue]), allowlist)

    assert offenders, (
        "synthetic out-of-category machinectl_cmd caller was NOT flagged — the "
        "allowlist may have been over-broadened to src/** to absorb the setup "
        "package (it must discriminate by exact path membership)"
    )
    assert rogue in {p for p, _ in offenders}


def test_synthetic_caller_passes_when_added_to_allowlist(tmp_path: Path) -> None:
    """Control: the SAME synthetic caller is NOT flagged when its path is in
    the allowlist. Confirms the detector's verdict is driven by allowlist
    membership (not some incidental property of the temp file), so the
    negative above is a real discriminating assertion, not a tautology."""
    rogue = tmp_path / "rogue_setup_lookalike.py"
    rogue.write_text("from core.host_config import machinectl_cmd\n")

    permissive = _MACHINECTL_CMD_LITERAL_ALLOWLIST | frozenset({rogue.as_posix()})
    offenders = _machinectl_cmd_violations(iter([rogue]), permissive)

    assert not offenders, (
        "detector flagged an allowlisted file — the negative test would then "
        "be a tautology rather than a membership-discriminating assertion"
    )
