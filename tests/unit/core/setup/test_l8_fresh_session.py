"""Unit tests for ``core.setup.l8_fresh_session`` (L8 — fresh-session re-probe).

Covers: the always-MISSING verification probe; the ``id -G`` bridge-gid
membership check (present -> pass; absent -> FAIL); the end-to-end
machinectl-reachable check recovering the inner exit via the sentinel
mechanism; the bridge-group-missing refusal; PHASE wiring (no rollback —
verification mutates nothing).
"""

from __future__ import annotations

import subprocess

import pytest
from core.exceptions import SandboxExecutionError
from core.host_config import HostConfig, MachinectlAuth, minimal_host_config
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
        self.sentinel_flags: list[bool] = []

    def run(
        self, cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        self.sentinel_flags.append(bool(kwargs.get("sentinel")))
        if "id" in cmd and "-G" in cmd:
            return subprocess.CompletedProcess(cmd, 0, self._id_gids, "")
        # The machinectl auth-probe path runs with sentinel=True.
        assert kwargs.get("sentinel") is True
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
    # First call is `id -G` (no sentinel); second is the sentinel-recovered
    # machinectl auth-probe.
    assert "id" in fake.calls[0]
    assert fake.sentinel_flags[0] is False
    assert fake.sentinel_flags[1] is True


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
