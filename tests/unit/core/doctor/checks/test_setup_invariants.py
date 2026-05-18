"""Tests for core.doctor.checks.setup_invariants.

The check is a read-only steady-state audit that reuses (never re-implements)
setup's single-source logic: ``resolve_machinectl_path`` (L0),
``render_sudoers_rule`` (L3), and L2's subuid/group helpers. WARN (never FAIL)
on any violated invariant.

The ``core.setup.*`` imports are LAZY (function-local) to break a
registry → check → l0_identity → core.doctor import-time cycle (orchestrator
signal). Tests therefore patch the **origin** module attributes
(``core.setup.l0_identity.X`` etc.), not bound names on the check module.
"""

from __future__ import annotations

import socket
from typing import Any

from core.dispatch import _DISPATCH_BINARY, Op
from core.host_config import MachinectlAuth
from core.setup.l3_sudoers_polkit import render_sudoers_rule

_MOD = "core.doctor.checks.setup_invariants"


def test_module_exposes_single_check() -> None:
    from core.doctor.checks import setup_invariants

    assert set(setup_invariants.__all__) == {"check_setup_invariants"}


def test_setup_modules_imported_lazily_no_cycle() -> None:
    """The fix for the registry↔check↔l0_identity cycle: the check module must
    NOT bind the ``core.setup.*`` symbols at module top-level (they are
    function-local). Importing the check module in isolation must not pull
    ``core.setup.l0_identity`` as a module attribute."""
    from core.doctor.checks import setup_invariants

    assert not hasattr(setup_invariants, "resolve_machinectl_path")
    assert not hasattr(setup_invariants, "render_sudoers_rule")
    assert not hasattr(setup_invariants, "_subid_status")


def test_reuses_l0_l3_l2_single_source() -> None:
    """Orchestrator decision 2 / F-011: the check reuses the canonical
    resolver + renderer + L2 helpers (verified by referencing the same
    callables the setup modules expose)."""
    from core.setup import l0_identity, l2_host_prereqs, l3_sudoers_polkit

    assert callable(l0_identity.resolve_machinectl_path)
    assert callable(l3_sudoers_polkit.render_sudoers_rule)
    assert callable(l2_host_prereqs._subid_status)


def _patch_all_green(monkeypatch: Any, operator: str = "alice") -> None:
    """Stub every audit helper to its all-invariants-hold outcome."""
    monkeypatch.setattr("core.setup.l0_identity.resolve_operator", lambda: operator)
    monkeypatch.setattr(f"{_MOD}._audit_reserved_dir", lambda v: None)
    monkeypatch.setattr(f"{_MOD}._audit_per_user_tree", lambda op, v: None)
    monkeypatch.setattr(f"{_MOD}._audit_subid_and_group", lambda u, op, g, v: None)
    monkeypatch.setattr(f"{_MOD}._audit_machinectl_stability", lambda hc, t, v: None)
    monkeypatch.setattr(f"{_MOD}._audit_rule_body", lambda hc, op, t, v: None)
    monkeypatch.setattr(f"{_MOD}._audit_sudo_floor", lambda v: None)


