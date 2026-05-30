"""Unit tests for ``core.setup.l8_fresh_session`` (L8 — fresh-session re-probe).

Covers: the always-MISSING verification probe; the ``id -G`` bridge-gid
membership check (present -> pass; absent -> FAIL); the end-to-end
machinectl-reachable check recovering the inner exit via the dispatcher's
begin/exit framing (``framed=True``); the bridge-group-missing refusal; PHASE
wiring (no rollback — verification mutates nothing).
"""

from __future__ import annotations

import subprocess

import pytest
from core.exceptions import SandboxExecutionError
from core.host_config import (
    DockerExecutionMode,
    HostConfig,
    MachinectlAuth,
    minimal_host_config,
)
from core.setup import l8_fresh_session as l8
from core.setup.l8_fresh_session import PHASE, FreshSessionError
from core.setup.phase_runner import Identity, PhaseResult, SetupContext


class _Grp:
    gr_gid = 4242


@pytest.fixture(autouse=True)
def _bridge_group(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "core.setup.l8_fresh_session.grp.getgrnam", lambda _name: _Grp()
    )


def _hc() -> HostConfig:
    return minimal_host_config("sandbox", MachinectlAuth.SUDO)


def _ctx() -> SetupContext:
    return SetupContext(host_config=_hc(), operator="alice")


class _FakeExecutor:
    """Stand-in: ``id -G`` returns ``id_gids``; the machinectl probe matches
    unless ``machinectl_error`` is set (the sentinel non-zero-inner signal).
    """

    def __init__(
        self,
        id_gids: str = "1000 4242 27",
        machinectl_error: SandboxExecutionError | None = None,
    ):
        self._id_gids = id_gids
        self._machinectl_error = machinectl_error
        self.calls: list[list[str]] = []
        self.framed_flags: list[bool] = []

    def run(
        self, cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        self.framed_flags.append(bool(kwargs.get("framed")))
        if "id" in cmd and "-G" in cmd:
            return subprocess.CompletedProcess(cmd, 0, self._id_gids, "")
        # The machinectl auth-probe path runs with framed=True (the dispatcher
        # emits the begin/exit framing; the crossed payload stays bare so the
        # per-op rule matches — F-018, NOT a sentinel=True wrap).
        assert kwargs.get("framed") is True
        if self._machinectl_error is not None:
            raise self._machinectl_error
        return subprocess.CompletedProcess(cmd, 0, "ok", "")


def _install(
    monkeypatch: pytest.MonkeyPatch, fake: _FakeExecutor
) -> _FakeExecutor:
    monkeypatch.setattr(l8, "Executor", lambda: fake)
    return fake


# ── probe is always MISSING (verification phase) ─────────────────────────────


def test_probe_always_missing() -> None:
    result, detail = l8._probe(_ctx())
    assert result == PhaseResult.MISSING
    assert "no idempotent skip" in detail


# ── happy path: group set + machinectl reachable ─────────────────────────────


def test_verify_passes_when_group_and_machinectl_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch, _FakeExecutor())
    detail = l8._act(_ctx())
    assert "fresh-session verified" in detail
    # First call is `id -G` via pipe_cmd (not framed); second is the
    # framed-recovered machinectl auth-probe (the dispatcher emits the framing).
    assert "id" in fake.calls[0]
    assert fake.framed_flags[0] is False
    assert fake.framed_flags[1] is True


def test_group_check_uses_pipe_cmd_machinectl_uses_sudo_u(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G1/F-016: the two checks use different operator-drop primitives.

    The ``id -G`` group check is a plain binary → ``pipe_cmd`` (systemd-run
    --uid) is correct. The machinectl-reachability check runs setuid ``sudo``,
    which a systemd-run --uid transient unit cannot exec (EXIT_EXEC 203 — the
    same F-016 defect fixed in L3a), so it MUST drop via ``sudo_as_operator``
    (``sudo -u``). Round-5 fedora 12.4 hit the empty-sentinel here. The argv
    captured is the real L8 output (only the boundary call is faked).
    """
    fake = _install(monkeypatch, _FakeExecutor())
    l8._act(_ctx())
    # calls[0] = id -G via pipe_cmd (systemd-run --uid); calls[1] = machinectl
    # via sudo_as_operator.
    group_argv, machinectl_argv = fake.calls[0], fake.calls[1]
    assert group_argv[0] == "systemd-run"
    assert "id" in group_argv and "-G" in group_argv
    assert machinectl_argv[:3] == ["sudo", "-u", "alice"]
    assert "systemd-run" not in machinectl_argv
    assert not any(a.startswith("--uid=") for a in machinectl_argv)


def test_reverify_true_when_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _FakeExecutor())
    assert l8._reverify(_ctx()) is True


# ── bridge gid not in fresh-session group set ────────────────────────────────


def test_fail_when_bridge_gid_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _FakeExecutor(id_gids="1000 27 100"))
    with pytest.raises(FreshSessionError, match="does not include"):
        l8._act(_ctx())


def test_fail_when_id_command_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Err(_FakeExecutor):
        def run(
            self, cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if "id" in cmd:
                raise SandboxExecutionError("id -G blew up")
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

    _install(monkeypatch, _Err())
    with pytest.raises(FreshSessionError, match="`id -G`"):
        l8._act(_ctx())


# ── machinectl not reachable through the new rule ────────────────────────────


def test_fail_when_machinectl_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    err = SandboxExecutionError(
        "[FATAL] Inner command failed with exit status 2."
    )
    _install(monkeypatch, _FakeExecutor(machinectl_error=err))
    with pytest.raises(FreshSessionError, match="NOT reachable end-to-end"):
        l8._act(_ctx())


# ── bridge group missing on host ─────────────────────────────────────────────


def test_fail_when_bridge_group_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(_name: str) -> _Grp:
        raise KeyError(_name)

    monkeypatch.setattr(
        "core.setup.l8_fresh_session.grp.getgrnam", _raise
    )
    _install(monkeypatch, _FakeExecutor())
    with pytest.raises(FreshSessionError, match="does not exist on this host"):
        l8._act(_ctx())


# ── PHASE wiring ─────────────────────────────────────────────────────────────


def test_phase_identity_and_graph() -> None:
    assert PHASE.id == "l8"
    assert PHASE.depends_on == ("l3a",)
    assert PHASE.identity == Identity.OPERATOR
    assert PHASE.rollback is None
    # no crossing/boundary group to re-probe in operator-rootless → sep-user only.
    assert PHASE.applies_in == frozenset({DockerExecutionMode.SEPARATE_USER})
