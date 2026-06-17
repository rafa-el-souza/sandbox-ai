# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the optional fapolicyd integration phase.

Covers: probe refusals (binary absent → CONFLICT, trust.d dir absent →
CONFLICT), probe MISSING / ALREADY_CORRECT / DRIFT, the content-aware
contract (a rebuilt managed binary flips the probe to DRIFT), the act
(file write + 0644 + fapolicyd-cli --update + not-running warning), reverify
true/false, and the PHASE shape. ``shutil.which`` + ``Executor`` + the
reserved-namespace paths are all faked — no real fapolicyd is touched.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from core.exceptions import SandboxExecutionError
from core.host_config import DockerExecutionMode, minimal_host_config
from core.setup.extras import fapolicyd as fap
from core.setup.phase_runner import Identity, PhaseResult, SetupContext

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

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
class _Exec:
    """Fake Executor: records argv, drives per-command outcomes."""

    calls: list[list[str]] = field(default_factory=list[list[str]])
    update_ok: bool = True
    is_active_ok: bool = True
    check_trust_ok: bool = True
    check_trust_stdout: str = "/usr/local/libexec/sandbox-ai/dispatch trusted: yes\n"

    def run(
        self, cmd: list[str], **_kw: object
    ) -> object:
        self.calls.append(cmd)
        if cmd[:2] == ["fapolicyd-cli", "--update"]:
            if not self.update_ok:
                raise SandboxExecutionError("update failed")
        elif cmd[:2] == ["systemctl", "is-active"]:
            if not self.is_active_ok:
                raise SandboxExecutionError("inactive")
        elif cmd[:2] == ["fapolicyd-cli", "--check-trust"]:
            if not self.check_trust_ok:
                raise SandboxExecutionError("check-trust failed")
            return _Result(self.check_trust_stdout)
        return _Result("")


@dataclass
class _Result:
    stdout: str


@dataclass
class _World:
    """Faked filesystem world for the fapolicyd phase."""

    which_fapolicyd: bool = True
    executor: _Exec = field(default_factory=_Exec)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _World:
    w = _World()

    # Two synthetic managed binaries with known content.
    dispatch = tmp_path / "dispatch"
    runsc = tmp_path / "runsc"
    dispatch.write_bytes(b"DISPATCH-BINARY-CONTENT")
    runsc.write_bytes(b"RUNSC-BINARY-CONTENT")
    dropin = tmp_path / "sandbox-ai.trust"
    trust_d = tmp_path / "trust.d"
    trust_d.mkdir()

    monkeypatch.setattr(fap, "_DISPATCH_PATH", str(dispatch))
    monkeypatch.setattr(fap, "_RUNSC_PATH", str(runsc))
    monkeypatch.setattr(
        fap, "_MANAGED_BINARIES", (str(dispatch), str(runsc))
    )
    monkeypatch.setattr(fap, "_TRUST_DROPIN_PATH", str(dropin))
    monkeypatch.setattr(fap, "_TRUST_DROPIN_DIR", str(trust_d))

    def fake_which(name: str) -> str | None:
        if name == "fapolicyd":
            return "/usr/sbin/fapolicyd" if w.which_fapolicyd else None
        return None

    monkeypatch.setattr("shutil.which", fake_which)
    monkeypatch.setattr(fap, "Executor", lambda: w.executor)

    # os.chown is root-only; neutralize it for the unprivileged test runner.
    monkeypatch.setattr("os.chown", lambda *_a, **_k: None)

    return w


def _expected_content() -> str:
    return fap._render_trust_content()


def _write_existing(content: str) -> None:
    with open(fap._TRUST_DROPIN_PATH, "w", encoding="utf-8") as fh:
        fh.write(content)


# ── Probe refusals ───────────────────────────────────────────────────────────


def test_probe_refuses_when_fapolicyd_absent(
    world: _World, ctx: SetupContext
) -> None:
    world.which_fapolicyd = False
    result, detail = fap.PHASE.probe(ctx)
    assert result == PhaseResult.CONFLICT
    assert "fapolicyd not installed" in detail


