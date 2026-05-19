"""Unit tests for the L5 linger + rootless-dockerd phase.

Covers: probe branches (linger-absent MISSING, dockerd-unreachable MISSING,
ALREADY_CORRECT), act enabling linger + installing only when dockerd absent,
act skipping install when dockerd already up, reverify true/false, the
already-correct→missing transition (L5 has no DRIFT dimension — an
unreachable rootless dockerd is MISSING, not stale), and the PHASE shape. All
``Executor.run`` calls are faked — no real ``loginctl`` / ``machinectl`` /
docker.
"""

from __future__ import annotations

import pwd
import subprocess

import pytest
from core.exceptions import SandboxExecutionError
from core.host_config import MachinectlAuth, minimal_host_config
from core.setup import l5_dockerd as l5
from core.setup.phase_runner import Identity, PhaseResult, SetupContext


def _present_pw() -> pwd.struct_passwd:
    """A real ``pwd.struct_passwd`` for the sandbox user.

    ``probe_sandbox_pw_or_missing`` discriminates the positive case with
    ``isinstance(result, pwd.struct_passwd)``, so the fake must be a genuine
    ``pwd.struct_passwd`` — built from the canonical 7-tuple.
    """
    return pwd.struct_passwd(
        ("sandboxuser", "x", 4242, 4242, "", "/home/sandboxuser", "/bin/sh")
    )


@pytest.fixture
def ctx() -> SetupContext:
    return SetupContext(
        host_config=minimal_host_config("sandboxuser", MachinectlAuth.SUDO),
        operator="op",
    )


class _World:
    """Mutable fake of the host: linger flag + dockerd reachability."""

    def __init__(self) -> None:
        self.linger = False
        self.dockerd = False
        self.calls: list[str] = []


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> _World:
    w = _World()

    def fake_run(
        _self: object,
        cmd: list[str],
        *_a: object,
        **_kw: object,
    ) -> subprocess.CompletedProcess[str]:
        joined = " ".join(cmd)
        w.calls.append(joined)
        if "show-user" in cmd and "--property=Linger" in joined:
            val = "yes" if w.linger else "no"
            return subprocess.CompletedProcess(cmd, 0, f"Linger={val}\n", "")
        if "enable-linger" in cmd:
            w.linger = True
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if "docker info" in joined:
            if not w.dockerd:
                raise SandboxExecutionError("docker info failed")
            return subprocess.CompletedProcess(cmd, 0, "ok", "")
        if "dockerd-rootless-setuptool.sh install" in joined:
            w.dockerd = True
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(f"unexpected command: {joined}")

    monkeypatch.setattr("core.executor.Executor.run", fake_run)
    # The sandbox user EXISTS for the converged-host probe/act/reverify tests
    # (post-L2). The not-yet-created case has its own dedicated tests below.
    monkeypatch.setattr("pwd.getpwnam", lambda _n: _present_pw())
    return w


def test_probe_missing_when_linger_absent(
    world: _World, ctx: SetupContext
) -> None:
    result, detail = l5.PHASE.probe(ctx)
    assert result == PhaseResult.MISSING
    assert "linger not enabled" in detail


def test_probe_missing_when_dockerd_unreachable(
    world: _World, ctx: SetupContext
) -> None:
    world.linger = True
    result, detail = l5.PHASE.probe(ctx)
    assert result == PhaseResult.MISSING
    assert "dockerd not reachable" in detail


def test_probe_already_correct(world: _World, ctx: SetupContext) -> None:
    world.linger = True
    world.dockerd = True
    result, _ = l5.PHASE.probe(ctx)
    assert result == PhaseResult.ALREADY_CORRECT


