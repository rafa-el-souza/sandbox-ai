# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the L6a runsc-install phase (shape #3).

Covers: probe branches (MISSING absent, ALREADY_CORRECT match, DRIFT
no-force, DRIFT force), act install / drift-skip / force-overwrite, reverify
true/false, the ``set_force_update`` toggle, the content-aware fixture, and the
PHASE shape. ``core.binary_install`` is faked — no real network / chattr.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from core.host_config import (
    DockerExecutionMode,
    minimal_host_config,
)
from core.setup import l6a_runsc as l6a
from core.setup.phase_runner import Identity, PhaseResult, SetupContext

if TYPE_CHECKING:
    from collections.abc import Callable

    from core.setup.phase_runner import Phase


@pytest.fixture
def ctx() -> SetupContext:
    return SetupContext(
        host_config=minimal_host_config(
            "sandboxuser", DockerExecutionMode.SEPARATE_USER
        ),
        operator="op",
    )


@dataclass
class _Drift:
    status: str
    installed_sha: str | None
    pinned_sha: str


class _World:
    """Fake binary_install: a settable drift status + install recorder."""

    def __init__(self) -> None:
        self.status = "absent"
        self.installs: list[bool] = []  # records `force` per install_pinned


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> _World:
    w = _World()

    def fake_detect(_name: str, _hc: object) -> _Drift:
        if w.status == "absent":
            return _Drift("absent", None, "PINNED")
        if w.status == "match":
            return _Drift("match", "PINNED", "PINNED")
        return _Drift("drift", "INSTALLED", "PINNED")

    def fake_install(_name: str, _hc: object, *, force: bool = False) -> None:
        w.installs.append(force)
        w.status = "match"

    monkeypatch.setattr(l6a, "detect_drift", fake_detect)
    monkeypatch.setattr(l6a, "install_pinned", fake_install)
    return w


@pytest.fixture(autouse=True)
def _reset_force() -> object:
    l6a.set_force_update(False)
    yield
    l6a.set_force_update(False)


def test_probe_missing_when_absent(world: _World, ctx: SetupContext) -> None:
    result, detail = l6a.PHASE.probe(ctx)
    assert result == PhaseResult.MISSING
    assert "absent" in detail


def test_probe_already_correct_when_match(
    world: _World, ctx: SetupContext
) -> None:
    world.status = "match"
    result, _ = l6a.PHASE.probe(ctx)
    assert result == PhaseResult.ALREADY_CORRECT


def test_probe_drift_no_force_mentions_update_command(
    world: _World, ctx: SetupContext
) -> None:
    world.status = "drift"
    result, detail = l6a.PHASE.probe(ctx)
    assert result == PhaseResult.DRIFT
    assert "sudo sandbox setup --update-runsc" in detail
    assert "--update-runsc: will overwrite" not in detail


def test_probe_drift_with_force_notes_overwrite(
    world: _World, ctx: SetupContext
) -> None:
    world.status = "drift"
    l6a.set_force_update(True)
    result, detail = l6a.PHASE.probe(ctx)
    assert result == PhaseResult.DRIFT
    assert "will overwrite" in detail


def test_act_installs_when_absent(world: _World, ctx: SetupContext) -> None:
    detail = l6a.PHASE.act(ctx)
    assert world.installs == [False]
    assert "installed" in detail


def test_act_drift_skip_does_not_overwrite(
    world: _World, ctx: SetupContext
) -> None:
    world.status = "drift"
    detail = l6a.PHASE.act(ctx)
    assert world.installs == []
    assert "left in place" in detail


def test_act_force_overwrites_drift(world: _World, ctx: SetupContext) -> None:
    world.status = "drift"
    l6a.set_force_update(True)
    l6a.PHASE.act(ctx)
    assert world.installs == [True]


def test_reverify_true_after_install(world: _World, ctx: SetupContext) -> None:
    l6a.PHASE.act(ctx)
    assert l6a.PHASE.reverify(ctx) is True


def test_reverify_false_after_drift_skip(
    world: _World, ctx: SetupContext
) -> None:
    world.status = "drift"
    l6a.PHASE.act(ctx)
    assert l6a.PHASE.reverify(ctx) is False


def test_content_aware(
    world: _World,
    ctx: SetupContext,
    assert_phase_content_aware: Callable[
        [Phase, SetupContext, Callable[[], None]], None
    ],
) -> None:
    world.status = "match"

    def make_stale() -> None:
        # The on-disk runsc sha drifted from the pinned sha (a registry pin
        # rotation, or a tampered binary). Probe must flip to DRIFT.
        world.status = "drift"

    assert_phase_content_aware(l6a.PHASE, ctx, make_stale)


def test_phase_shape() -> None:
    assert l6a.PHASE.id == "l6a"
    assert l6a.PHASE.depends_on == ("l6",)
    assert l6a.PHASE.identity == Identity.ROOT
    # separate-user only: the root-owned runsc install is host-root, so in
    # operator-rootless it is owned by the host_batch RUNSC item + _bootstrap-host
    # escalation (D5a/O3); the runner reports the phase skipped there.
    assert l6a.PHASE.applies_in == frozenset(
        {DockerExecutionMode.SEPARATE_USER}
    )


def test_update_runsc_subset_ordering_needs_external_deps_flag() -> None:
    """The REAL l6a phase, filtered alone, must order only with the flag.

    Regression for the round-5 fedora 12.3 crash (F-016 sibling E1):
    ``--update-runsc`` filters the phase list to just ``l6a``, whose real
    ``depends_on=("l6",)`` is then a dangling edge. Strict ``order_phases``
    raises ``PhaseDependencyError``; the subset path must pass
    ``allow_external_deps=True``. Asserting against ``l6a.PHASE`` (not a
    synthetic stand-in) ties the fix to the actual phase that crashed.
    """
    from core.setup.phase_runner import PhaseDependencyError, order_phases

    with pytest.raises(PhaseDependencyError, match="unknown phase 'l6'"):
        order_phases([l6a.PHASE])

    ordered = order_phases([l6a.PHASE], allow_external_deps=True)
    assert [p.id for p in ordered] == ["l6a"]
