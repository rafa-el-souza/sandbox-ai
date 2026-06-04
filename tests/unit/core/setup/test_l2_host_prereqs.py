"""Unit tests for ``core.setup.l2_host_prereqs`` (Group 5, task 5.4).

Covers the content-aware probe (ALREADY_CORRECT / MISSING for absent
user/group/machined/membership/subid, CONFLICT for the refuse-to-shrink
inadequate-subid case), act success (machined enable, useradd, subid append,
groupadd, usermod) + act failure, reverify, and the conftest content-aware
fixture.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from core.host_config import DockerExecutionMode, MachinectlAuth, minimal_host_config
from core.setup import l2_host_prereqs
from core.setup.l2_host_prereqs import PHASE
from core.setup.phase_runner import Identity, PhaseResult, SetupContext

if TYPE_CHECKING:
    from collections.abc import Callable

    from core.setup.phase_runner import Phase

    ContentAwareAssertion = Callable[
        [Phase, SetupContext, Callable[[], None]], None
    ]


def _ctx() -> SetupContext:
    # The operator is "alice" (the user the _World membership fixtures grant).
    return SetupContext(
        host_config=minimal_host_config("sandboxuser", MachinectlAuth.SUDO),
        operator="alice",
    )


_FULL = [(100000, 65536)]
_SHORT = [(100000, 1000)]


class _World:
    """Mutable fake of the host: users, groups, machined, subid ranges."""

    def __init__(self) -> None:
        self.users = {"sandboxuser", "alice"}
        self.groups = {"sb-ws": ["alice"]}
        self.machined = True
        self.subuid = _FULL
        self.subgid = _FULL
        # Bridge gid sits inside the (100000, 65536) subgid range.
        self.bridge_gid = 101000


def _install(monkeypatch: pytest.MonkeyPatch, w: _World) -> None:
    class _Pw:
        def __init__(self, name: str) -> None:
            self.pw_name = name
            self.pw_uid = 4242
            self.pw_gid = 4242

    def _getpwnam(n: str) -> _Pw:
        if n in w.users:
            return _Pw(n)
        raise KeyError(n)

    class _Gr:
        def __init__(self, name: str, members: list[str]) -> None:
            self.gr_name = name
            self.gr_mem = members
            self.gr_gid = w.bridge_gid

    def _getgrnam(n: str) -> _Gr:
        if n in w.groups:
            return _Gr(n, w.groups[n])
        raise KeyError(n)

    monkeypatch.setattr("pwd.getpwnam", _getpwnam)
    monkeypatch.setattr("grp.getgrnam", _getgrnam)
    monkeypatch.setattr(
        "core.setup.l2_host_prereqs.parse_subuid_for_user",
        lambda _u: list(w.subuid),
    )
    monkeypatch.setattr(
        "core.setup.l2_host_prereqs.parse_subgid_for_user",
        lambda _u: list(w.subgid),
    )

    class _P:
        def __init__(self, active: bool) -> None:
            self.stdout = "active" if active else "inactive"
            self.returncode = 0

    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: _P(w.machined),
    )


# ── PHASE wiring ─────────────────────────────────────────────────────────────


def test_phase_identity_and_deps() -> None:
    assert PHASE.id == "l2"
    assert PHASE.depends_on == ("l1",)
    assert PHASE.identity == Identity.ROOT
    # separate-user only: every L2 mutation is inapplicable or host-root-batch-
    # owned in operator-rootless (D5a/O3), so the runner reports it skipped there
    # — joining the M2 crossing-only phases (L3/L3a/L6.5/L8).
    assert PHASE.applies_in == frozenset({DockerExecutionMode.SEPARATE_USER})


# ── probe ────────────────────────────────────────────────────────────────────


def test_probe_already_correct(monkeypatch: pytest.MonkeyPatch) -> None:
    w = _World()
    _install(monkeypatch, w)
    result, detail = PHASE.probe(_ctx())
    assert result == PhaseResult.ALREADY_CORRECT
    assert "machined active" in detail


def test_probe_missing_user(monkeypatch: pytest.MonkeyPatch) -> None:
    w = _World()
    w.users = {"alice"}
    _install(monkeypatch, w)
    result, detail = PHASE.probe(_ctx())
    assert result == PhaseResult.MISSING
    assert "sandbox user" in detail


def test_probe_conflict_inadequate_subuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _World()
    w.subuid = _SHORT
    _install(monkeypatch, w)
    result, detail = PHASE.probe(_ctx())
    assert result == PhaseResult.CONFLICT
    assert "Refusing to shrink" in detail


def test_probe_missing_subid_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _World()
    w.subuid = []
    w.subgid = []
    _install(monkeypatch, w)
    result, detail = PHASE.probe(_ctx())
    assert result == PhaseResult.MISSING
    assert "subid" in detail


def test_probe_missing_subid_one_side_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _World()
    w.subgid = []
    _install(monkeypatch, w)
    result, detail = PHASE.probe(_ctx())
    assert result == PhaseResult.MISSING


def test_probe_missing_machined(monkeypatch: pytest.MonkeyPatch) -> None:
    w = _World()
    w.machined = False
    _install(monkeypatch, w)
    result, detail = PHASE.probe(_ctx())
    assert result == PhaseResult.MISSING
    assert "machined" in detail


def test_probe_missing_group(monkeypatch: pytest.MonkeyPatch) -> None:
    w = _World()
    w.groups = {}
    _install(monkeypatch, w)
    result, detail = PHASE.probe(_ctx())
    assert result == PhaseResult.MISSING
    assert "bridge group" in detail


def test_probe_missing_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _World()
    w.groups = {"sb-ws": []}
    _install(monkeypatch, w)
    result, detail = PHASE.probe(_ctx())
    assert result == PhaseResult.MISSING
    assert "not in group" in detail


def test_probe_membership_via_primary_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _World()
    w.groups = {"sb-ws": []}
    _install(monkeypatch, w)
    # operator's primary gid == bridge gid (w.bridge_gid) → member.

    class _Pw:
        pw_uid = 4242
        pw_gid = 101000

    def _getpwnam(n: str) -> _Pw:
        if n in {"alice", "sandboxuser"}:
            return _Pw()
        raise KeyError(n)

    monkeypatch.setattr("pwd.getpwnam", _getpwnam)
    result, _ = PHASE.probe(_ctx())
    assert result == PhaseResult.ALREADY_CORRECT


def test_probe_drift_bridge_gid_out_of_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _World()
    w.bridge_gid = 500  # outside the (100000, 65536) subgid range
    _install(monkeypatch, w)
    result, detail = PHASE.probe(_ctx())
    assert result == PhaseResult.DRIFT
    assert "outside" in detail


def test_act_refuses_bad_bridge_gid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _World()
    w.bridge_gid = 500
    _install(monkeypatch, w)
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _StdoutActive())
    with pytest.raises(RuntimeError, match="outside"):
        PHASE.act(_ctx())


def test_reverify_false_bad_bridge_gid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _World()
    w.bridge_gid = 500
    _install(monkeypatch, w)
    assert PHASE.reverify(_ctx()) is False


# ── act ──────────────────────────────────────────────────────────────────────


def test_act_full_convergence(monkeypatch: pytest.MonkeyPatch) -> None:
    w = _World()
    w.users = {"alice"}
    w.groups = {}
    w.machined = False
    w.subuid = []
    w.subgid = []
    _install(monkeypatch, w)
    runs: list[list[str]] = []

    class _P:
        stdout = "inactive"
        returncode = 0

    def _run(argv: list[str], **_k: object) -> object:
        runs.append(argv)
        return _P()

    monkeypatch.setattr("subprocess.run", _run)
    monkeypatch.setattr(
        "core.setup.l2_host_prereqs.autodetect_workspace_bridge_gid_recommendation",
        lambda _u: 9000,
    )
    detail = PHASE.act(_ctx())
    joined = [" ".join(r) for r in runs]
    assert any("systemctl enable --now systemd-machined" in j for j in joined)
    assert any("useradd --system" in j for j in joined)
    assert any("--add-subuids" in j for j in joined)
    assert any("--add-subgids" in j for j in joined)
    assert any("groupadd -g 9000 sb-ws" in j for j in joined)
    assert any("usermod -aG sb-ws alice" in j for j in joined)
    assert "created user" in detail


def test_act_already_converged(monkeypatch: pytest.MonkeyPatch) -> None:
    w = _World()
    _install(monkeypatch, w)
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _StdoutActive())
    detail = PHASE.act(_ctx())
    assert detail == "L2 already converged"


class _StdoutActive:
    stdout = "active"
    returncode = 0


def test_act_inadequate_subid_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _World()
    w.subuid = _SHORT
    _install(monkeypatch, w)
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _StdoutActive())
    with pytest.raises(RuntimeError, match="Refusing to shrink"):
        PHASE.act(_ctx())


def test_act_failure_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    w = _World()
    w.users = {"alice"}
    _install(monkeypatch, w)

    def _boom(argv: list[str], **_k: object) -> object:
        if argv[:1] == ["useradd"]:
            raise subprocess.CalledProcessError(1, argv)
        return _StdoutActive()

    monkeypatch.setattr("subprocess.run", _boom)
    with pytest.raises(subprocess.CalledProcessError):
        PHASE.act(_ctx())


# ── reverify ─────────────────────────────────────────────────────────────────


def test_reverify_true(monkeypatch: pytest.MonkeyPatch) -> None:
    w = _World()
    _install(monkeypatch, w)
    assert PHASE.reverify(_ctx()) is True


def test_reverify_false_no_machined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _World()
    w.machined = False
    _install(monkeypatch, w)
    assert PHASE.reverify(_ctx()) is False


def test_reverify_false_no_user(monkeypatch: pytest.MonkeyPatch) -> None:
    w = _World()
    w.users = {"alice"}
    _install(monkeypatch, w)
    assert PHASE.reverify(_ctx()) is False


def test_reverify_false_inadequate_subid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _World()
    w.subuid = _SHORT
    _install(monkeypatch, w)
    assert PHASE.reverify(_ctx()) is False


def test_reverify_false_no_group(monkeypatch: pytest.MonkeyPatch) -> None:
    w = _World()
    w.groups = {}
    _install(monkeypatch, w)
    assert PHASE.reverify(_ctx()) is False


def test_operator_in_group_primary_lookup_keyerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Group exists but operator is not a listed member; the primary-group
    # fallback's pwd.getpwnam(operator) raises KeyError → returns False.
    class _Gr:
        def __init__(self) -> None:
            self.gr_mem: list[str] = []
            self.gr_gid = 101000

    monkeypatch.setattr("grp.getgrnam", lambda _n: _Gr())

    def _boom(_n: str) -> object:
        raise KeyError(_n)

    monkeypatch.setattr("pwd.getpwnam", _boom)
    assert l2_host_prereqs._operator_in_group("ghost", "sb-ws") is False


def test_user_admin_groups_supplementary_and_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``alice`` is a supplementary member of ``sudo`` and her primary gid is
    # ``wheel``'s gid → both surface; ``admin`` is absent from /etc/group.
    class _Gr:
        def __init__(self, gid: int, mem: list[str]) -> None:
            self.gr_gid = gid
            self.gr_mem = mem

    groups = {"sudo": _Gr(27, ["alice"]), "wheel": _Gr(10, [])}

    def _getgrnam(name: str) -> object:
        if name not in groups:
            raise KeyError(name)
        return groups[name]

    monkeypatch.setattr("grp.getgrnam", _getgrnam)
    monkeypatch.setattr("pwd.getpwnam", lambda _n: type("P", (), {"pw_gid": 10})())
    assert l2_host_prereqs._user_admin_groups("alice") == ["sudo", "wheel"]


def test_user_admin_groups_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("grp.getgrnam", lambda _n: type("G", (), {"gr_gid": 27, "gr_mem": []})())
    monkeypatch.setattr("pwd.getpwnam", lambda _n: type("P", (), {"pw_gid": 1000})())
    assert l2_host_prereqs._user_admin_groups("sandbox") == []


def test_user_admin_groups_unknown_user_no_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The user is absent from /etc/passwd (no primary gid) but is listed as a
    # supplementary member of ``sudo`` — that membership still surfaces.
    def _getgrnam(name: str) -> object:
        if name == "sudo":
            return type("G", (), {"gr_gid": 27, "gr_mem": ["ghost"]})()
        raise KeyError(name)

    def _boom(_n: str) -> object:
        raise KeyError(_n)

    monkeypatch.setattr("grp.getgrnam", _getgrnam)
    monkeypatch.setattr("pwd.getpwnam", _boom)
    assert l2_host_prereqs._user_admin_groups("ghost") == ["sudo"]


def test_machined_active_subprocess_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_a: object, **_k: object) -> object:
        raise OSError

    monkeypatch.setattr("subprocess.run", _boom)
    assert l2_host_prereqs._machined_active() is False


# ── content-aware probe contract (conftest fixture) ──────────────────────────


def test_content_aware(
    monkeypatch: pytest.MonkeyPatch,
    assert_phase_content_aware: ContentAwareAssertion,
) -> None:
    w = _World()
    _install(monkeypatch, w)

    def _make_stale() -> None:
        # The sb-ws group's gid is moved outside the sandbox user's subgid
        # range under us (operator hand-edit / wheel-upgrade range change).
        # The probe must report DRIFT (content compare, not mere presence) —
        # design D10.
        w.bridge_gid = 500

    assert_phase_content_aware(PHASE, _ctx(), _make_stale)