def test_probe_missing_when_sandbox_user_absent(
    world: _World, ctx: SetupContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh-host first run: L2 has not created the user yet.

    The plan pass runs all probes before any act, so ``pwd.getpwnam`` raises
    ``KeyError``. L5 must return MISSING (NOT raise, NOT run
    ``loginctl``/``docker info``) with the not-yet-created wording, mirroring
    l1/l2a/l4/l6.
    """

    def _no_user(_n: str) -> pwd.struct_passwd:
        raise KeyError(_n)

    monkeypatch.setattr("pwd.getpwnam", _no_user)
    result, detail = l5.PHASE.probe(ctx)
    assert result == PhaseResult.MISSING
    assert "does not exist yet" in detail
    assert "created by L2" in detail
    assert "dockerd will be installed" in detail
    # The guard short-circuits BEFORE any loginctl / docker info crosses.
    assert not any("show-user" in c for c in world.calls)
    assert not any("docker info" in c for c in world.calls)


def test_probe_non_user_error_still_propagates(
    world: _World, ctx: SetupContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real L5 fault with the user PRESENT must NOT be masked as MISSING.

    The guard only handles the not-yet-created user. With the user present,
    an unexpected ``loginctl`` fault (e.g. logind down) propagates so the
    systemic phase-runner guard classifies the phase FAIL — it is not
    silently swallowed into a MISSING.
    """

    def _boom_run(
        _self: object, cmd: list[str], *_a: object, **_kw: object
    ) -> subprocess.CompletedProcess[str]:
        if "show-user" in cmd:
            raise RuntimeError("logind is down")
        raise AssertionError(f"unexpected command: {' '.join(cmd)}")

    monkeypatch.setattr("core.executor.Executor.run", _boom_run)
    with pytest.raises(RuntimeError, match="logind is down"):
        l5.PHASE.probe(ctx)


def test_act_enables_linger_and_installs_dockerd(
    world: _World, ctx: SetupContext
) -> None:
    detail = l5.PHASE.act(ctx)
    assert world.linger is True
    assert world.dockerd is True
    assert "installed" in detail
    assert any("enable-linger" in c for c in world.calls)
    assert any("dockerd-rootless-setuptool.sh install" in c for c in world.calls)


def test_act_skips_install_when_dockerd_already_up(
    world: _World, ctx: SetupContext
) -> None:
    world.dockerd = True
    detail = l5.PHASE.act(ctx)
    assert world.linger is True
    assert "already up" in detail
    assert not any(
        "dockerd-rootless-setuptool.sh install" in c for c in world.calls
    )


def test_reverify_true_when_converged(world: _World, ctx: SetupContext) -> None:
    world.linger = True
    world.dockerd = True
    assert l5.PHASE.reverify(ctx) is True


def test_reverify_false_when_linger_off(world: _World, ctx: SetupContext) -> None:
    world.dockerd = True
    assert l5.PHASE.reverify(ctx) is False


def test_reverify_false_when_dockerd_down(
    world: _World, ctx: SetupContext
) -> None:
    world.linger = True
    assert l5.PHASE.reverify(ctx) is False


def test_probe_already_correct_then_missing(
    world: _World,
    ctx: SetupContext,
) -> None:
    # L5 legitimately has no DRIFT dimension: an unreachable rootless dockerd
    # is MISSING, not DRIFT (the install is absent / not running, not a stale
    # version). So this asserts the honest L5 contract — ALREADY_CORRECT when
    # converged, MISSING once dockerd stops being reachable — rather than
    # using the content-aware (DRIFT) fixture, which does not apply here.
    world.linger = True
    world.dockerd = True

    before, _ = l5.PHASE.probe(ctx)
    assert before == PhaseResult.ALREADY_CORRECT

    # Rootless dockerd became unreachable out of band (toggle dockerd off).
    world.dockerd = False
    after, _ = l5.PHASE.probe(ctx)
    assert after == PhaseResult.MISSING


def test_phase_shape() -> None:
    assert l5.PHASE.id == "l5"
    assert l5.PHASE.depends_on == ("l4",)
    assert l5.PHASE.identity == Identity.SANDBOX
