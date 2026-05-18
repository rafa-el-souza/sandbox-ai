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
from core.host_config import MachinectlAuth, minimal_host_config
from core.setup import l6a_runsc as l6a
from core.setup.phase_runner import Identity, PhaseResult, SetupContext

if TYPE_CHECKING:
    from collections.abc import Callable

    from core.setup.phase_runner import Phase


@pytest.fixture
def ctx() -> SetupContext:
    return SetupContext(
        host_config=minimal_host_config("sandboxuser", MachinectlAuth.SUDO),
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