def test_probe_refuses_when_trust_d_missing(
    world: _World, ctx: SetupContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fap, "_TRUST_DROPIN_DIR", "/nonexistent/trust.d")
    result, detail = fap.PHASE.probe(ctx)
    assert result == PhaseResult.CONFLICT
    assert "trust.d directory missing" in detail


# ── Probe state classification ───────────────────────────────────────────────


def test_probe_missing_when_dropin_absent(
    world: _World, ctx: SetupContext
) -> None:
    result, detail = fap.PHASE.probe(ctx)
    assert result == PhaseResult.MISSING
    assert "absent" in detail


def test_probe_already_correct_when_content_matches(
    world: _World, ctx: SetupContext
) -> None:
    _write_existing(_expected_content())
    result, _ = fap.PHASE.probe(ctx)
    assert result == PhaseResult.ALREADY_CORRECT


def test_probe_drift_when_content_stale(
    world: _World, ctx: SetupContext
) -> None:
    _write_existing("# sandbox-ai managed\n/old/path 1 deadbeef\n")
    result, detail = fap.PHASE.probe(ctx)
    assert result == PhaseResult.DRIFT
    assert "stale" in detail


# ── Content-aware contract ───────────────────────────────────────────────────


def test_content_aware(
    world: _World,
    ctx: SetupContext,
    assert_phase_content_aware: Callable[
        [Phase, SetupContext, Callable[[], None]], None
    ],
) -> None:
    _write_existing(_expected_content())

    def make_stale() -> None:
        # The dispatcher binary was rebuilt (a wheel upgrade / recompile):
        # its size + sha256 change, so the recorded trust line is now stale.
        with open(fap._DISPATCH_PATH, "wb") as fh:
            fh.write(b"REBUILT-DISPATCH-WITH-DIFFERENT-BYTES")

    assert_phase_content_aware(fap.PHASE, ctx, make_stale)


# ── Act ──────────────────────────────────────────────────────────────────────


def test_act_writes_dropin_and_reloads(
    world: _World, ctx: SetupContext
) -> None:
    detail = fap.PHASE.act(ctx)
    with open(fap._TRUST_DROPIN_PATH, encoding="utf-8") as fh:
        written = fh.read()
    assert written == _expected_content()
    assert written.startswith(
        "# sandbox-ai managed — do not edit; rerun 'sudo sandbox setup'"
    )
    # Each managed binary recorded as `<path> <size> <sha256>`.
    sha = hashlib.sha256(b"DISPATCH-BINARY-CONTENT").hexdigest()
    assert f"{fap._DISPATCH_PATH} 23 {sha}" in written
    assert ["fapolicyd-cli", "--update"] in world.executor.calls
    assert "reloaded the fapolicyd trust DB" in detail


def test_act_warns_when_fapolicyd_inactive(
    world: _World, ctx: SetupContext
) -> None:
    world.executor.is_active_ok = False
    detail = fap.PHASE.act(ctx)
    assert "WARNING" in detail
    assert "not running" in detail


def test_act_no_warning_when_fapolicyd_active(
    world: _World, ctx: SetupContext
) -> None:
    detail = fap.PHASE.act(ctx)
    assert "WARNING" not in detail


# ── Reverify ─────────────────────────────────────────────────────────────────


def test_reverify_true_when_trusted(
    world: _World, ctx: SetupContext
) -> None:
    assert fap.PHASE.reverify(ctx) is True


def test_reverify_false_when_not_trusted(
    world: _World, ctx: SetupContext
) -> None:
    world.executor.check_trust_stdout = "dispatch trusted: no\n"
    assert fap.PHASE.reverify(ctx) is False


def test_reverify_false_on_check_trust_error(
    world: _World, ctx: SetupContext
) -> None:
    world.executor.check_trust_ok = False
    assert fap.PHASE.reverify(ctx) is False


# ── PHASE shape ──────────────────────────────────────────────────────────────


def test_phase_shape() -> None:
    assert fap.PHASE.id == "fapolicyd"
    assert fap.PHASE.depends_on == ("l8",)
    assert fap.PHASE.identity == Identity.ROOT
    assert fap.PHASE.rollback is None