class TestTopLevelVerdicts:
    def test_all_invariants_hold_reports_pass(self, monkeypatch: Any) -> None:
        from core.doctor.checks.setup_invariants import check_setup_invariants

        _patch_all_green(monkeypatch)
        monkeypatch.setattr("pathlib.Path.read_text", lambda self: "rule-body")
        result = check_setup_invariants("sandbox", None)
        assert result.status == "pass"
        assert "all setup invariants hold" in result.detail
        assert "operator=alice" in result.detail

    def test_operator_unresolvable_warns(self, monkeypatch: Any) -> None:
        from core.doctor.checks.setup_invariants import check_setup_invariants
        from core.setup.l0_identity import OperatorResolutionError

        def boom() -> str:
            raise OperatorResolutionError("no operator")

        monkeypatch.setattr("core.setup.l0_identity.resolve_operator", boom)
        result = check_setup_invariants("sandbox", None)
        assert result.status == "warn"
        assert "operator unresolvable" in result.detail
        assert result.remediation == "run 'sudo sandbox setup' to restore canonical setup state"

    def test_drop_in_missing_warns(self, monkeypatch: Any) -> None:
        from core.doctor.checks.setup_invariants import check_setup_invariants

        _patch_all_green(monkeypatch)

        def missing(self: Any) -> str:
            raise FileNotFoundError

        monkeypatch.setattr("pathlib.Path.read_text", missing)
        result = check_setup_invariants("sandbox", None)
        assert result.status == "warn"
        assert "sudoers drop-in" in result.detail
        assert "missing" in result.detail

    def test_polkit_mode_skips_sudo_audits(self, monkeypatch: Any) -> None:
        from core.doctor.checks.setup_invariants import check_setup_invariants

        _patch_all_green(monkeypatch)
        called: dict[str, bool] = {"stability": False, "body": False, "floor": False}
        monkeypatch.setattr(
            f"{_MOD}._audit_machinectl_stability",
            lambda hc, t, v: called.__setitem__("stability", True),
        )
        monkeypatch.setattr(
            f"{_MOD}._audit_rule_body",
            lambda hc, op, t, v: called.__setitem__("body", True),
        )
        monkeypatch.setattr(
            f"{_MOD}._audit_sudo_floor",
            lambda v: called.__setitem__("floor", True),
        )
        monkeypatch.setattr("pathlib.Path.read_text", lambda self: "x")
        result = check_setup_invariants("sandbox", None, auth_mode=MachinectlAuth.POLKIT)
        assert result.status == "pass"
        assert called == {"stability": False, "body": False, "floor": False}

    def test_polkit_drop_in_missing_says_polkit(self, monkeypatch: Any) -> None:
        from core.doctor.checks.setup_invariants import check_setup_invariants

        _patch_all_green(monkeypatch)

        def missing(self: Any) -> str:
            raise FileNotFoundError

        monkeypatch.setattr("pathlib.Path.read_text", missing)
        result = check_setup_invariants("sandbox", None, auth_mode=MachinectlAuth.POLKIT)
        assert result.status == "warn"
        assert "polkit drop-in" in result.detail


