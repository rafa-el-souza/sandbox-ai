"""Unit tests for the L7 helper-image pre-pull phase.

Covers: probe branches (ALREADY_CORRECT pinned-digest cached, DRIFT
tag-moved, MISSING no-image), act pull, reverify true/false, the content-aware
fixture (pinned→tag-drift), and the PHASE shape. ``Executor.run`` is faked —
no real machinectl / docker.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest
from core.exceptions import SandboxExecutionError
from core.host_config import MachinectlAuth, minimal_host_config
from core.setup import l7_helper_image as l7
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


class _Store:
    """Fake local image store: a set of resolvable refs."""

    def __init__(self) -> None:
        self.refs: set[str] = set()
        self.pulled: list[str] = []


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    s = _Store()

    def fake_run(
        _self: object,
        cmd: list[str],
        *_a: object,
        **_kw: object,
    ) -> subprocess.CompletedProcess[str]:
        inner = cmd[-1]
        if inner.startswith("docker image inspect "):
            ref = inner.removeprefix("docker image inspect ")
            if ref in s.refs:
                return subprocess.CompletedProcess(cmd, 0, "[]", "")
            raise SandboxExecutionError("no such image")
        if inner.startswith("docker pull "):
            ref = inner.removeprefix("docker pull ")
            s.pulled.append(ref)
            s.refs.add(ref)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(f"unexpected: {inner}")

    monkeypatch.setattr("core.executor.Executor.run", fake_run)
    return s


def test_probe_already_correct_when_digest_cached(
    store: _Store, ctx: SetupContext
) -> None:
    store.refs.add(l7._HELPER_REF)
    result, _ = l7.PHASE.probe(ctx)
    assert result == PhaseResult.ALREADY_CORRECT


def test_probe_drift_when_tag_moved(store: _Store, ctx: SetupContext) -> None:
    store.refs.add(l7._HELPER_TAGGED)
    result, detail = l7.PHASE.probe(ctx)
    assert result == PhaseResult.DRIFT
    assert "not at the pinned digest" in detail


def test_probe_missing_when_no_image(store: _Store, ctx: SetupContext) -> None:
    result, _ = l7.PHASE.probe(ctx)
    assert result == PhaseResult.MISSING


def test_act_pulls_pinned_digest(store: _Store, ctx: SetupContext) -> None:
    detail = l7.PHASE.act(ctx)
    assert l7._HELPER_REF in store.pulled
    assert "pulled" in detail


def test_reverify_true_after_pull(store: _Store, ctx: SetupContext) -> None:
    l7.PHASE.act(ctx)
    assert l7.PHASE.reverify(ctx) is True


def test_reverify_false_when_absent(store: _Store, ctx: SetupContext) -> None:
    assert l7.PHASE.reverify(ctx) is False


def test_content_aware(
    store: _Store,
    ctx: SetupContext,
    assert_phase_content_aware: Callable[
        [Phase, SetupContext, Callable[[], None]], None
    ],
) -> None:
    store.refs.add(l7._HELPER_REF)

    def make_stale() -> None:
        # An upstream rotation moved the tag: the pinned digest is gone, only
        # the (now-different) tag resolves locally. Probe must report DRIFT.
        store.refs.discard(l7._HELPER_REF)
        store.refs.add(l7._HELPER_TAGGED)

    assert_phase_content_aware(l7.PHASE, ctx, make_stale)


def test_phase_shape() -> None:
    assert l7.PHASE.id == "l7"
    assert l7.PHASE.depends_on == ("l65",)
    assert l7.PHASE.identity == Identity.SANDBOX
