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
from core.host_config import (
    DockerExecutionMode,
    MachinectlAuth,
    minimal_host_config,
)
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
        # ``user@<uid>.service`` readiness for the post-linger gate (FIX-B-i).
        # Default ready — existing converged-host tests do not exercise the
        # not-yet-ready window; dedicated tests below toggle this off.
        self.user_manager_ready = True
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
        if "systemctl is-active user@" in joined:
            # Post-linger readiness poll — the inner bash loop succeeds iff
            # the manager is "ready", otherwise the loop exits 1 and Executor
            # raises SandboxExecutionError (matches real-world behavior).
            if w.user_manager_ready:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            raise SandboxExecutionError(
                "[FATAL] Sandbox Execution Fault: Command failed with exit status 1.\n"
                "Error Trace:\nuser@4242.service did not become active"
            )
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
    l1/l2a/l6.
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
    # L4 was deleted (round-9/F-021); L5 now depends on L2a, which transitively
    # depends on L2 (the sandbox user's creator).
    assert l5.PHASE.depends_on == ("l2a",)
    assert l5.PHASE.identity == Identity.SANDBOX


# ── FIX-A regression: loginctl show-user errors on freshly-created user ──────


def test_probe_missing_when_loginctl_show_user_raises(
    ctx: SetupContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``loginctl show-user`` raising must be tolerated as MISSING.

    Round-3 smoke (fedora 12.1b, arch 12.1b) failed identically: a freshly
    created sandbox user (post-L2, never logged in) is unknown to
    ``systemd-logind`` — ``loginctl show-user … --property=Linger`` returns
    exit 1 with "User ID N is not logged in or lingering". The previous
    implementation called ``Executor().run([...])`` (default ``check=True``)
    so the ``CalledProcessError`` wrapped to ``SandboxExecutionError``
    propagated through ``_probe`` and the phase classified FAIL. F-014
    same-class recurrence of the round-3 L5 fix.

    Post-fix expectation: ``_linger_enabled`` catches the
    ``SandboxExecutionError`` and returns ``False``, so ``_probe`` reaches
    the ``not linger_enabled`` branch and returns ``MISSING`` (apply pass
    proceeds to ``enable-linger``).
    """
    # User present (post-L2), but loginctl errors as in the smoke evidence.
    monkeypatch.setattr("pwd.getpwnam", lambda _n: _present_pw())

    def _loginctl_errors(
        _self: object, cmd: list[str], *_a: object, **_kw: object
    ) -> subprocess.CompletedProcess[str]:
        if "show-user" in cmd:
            raise SandboxExecutionError(
                "[FATAL] Sandbox Execution Fault: Command "
                "'loginctl show-user sandbox --property=Linger' failed with "
                "exit status 1.\nError Trace:\nFailed to get user: User ID "
                "4242 is not logged in or lingering"
            )
        raise AssertionError(f"unexpected command: {' '.join(cmd)}")

    monkeypatch.setattr("core.executor.Executor.run", _loginctl_errors)

    result, detail = l5.PHASE.probe(ctx)
    assert result == PhaseResult.MISSING
    assert "linger not enabled" in detail


# ── FIX-B-i regression: post-linger user-manager readiness poll ──────────────


def test_act_polls_user_manager_before_install(
    world: _World, ctx: SetupContext
) -> None:
    """The readiness poll must happen between ``enable-linger`` and install.

    A freshly-lingered user's ``user@<uid>.service`` takes a moment to come
    up; crossing via ``machinectl shell`` against an unready manager returns
    empty stdout (sentinel-not-found fail-closed, observed on fedora 12.2).
    The poll uses ``systemctl is-active user@<uid>.service`` root-side (no
    crossing) in a bounded shell-retry loop, exactly between the
    ``enable-linger`` mutation and the rootless-install crossing.
    """
    detail = l5.PHASE.act(ctx)
    assert "installed" in detail

    # Locate the three load-bearing commands in call order.
    enable_idx = next(
        i for i, c in enumerate(world.calls) if "enable-linger" in c
    )
    poll_idx = next(
        i for i, c in enumerate(world.calls)
        if "systemctl is-active user@" in c
    )
    install_idx = next(
        i for i, c in enumerate(world.calls)
        if "dockerd-rootless-setuptool.sh install" in c
    )
    # Enforce ordering: enable-linger → readiness poll → install crossing.
    assert enable_idx < poll_idx < install_idx
    # The poll targets the sandbox user's uid (4242 from _present_pw).
    poll_cmd = world.calls[poll_idx]
    assert "user@4242.service" in poll_cmd
    # The poll is a bounded retry loop (not a one-shot).
    assert "seq 1 30" in poll_cmd


def test_act_polls_user_manager_when_dockerd_already_up(
    world: _World, ctx: SetupContext
) -> None:
    """The poll must also gate the dockerd-already-up shortcut path.

    The shortcut path crosses via ``_dockerd_reachable`` (``docker info``);
    that crossing also needs a ready user manager.
    """
    world.dockerd = True
    l5.PHASE.act(ctx)
    enable_idx = next(
        i for i, c in enumerate(world.calls) if "enable-linger" in c
    )
    poll_idx = next(
        i for i, c in enumerate(world.calls)
        if "systemctl is-active user@" in c
    )
    info_idx = next(
        i for i, c in enumerate(world.calls) if "docker info" in c
    )
    assert enable_idx < poll_idx < info_idx


def test_act_raises_when_user_manager_never_ready(
    world: _World, ctx: SetupContext
) -> None:
    """If the per-user manager never becomes active, act surfaces the failure.

    The bounded shell-retry loop's ``exit 1`` raises a
    ``SandboxExecutionError`` from ``Executor().run`` (which the phase_runner
    catches and classifies FAIL with a diagnostic). The diagnostic mentions
    the user-manager unit explicitly.
    """
    world.user_manager_ready = False
    with pytest.raises(SandboxExecutionError) as exc:
        l5.PHASE.act(ctx)
    assert "user@4242.service" in str(exc.value)
    # No install crossing must occur after a failed readiness gate.
    assert not any(
        "dockerd-rootless-setuptool.sh install" in c for c in world.calls
    )


# ── operator-rootless: LOCAL crossing, no machinectl, no linger (§6.2) ────────


def _oprootless_ctx() -> SetupContext:
    return SetupContext(
        host_config=minimal_host_config(
            "sandboxuser",
            MachinectlAuth.SUDO,
            mode=DockerExecutionMode.OPERATOR_ROOTLESS,
        ),
        operator="alice",
    )


def test_act_operator_rootless_local_no_linger(monkeypatch: pytest.MonkeyPatch) -> None:
    """op-rootless act installs dockerd LOCAL (no machinectl), no linger/readiness gate.

    Linger is host-root-batch-owned in operator-rootless (the ``LINGER`` item),
    and setup runs in the operator's own live session — so L5 does NOT
    ``enable-linger``, does NOT poll ``user@<uid>.service``, and crosses with an
    empty LOCAL prefix (sentinel off — a local command's exit is not masked).
    """
    seen: list[tuple[list[str], object]] = []

    def fake_run(
        _self: object, cmd: list[str], *_a: object, **kw: object
    ) -> subprocess.CompletedProcess[str]:
        seen.append((cmd, kw.get("sentinel")))
        if "dockerd-rootless-setuptool.sh install" in " ".join(cmd):
            return subprocess.CompletedProcess(cmd, 0, "", "")
        # docker info: not-up before install (force install), up after.
        installed = any(
            "dockerd-rootless-setuptool.sh install" in " ".join(c) for c, _ in seen
        )
        if not installed:
            raise SandboxExecutionError("docker info failed")
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr("core.executor.Executor.run", fake_run)

    detail = l5.PHASE.act(_oprootless_ctx())

    joined = [" ".join(c) for c, _ in seen]
    assert not any("enable-linger" in j for j in joined)
    assert not any("user@" in j for j in joined)
    assert not any("machinectl" in j for j in joined)
    install = [
        (c, s) for c, s in seen if "dockerd-rootless-setuptool.sh install" in " ".join(c)
    ]
    assert len(install) == 1
    cmd, sentinel = install[0]
    assert cmd[0] == "/bin/bash"  # empty crossing prefix → bash is argv[0]
    assert sentinel is False
    assert "alice" in detail


def test_probe_operator_rootless_local_skips_linger_and_pw_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """op-rootless probe checks only LOCAL dockerd reachability (owner = operator).

    The not-yet-created-user guard and the linger check are separate-user-only,
    so ``pwd.getpwnam`` must NOT be consulted (we make it raise to prove it) and
    no ``loginctl show-user`` crossing happens.
    """
    seen: list[tuple[list[str], object]] = []

    def fake_run(
        _self: object, cmd: list[str], *_a: object, **kw: object
    ) -> subprocess.CompletedProcess[str]:
        seen.append((cmd, kw.get("sentinel")))
        if "docker info" in " ".join(cmd):
            return subprocess.CompletedProcess(cmd, 0, "ok", "")
        raise AssertionError(f"unexpected command: {' '.join(cmd)}")

    monkeypatch.setattr("core.executor.Executor.run", fake_run)

    def _boom(_n: str) -> object:
        raise KeyError("pwd.getpwnam must not be called on the op-rootless path")

    monkeypatch.setattr("pwd.getpwnam", _boom)

    result, detail = l5.PHASE.probe(_oprootless_ctx())

    assert result == PhaseResult.ALREADY_CORRECT
    assert "alice" in detail
    joined = [" ".join(c) for c, _ in seen]
    assert all("show-user" not in j for j in joined)
    assert all("machinectl" not in j for j in joined)
    assert all(s is False for _, s in seen)