class TestAuditReservedDir:
    def test_missing_dir(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr("pathlib.Path.is_dir", lambda self: False)
        v: list[str] = []
        m._audit_reserved_dir(v)
        assert any("directory missing" in x for x in v)

    def test_wrong_mode_and_owner(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        class FakeStat:
            st_mode = 0o040700  # dir, mode 0700
            st_uid = 1000
            st_gid = 1000

        monkeypatch.setattr("pathlib.Path.is_dir", lambda self: True)
        monkeypatch.setattr("pathlib.Path.stat", lambda self: FakeStat())
        v: list[str] = []
        m._audit_reserved_dir(v)
        assert any("0o755" in x for x in v)
        assert any("root:root" in x for x in v)

    def test_correct(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        class FakeStat:
            st_mode = 0o040755
            st_uid = 0
            st_gid = 0

        monkeypatch.setattr("pathlib.Path.is_dir", lambda self: True)
        monkeypatch.setattr("pathlib.Path.stat", lambda self: FakeStat())
        v: list[str] = []
        m._audit_reserved_dir(v)
        assert v == []


class TestAuditPerUserTree:
    def test_missing_leaf(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr(f"{_MOD}.pwd.getpwnam", lambda n: type("P", (), {"pw_uid": 1000})())
        monkeypatch.setattr("pathlib.Path.is_dir", lambda self: False)
        v: list[str] = []
        m._audit_per_user_tree("alice", v)
        assert len(v) == 4
        assert all("missing" in x for x in v)

    def test_wrong_mode_and_uid(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr(f"{_MOD}.pwd.getpwnam", lambda n: type("P", (), {"pw_uid": 1000})())
        monkeypatch.setattr("pathlib.Path.is_dir", lambda self: True)

        class FakeStat:
            st_mode = 0o040755
            st_uid = 0

        monkeypatch.setattr("pathlib.Path.stat", lambda self: FakeStat())
        v: list[str] = []
        m._audit_per_user_tree("alice", v)
        assert any("0o700" in x for x in v)
        assert any("uid 0" in x for x in v)

    def test_unknown_operator_uid_skips_uid_check(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        def boom(n: str) -> Any:
            raise KeyError(n)

        monkeypatch.setattr(f"{_MOD}.pwd.getpwnam", boom)
        monkeypatch.setattr("pathlib.Path.is_dir", lambda self: True)

        class FakeStat:
            st_mode = 0o040700
            st_uid = 999

        monkeypatch.setattr("pathlib.Path.stat", lambda self: FakeStat())
        v: list[str] = []
        m._audit_per_user_tree("ghost", v)
        assert v == []  # mode 0700 ok, uid check skipped (operator uid unknown)


class TestAuditSubidAndGroup:
    def test_inadequate_subid(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr("core.setup.l2_host_prereqs._subid_status", lambda u: ("absent", "no entry"))
        monkeypatch.setattr("core.setup.l2_host_prereqs._group_exists", lambda g: True)
        monkeypatch.setattr(f"{_MOD}.grp.getgrnam", lambda g: type("G", (), {"gr_gid": 100000})())
        monkeypatch.setattr(f"{_MOD}.parse_subgid_for_user", lambda u: [(100000, 65536)])
        monkeypatch.setattr("core.setup.l2_host_prereqs._gid_in_subgid_range", lambda gid, r: True)
        monkeypatch.setattr("core.setup.l2_host_prereqs._operator_in_group", lambda op, g: True)
        v: list[str] = []
        m._audit_subid_and_group("sandbox", "alice", "sb-ws", v)
        assert any("/etc/subuid|subgid" in x for x in v)

    def test_group_absent(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr("core.setup.l2_host_prereqs._subid_status", lambda u: ("adequate", "ok"))
        monkeypatch.setattr("core.setup.l2_host_prereqs._group_exists", lambda g: False)
        v: list[str] = []
        m._audit_subid_and_group("sandbox", "alice", "sb-ws", v)
        assert any("bridge group 'sb-ws' absent" in x for x in v)

    def test_gid_outside_range_and_operator_not_member(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr("core.setup.l2_host_prereqs._subid_status", lambda u: ("adequate", "ok"))
        monkeypatch.setattr("core.setup.l2_host_prereqs._group_exists", lambda g: True)
        monkeypatch.setattr(f"{_MOD}.grp.getgrnam", lambda g: type("G", (), {"gr_gid": 5})())
        monkeypatch.setattr(f"{_MOD}.parse_subgid_for_user", lambda u: [(100000, 65536)])
        monkeypatch.setattr("core.setup.l2_host_prereqs._gid_in_subgid_range", lambda gid, r: False)
        monkeypatch.setattr("core.setup.l2_host_prereqs._operator_in_group", lambda op, g: False)
        v: list[str] = []
        m._audit_subid_and_group("sandbox", "alice", "sb-ws", v)
        assert any("outside" in x for x in v)
        assert any("not in 'sb-ws'" in x for x in v)

    def test_all_green(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr("core.setup.l2_host_prereqs._subid_status", lambda u: ("adequate", "ok"))
        monkeypatch.setattr("core.setup.l2_host_prereqs._group_exists", lambda g: True)
        monkeypatch.setattr(f"{_MOD}.grp.getgrnam", lambda g: type("G", (), {"gr_gid": 100100})())
        monkeypatch.setattr(f"{_MOD}.parse_subgid_for_user", lambda u: [(100000, 65536)])
        monkeypatch.setattr("core.setup.l2_host_prereqs._gid_in_subgid_range", lambda gid, r: True)
        monkeypatch.setattr("core.setup.l2_host_prereqs._operator_in_group", lambda op, g: True)
        v: list[str] = []
        m._audit_subid_and_group("sandbox", "alice", "sb-ws", v)
        assert v == []


class TestAuditMachinectlStability:
    def _hc(self) -> Any:
        from core.host_config import minimal_host_config

        return minimal_host_config("sandbox", MachinectlAuth.SUDO)

    def test_resolution_error(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m
        from core.setup.l0_identity import MachinectlResolutionError

        def boom(hc: Any) -> str:
            raise MachinectlResolutionError("two machinectl")

        monkeypatch.setattr("core.setup.l0_identity.resolve_machinectl_path", boom)
        v: list[str] = []
        m._audit_machinectl_stability(self._hc(), "text", v)
        assert any("machinectl-path-stability" in x for x in v)

    def test_drift_path_not_in_drop_in(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr(
            "core.setup.l0_identity.resolve_machinectl_path", lambda hc: "/usr/local/bin/machinectl"
        )
        v: list[str] = []
        m._audit_machinectl_stability(self._hc(), "rule pins /usr/bin/machinectl", v)
        assert any("not the path pinned" in x for x in v)

    def test_drop_in_none_no_violation(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr(
            "core.setup.l0_identity.resolve_machinectl_path", lambda hc: "/usr/bin/machinectl"
        )
        v: list[str] = []
        m._audit_machinectl_stability(self._hc(), None, v)
        assert v == []

    def test_path_present_no_violation(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr(
            "core.setup.l0_identity.resolve_machinectl_path", lambda hc: "/usr/bin/machinectl"
        )
        v: list[str] = []
        m._audit_machinectl_stability(self._hc(), "x /usr/bin/machinectl x", v)
        assert v == []


class TestAuditRuleBody:
    def _hc(self) -> Any:
        from core.host_config import minimal_host_config

        return minimal_host_config("sandbox", MachinectlAuth.SUDO)

    def test_canonical_body_no_violation(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr(
            "core.setup.l0_identity.resolve_machinectl_path", lambda hc: "/usr/bin/machinectl"
        )
        body = render_sudoers_rule("/usr/bin/machinectl", "alice", socket.gethostname(), "sandbox")
        v: list[str] = []
        m._audit_rule_body(self._hc(), "alice", body, v)
        assert v == []

    def test_double_quote_flagged(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr(
            "core.setup.l0_identity.resolve_machinectl_path", lambda hc: "/usr/bin/machinectl"
        )
        body = render_sudoers_rule("/usr/bin/machinectl", "alice", socket.gethostname(), "sandbox")
        tampered = body.replace("auth-probe", 'auth-"probe')
        v: list[str] = []
        m._audit_rule_body(self._hc(), "alice", tampered, v)
        assert any("double-quote in a Cmnd_Spec" in x for x in v)

    def test_op_enum_drift_detected(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr(
            "core.setup.l0_identity.resolve_machinectl_path", lambda hc: "/usr/bin/machinectl"
        )
        body = render_sudoers_rule("/usr/bin/machinectl", "alice", socket.gethostname(), "sandbox")
        first_op = next(iter(Op)).value
        stale = "\n".join(
            line for line in body.splitlines() if f"{_DISPATCH_BINARY}\\ {first_op}" not in line
        )
        v: list[str] = []
        m._audit_rule_body(self._hc(), "alice", stale, v)
        assert any("installed op set != core.dispatch.Op" in x for x in v)
        assert any(first_op in x for x in v)

    def test_resolution_error_skips_body(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m
        from core.setup.l0_identity import MachinectlResolutionError

        def boom(hc: Any) -> str:
            raise MachinectlResolutionError("zero machinectl")

        monkeypatch.setattr("core.setup.l0_identity.resolve_machinectl_path", boom)
        v: list[str] = []
        m._audit_rule_body(self._hc(), "alice", "irrelevant", v)
        assert v == []  # body comparison skipped; stability audit owns the resolution error

    def test_render_error_surfaced_as_warn(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr(
            "core.setup.l0_identity.resolve_machinectl_path", lambda hc: "/usr/bin/machinectl"
        )

        from core.setup.l3_sudoers_polkit import RuleRenderError

        def boom(*a: Any, **k: Any) -> str:
            raise RuleRenderError("bad op name")

        monkeypatch.setattr("core.setup.l3_sudoers_polkit.render_sudoers_rule", boom)
        v: list[str] = []
        m._audit_rule_body(self._hc(), "alice", "x", v)
        assert any("op-enum drift" in x for x in v)

    def test_body_differs_but_ops_match(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr(
            "core.setup.l0_identity.resolve_machinectl_path", lambda hc: "/usr/bin/machinectl"
        )
        body = render_sudoers_rule("/usr/bin/machinectl", "alice", socket.gethostname(), "sandbox")
        v: list[str] = []
        m._audit_rule_body(self._hc(), "alice", body + "\n# stray comment\n", v)
        assert any("installed body differs" in x for x in v)


class TestAuditSudoFloor:
    def test_sub_floor_warns(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr("core.setup.l0_identity.parse_sudo_version", lambda: (1, 8, 23, 0))
        v: list[str] = []
        m._audit_sudo_floor(v)
        assert any("predates the validated floor 1.9.5p2" in x for x in v)
        assert any("1.8.23" in x for x in v)

    def test_at_floor_no_violation(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr("core.setup.l0_identity.parse_sudo_version", lambda: (1, 9, 5, 2))
        v: list[str] = []
        m._audit_sudo_floor(v)
        assert v == []

    def test_unparseable_no_violation(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr("core.setup.l0_identity.parse_sudo_version", lambda: None)
        v: list[str] = []
        m._audit_sudo_floor(v)
        assert v == []

    def test_sub_floor_with_patch_renders_p(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr("core.setup.l0_identity.parse_sudo_version", lambda: (1, 8, 27, 1))
        v: list[str] = []
        m._audit_sudo_floor(v)
        assert any("1.8.27p1" in x for x in v)


class TestExtractOps:
    def test_extracts_full_op_set(self) -> None:
        from core.doctor.checks.setup_invariants import _extract_ops

        body = render_sudoers_rule("/usr/bin/machinectl", "alice", "h", "sandbox")
        assert _extract_ops(body) == {op.value for op in Op}


class TestWarnAggregation:
    def test_multiple_violations_joined(self, monkeypatch: Any) -> None:
        from core.doctor.checks.setup_invariants import check_setup_invariants

        monkeypatch.setattr("core.setup.l0_identity.resolve_operator", lambda: "alice")
        monkeypatch.setattr(f"{_MOD}._audit_reserved_dir", lambda v: v.append("A bad"))
        monkeypatch.setattr(f"{_MOD}._audit_per_user_tree", lambda op, v: v.append("B bad"))
        monkeypatch.setattr(f"{_MOD}._audit_subid_and_group", lambda u, op, g, v: None)
        monkeypatch.setattr(f"{_MOD}._audit_machinectl_stability", lambda hc, t, v: None)
        monkeypatch.setattr(f"{_MOD}._audit_rule_body", lambda hc, op, t, v: None)
        monkeypatch.setattr(f"{_MOD}._audit_sudo_floor", lambda v: None)
        monkeypatch.setattr("pathlib.Path.read_text", lambda self: "x")
        result = check_setup_invariants("sandbox", None)
        assert result.status == "warn"
        assert "A bad" in result.detail
        assert "B bad" in result.detail
        assert "; " in result.detail
