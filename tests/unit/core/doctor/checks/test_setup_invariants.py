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
from core.setup.l3_sudoers import render_sudoers_rule

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
    from core.setup import l0_identity, l2_host_prereqs, l3_sudoers

    assert callable(l0_identity.resolve_machinectl_path)
    assert callable(l3_sudoers.render_sudoers_rule)
    assert callable(l2_host_prereqs._subid_status)


def _grant(*, granted: bool, nopasswd: bool, determinable: bool) -> Any:
    """Build a :class:`core.setup.l2_host_prereqs.SudoersGrant` for stubbing."""
    from core.setup.l2_host_prereqs import SudoersGrant

    return SudoersGrant(granted=granted, nopasswd=nopasswd, determinable=determinable)


def _patch_all_green(monkeypatch: Any, operator: str = "alice") -> None:
    """Stub every audit helper to its all-invariants-hold outcome."""
    monkeypatch.setattr("core.setup.l0_identity.resolve_operator", lambda: operator)
    monkeypatch.setattr(f"{_MOD}._audit_reserved_dir", lambda v: None)
    monkeypatch.setattr(f"{_MOD}._audit_subid_and_group", lambda u, op, g, v: None)
    monkeypatch.setattr(f"{_MOD}._audit_daemon_user_no_admin", lambda hc, v: None)
    monkeypatch.setattr(f"{_MOD}._audit_machinectl_stability", lambda hc, v: None)
    monkeypatch.setattr(f"{_MOD}._audit_systemd_run_stability", lambda hc, t, v: None)
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

    def test_root_only_drop_in_under_plain_doctor_passes_with_note(self, monkeypatch: Any) -> None:
        # Under a plain `sandbox doctor` the operator can't read the root-only
        # 0440 sudoers drop-in → PermissionError. The audit MUST NOT crash: skip
        # the rule-body audit and PASS-with-note (other invariants hold).
        from core.doctor.checks.setup_invariants import check_setup_invariants

        _patch_all_green(monkeypatch)

        def _denied(self: Any) -> str:
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr("pathlib.Path.read_text", _denied)
        result = check_setup_invariants("sandbox", None)
        assert result.status == "pass"
        assert "root-only" in result.detail
        assert "L3a per-op probe" in result.detail

    def test_operator_unresolvable_falls_back_to_current_user(self, monkeypatch: Any) -> None:
        # Under a plain `sandbox doctor` (no sudo context) resolve_operator()
        # raises; the check must NOT short-circuit (that left the audit dead in
        # its normal invocation) — it falls back to the current real user and
        # runs the full audit. resolve_operator() stays strict for setup itself.
        import os
        import pwd

        from core.doctor.checks.setup_invariants import check_setup_invariants
        from core.setup.l0_identity import OperatorResolutionError

        _patch_all_green(monkeypatch)

        def boom() -> str:
            raise OperatorResolutionError("no operator")

        monkeypatch.setattr("core.setup.l0_identity.resolve_operator", boom)
        monkeypatch.setattr("pathlib.Path.read_text", lambda self: "rule-body")

        result = check_setup_invariants("sandbox", None)
        current = pwd.getpwuid(os.getuid()).pw_name
        assert result.status == "pass"
        assert f"operator={current}" in result.detail
        assert "operator unresolvable" not in result.detail

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

class TestOperatorRootlessBranch:
    """C-005 1.4 / design D2: in operator-rootless the check stays both-mode but
    skips its machinectl-stability + sudoers-rule-body sub-audits (no crossing /
    rule exists) and runs ONLY the mode-applicable sub-audits (reserved dir +
    subid/subgid/bridge-group)."""

    def test_operator_rootless_passes_without_reading_drop_in(self, monkeypatch: Any) -> None:
        from core.doctor.checks.setup_invariants import check_setup_invariants
        from core.host_config import DockerExecutionMode

        monkeypatch.setattr("core.setup.l0_identity.resolve_operator", lambda: "alice")
        monkeypatch.setattr(f"{_MOD}._audit_reserved_dir", lambda v: None)
        monkeypatch.setattr(f"{_MOD}._audit_subid_and_group", lambda u, op, g, v: None)

        def must_not_run_machinectl(*a: Any, **k: Any) -> None:
            raise AssertionError("_audit_machinectl_stability must be skipped in operator-rootless")

        def must_not_run_rule_body(*a: Any, **k: Any) -> None:
            raise AssertionError("_audit_rule_body must be skipped in operator-rootless")

        def must_not_read(self: Any) -> str:
            raise AssertionError("the drop-in must not be read in operator-rootless")

        monkeypatch.setattr(f"{_MOD}._audit_machinectl_stability", must_not_run_machinectl)
        monkeypatch.setattr(f"{_MOD}._audit_rule_body", must_not_run_rule_body)
        monkeypatch.setattr("pathlib.Path.read_text", must_not_read)

        result = check_setup_invariants("sandbox", None, mode=DockerExecutionMode.OPERATOR_ROOTLESS)
        assert result.status == "pass"
        assert "operator-rootless setup invariants hold" in result.detail
        assert "operator=alice" in result.detail
        assert "not applicable" in result.detail

    def test_operator_rootless_runs_subid_and_reserved_dir_audits(self, monkeypatch: Any) -> None:
        from core.doctor.checks.setup_invariants import check_setup_invariants
        from core.host_config import DockerExecutionMode

        ran: list[str] = []
        monkeypatch.setattr("core.setup.l0_identity.resolve_operator", lambda: "alice")
        monkeypatch.setattr(f"{_MOD}._audit_reserved_dir", lambda v: ran.append("reserved"))
        monkeypatch.setattr(f"{_MOD}._audit_subid_and_group", lambda u, op, g, v: ran.append("subid"))
        monkeypatch.setattr(f"{_MOD}._audit_machinectl_stability", lambda *a, **k: ran.append("machinectl"))
        monkeypatch.setattr(f"{_MOD}._audit_rule_body", lambda *a, **k: ran.append("rule_body"))

        check_setup_invariants("sandbox", None, mode=DockerExecutionMode.OPERATOR_ROOTLESS)
        assert "reserved" in ran
        assert "subid" in ran
        assert "machinectl" not in ran
        assert "rule_body" not in ran

    def test_operator_rootless_warns_on_subid_violation(self, monkeypatch: Any) -> None:
        from core.doctor.checks.setup_invariants import check_setup_invariants
        from core.host_config import DockerExecutionMode

        monkeypatch.setattr("core.setup.l0_identity.resolve_operator", lambda: "alice")
        monkeypatch.setattr(f"{_MOD}._audit_reserved_dir", lambda v: None)
        monkeypatch.setattr(
            f"{_MOD}._audit_subid_and_group",
            lambda u, op, g, v: v.append("bridge group 'sb-ws' absent per /etc/group"),
        )

        result = check_setup_invariants("sandbox", None, mode=DockerExecutionMode.OPERATOR_ROOTLESS)
        assert result.status == "warn"
        assert "sb-ws" in result.detail
        assert result.remediation == "run 'sudo sandbox setup' to restore canonical setup state"


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


class TestAuditDaemonUserNoAdmin:
    """C-005 2.1 / design D3: the dedicated daemon user must be in NO admin group
    (separate-user only). PASS (no violation) when clean; WARN with remediation
    when the daemon user is in sudo/wheel/admin."""

    def _hc(self) -> Any:
        from core.host_config import minimal_host_config

        return minimal_host_config("sandbox", MachinectlAuth.SUDO)

    _grant = staticmethod(_grant)

    def test_clean_no_violation(self, monkeypatch: Any) -> None:
        """No admin group AND a determinable no-grant from the sudoers policy."""
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr("core.setup.l2_host_prereqs._user_admin_groups", lambda u: [])
        monkeypatch.setattr(
            "core.setup.l2_host_prereqs._user_sudoers_grant",
            lambda u, *, self_query: self._grant(
                granted=False, nopasswd=False, determinable=True
            ),
        )
        v: list[str] = []
        m._audit_daemon_user_no_admin(self._hc(), v)
        assert v == []

    def test_in_admin_groups_warns_with_remediation(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr(
            "core.setup.l2_host_prereqs._user_admin_groups", lambda u: ["sudo", "wheel"]
        )
        monkeypatch.setattr(
            "core.setup.l2_host_prereqs._user_sudoers_grant",
            lambda u, *, self_query: self._grant(
                granted=False, nopasswd=False, determinable=True
            ),
        )
        v: list[str] = []
        m._audit_daemon_user_no_admin(self._hc(), v)
        assert len(v) == 1
        assert "sandbox" in v[0]
        assert "is a member of privilege-granting group(s) sudo, wheel" in v[0]
        assert "defeats the separate-user blast-radius reduction" in v[0]
        assert "gpasswd -d" in v[0]

    def test_nopasswd_policy_grant_warns(self, monkeypatch: Any) -> None:
        """No admin group, but the sudoers policy grants passwordless sudo (the
        cloud-init drop-in pattern that group-only detection missed)."""
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr("core.setup.l2_host_prereqs._user_admin_groups", lambda u: [])
        monkeypatch.setattr(
            "core.setup.l2_host_prereqs._user_sudoers_grant",
            lambda u, *, self_query: self._grant(
                granted=True, nopasswd=True, determinable=True
            ),
        )
        v: list[str] = []
        m._audit_daemon_user_no_admin(self._hc(), v)
        assert len(v) == 1
        assert "passwordless sudo by the sudoers policy" in v[0]
        assert "/etc/sudoers.d/" in v[0]
        assert "defeats the separate-user blast-radius reduction" in v[0]

    def test_password_gated_policy_grant_warns_not_nopasswd(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr("core.setup.l2_host_prereqs._user_admin_groups", lambda u: [])
        monkeypatch.setattr(
            "core.setup.l2_host_prereqs._user_sudoers_grant",
            lambda u, *, self_query: self._grant(
                granted=True, nopasswd=False, determinable=True
            ),
        )
        v: list[str] = []
        m._audit_daemon_user_no_admin(self._hc(), v)
        assert len(v) == 1
        assert "is granted sudo by the sudoers policy" in v[0]
        assert "passwordless" not in v[0]

    def test_group_and_policy_grant_both_named(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr("core.setup.l2_host_prereqs._user_admin_groups", lambda u: ["sudo"])
        monkeypatch.setattr(
            "core.setup.l2_host_prereqs._user_sudoers_grant",
            lambda u, *, self_query: self._grant(
                granted=True, nopasswd=True, determinable=True
            ),
        )
        v: list[str] = []
        m._audit_daemon_user_no_admin(self._hc(), v)
        assert len(v) == 1
        assert "is a member of privilege-granting group(s) sudo" in v[0]
        assert "passwordless sudo by the sudoers policy" in v[0]

    def test_indeterminate_policy_falls_back_to_group_only_note(self, monkeypatch: Any) -> None:
        """Non-root ``-U`` query → determinable=False → no false WARN; group-only
        + a note pointing at 'sudo sandbox doctor'."""
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr("core.setup.l2_host_prereqs._user_admin_groups", lambda u: [])
        monkeypatch.setattr(
            "core.setup.l2_host_prereqs._user_sudoers_grant",
            lambda u, *, self_query: self._grant(
                granted=False, nopasswd=False, determinable=False
            ),
        )
        v: list[str] = []
        m._audit_daemon_user_no_admin(self._hc(), v)
        assert len(v) == 1
        assert "sudoers-policy not checked" in v[0]
        assert "sudo sandbox doctor" in v[0]
        assert "defeats" not in v[0]

    def test_indeterminate_with_group_still_warns(self, monkeypatch: Any) -> None:
        """A group membership is a determinable grant even when the policy query
        is indeterminate — WARN on the group, no spurious note."""
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr("core.setup.l2_host_prereqs._user_admin_groups", lambda u: ["wheel"])
        monkeypatch.setattr(
            "core.setup.l2_host_prereqs._user_sudoers_grant",
            lambda u, *, self_query: self._grant(
                granted=False, nopasswd=False, determinable=False
            ),
        )
        v: list[str] = []
        m._audit_daemon_user_no_admin(self._hc(), v)
        assert len(v) == 1
        assert "is a member of privilege-granting group(s) wheel" in v[0]
        assert "sudoers-policy not checked" not in v[0]

    def test_reads_configured_daemon_user(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m
        from core.host_config import minimal_host_config

        seen: list[str] = []
        seen_policy: list[tuple[str, bool]] = []

        def _record(u: str) -> list[str]:
            seen.append(u)
            return []

        def _record_policy(u: str, *, self_query: bool) -> Any:
            seen_policy.append((u, self_query))
            return self._grant(granted=False, nopasswd=False, determinable=True)

        monkeypatch.setattr("core.setup.l2_host_prereqs._user_admin_groups", _record)
        monkeypatch.setattr(
            "core.setup.l2_host_prereqs._user_sudoers_grant", _record_policy
        )
        hc = minimal_host_config("dockerd-svc", MachinectlAuth.SUDO)
        m._audit_daemon_user_no_admin(hc, [])
        assert seen == ["dockerd-svc"]
        # Separate-user owner is a DIFFERENT user → self_query=False (needs root).
        assert seen_policy == [("dockerd-svc", False)]


class TestDaemonUserNoAdminInCheck:
    """The folded audit surfaces through the top-level separate-user verdict."""

    def test_separate_user_sudoer_daemon_user_warns(self, monkeypatch: Any) -> None:
        from core.doctor.checks.setup_invariants import check_setup_invariants

        monkeypatch.setattr("core.setup.l0_identity.resolve_operator", lambda: "alice")
        monkeypatch.setattr(f"{_MOD}._audit_reserved_dir", lambda v: None)
        monkeypatch.setattr(f"{_MOD}._audit_subid_and_group", lambda u, op, g, v: None)
        monkeypatch.setattr(f"{_MOD}._audit_machinectl_stability", lambda hc, v: None)
        monkeypatch.setattr(f"{_MOD}._audit_systemd_run_stability", lambda hc, t, v: None)
        monkeypatch.setattr(f"{_MOD}._audit_rule_body", lambda hc, op, t, v: None)
        monkeypatch.setattr(f"{_MOD}._audit_sudo_floor", lambda v: None)
        monkeypatch.setattr("core.setup.l2_host_prereqs._user_admin_groups", lambda u: ["sudo"])
        monkeypatch.setattr(
            "core.setup.l2_host_prereqs._user_sudoers_grant",
            lambda u, *, self_query: _grant(granted=False, nopasswd=False, determinable=True),
        )
        monkeypatch.setattr("pathlib.Path.read_text", lambda self: "rule-body")

        result = check_setup_invariants("sandbox", None)
        assert result.status == "warn"
        assert "privilege-granting group(s) sudo" in result.detail

    def test_separate_user_policy_grant_daemon_user_warns(self, monkeypatch: Any) -> None:
        """C-005 product gap: a daemon user with a NOPASSWD drop-in but NO admin
        group still surfaces as a WARN through the top-level verdict."""
        from core.doctor.checks.setup_invariants import check_setup_invariants

        monkeypatch.setattr("core.setup.l0_identity.resolve_operator", lambda: "alice")
        monkeypatch.setattr(f"{_MOD}._audit_reserved_dir", lambda v: None)
        monkeypatch.setattr(f"{_MOD}._audit_subid_and_group", lambda u, op, g, v: None)
        monkeypatch.setattr(f"{_MOD}._audit_machinectl_stability", lambda hc, v: None)
        monkeypatch.setattr(f"{_MOD}._audit_systemd_run_stability", lambda hc, t, v: None)
        monkeypatch.setattr(f"{_MOD}._audit_rule_body", lambda hc, op, t, v: None)
        monkeypatch.setattr(f"{_MOD}._audit_sudo_floor", lambda v: None)
        monkeypatch.setattr("core.setup.l2_host_prereqs._user_admin_groups", lambda u: [])
        monkeypatch.setattr(
            "core.setup.l2_host_prereqs._user_sudoers_grant",
            lambda u, *, self_query: _grant(granted=True, nopasswd=True, determinable=True),
        )
        monkeypatch.setattr("pathlib.Path.read_text", lambda self: "rule-body")

        result = check_setup_invariants("sandbox", None)
        assert result.status == "warn"
        assert "passwordless sudo by the sudoers policy" in result.detail

    def test_separate_user_clean_daemon_user_passes(self, monkeypatch: Any) -> None:
        from core.doctor.checks.setup_invariants import check_setup_invariants

        _patch_all_green(monkeypatch)
        monkeypatch.setattr("core.setup.l2_host_prereqs._user_admin_groups", lambda u: [])
        monkeypatch.setattr(
            "core.setup.l2_host_prereqs._user_sudoers_grant",
            lambda u, *, self_query: _grant(granted=False, nopasswd=False, determinable=True),
        )
        monkeypatch.setattr("pathlib.Path.read_text", lambda self: "rule-body")
        result = check_setup_invariants("sandbox", None)
        assert result.status == "pass"

    def test_operator_rootless_skips_daemon_user_audit(self, monkeypatch: Any) -> None:
        from core.doctor.checks.setup_invariants import check_setup_invariants
        from core.host_config import DockerExecutionMode

        monkeypatch.setattr("core.setup.l0_identity.resolve_operator", lambda: "alice")
        monkeypatch.setattr(f"{_MOD}._audit_reserved_dir", lambda v: None)
        monkeypatch.setattr(f"{_MOD}._audit_subid_and_group", lambda u, op, g, v: None)

        def must_not_run(*a: Any, **k: Any) -> None:
            raise AssertionError("_audit_daemon_user_no_admin must be skipped in operator-rootless")

        monkeypatch.setattr(f"{_MOD}._audit_daemon_user_no_admin", must_not_run)
        result = check_setup_invariants("sandbox", None, mode=DockerExecutionMode.OPERATOR_ROOTLESS)
        assert result.status == "pass"


class TestAuditMachinectlStability:
    """Post-C-009-D4 the operator drop-in is pipe-only (no machinectl path), so
    this audit checks ONLY that machinectl resolves uniquely on the sudoers
    ``secure_path`` basis — the resolver raising IS the check. There is no
    drop-in match (that moved to :class:`TestAuditSystemdRunStability`)."""

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
        m._audit_machinectl_stability(self._hc(), v)
        assert any("machinectl-path-stability" in x for x in v)

    def test_resolvable_no_violation(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr(
            "core.setup.l0_identity.resolve_machinectl_path", lambda hc: "/usr/bin/machinectl"
        )
        v: list[str] = []
        m._audit_machinectl_stability(self._hc(), v)
        assert v == []

    def test_no_false_warn_on_pipe_drop_in_without_machinectl(self, monkeypatch: Any) -> None:
        """Pre-fix RED (the C-009 false-WARN this fix removes): the operator pipe
        drop-in legitimately contains NO machinectl path (it pins the
        ``systemd-run`` launcher). A *resolvable* machinectl must therefore NOT
        WARN — the resolver succeeding is the only stability signal.

        Pre-fix verification protocol (CLAUDE.md): against the pre-fix
        ``_audit_machinectl_stability`` (which still did
        ``if resolved_machinectl not in drop_in_text: WARN(...)``) this assertion
        was RED on every C-009 host — the resolved ``/usr/bin/machinectl`` is
        absent from the pipe drop_in_text, so the stale match false-WARNed
        ``machinectl-path-stability: … not the path pinned …`` (observed: the
        violations list was non-empty, ``assert [] == []`` failed). Removing the
        stale drop-in match makes the resolvable-machinectl case clean, proving
        the test catches the false-WARN regression."""
        from core.doctor.checks import setup_invariants as m
        from core.setup.l3_sudoers import render_sudoers_rule

        monkeypatch.setattr(
            "core.setup.l0_identity.resolve_machinectl_path", lambda hc: "/usr/bin/machinectl"
        )
        # A REAL pipe drop-in: rendered against the systemd-run launcher, so it
        # carries no machinectl path at all.
        pipe_drop_in = render_sudoers_rule(
            "/usr/bin/systemd-run", "alice", socket.gethostname(), "sandbox"
        )
        assert "/usr/bin/machinectl" not in pipe_drop_in
        v: list[str] = []
        m._audit_machinectl_stability(self._hc(), v)
        assert v == []


class TestAuditSystemdRunStability:
    """Post-C-009-D4 the pipe ``Cmnd_Spec`` pins the systemd-run launcher; the
    re-resolved path must equal the one in the installed drop-in (the drift
    match that used to live on the machinectl audit moved here)."""

    def _hc(self) -> Any:
        from core.host_config import minimal_host_config

        return minimal_host_config("sandbox", MachinectlAuth.SUDO)

    def test_resolution_error(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m
        from core.setup.l0_identity import SystemdRunResolutionError

        def boom(hc: Any) -> str:
            raise SystemdRunResolutionError("zero launcher")

        monkeypatch.setattr("core.setup.l0_identity.resolve_systemd_run_path", boom)
        v: list[str] = []
        m._audit_systemd_run_stability(self._hc(), "text", v)
        assert any("pipe-launcher-path-stability" in x for x in v)

    def test_drift_path_not_in_drop_in(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr(
            "core.setup.l0_identity.resolve_systemd_run_path", lambda hc: "/usr/local/bin/systemd-run"
        )
        v: list[str] = []
        m._audit_systemd_run_stability(self._hc(), "rule pins /usr/bin/systemd-run", v)
        assert any("pipe-launcher-path-stability" in x for x in v)
        assert any("not the launcher pinned" in x for x in v)

    def test_drop_in_none_no_violation(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr(
            "core.setup.l0_identity.resolve_systemd_run_path", lambda hc: "/usr/bin/systemd-run"
        )
        v: list[str] = []
        m._audit_systemd_run_stability(self._hc(), None, v)
        assert v == []

    def test_path_present_no_violation(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr(
            "core.setup.l0_identity.resolve_systemd_run_path", lambda hc: "/usr/bin/systemd-run"
        )
        v: list[str] = []
        m._audit_systemd_run_stability(self._hc(), "x /usr/bin/systemd-run x", v)
        assert v == []


class TestAuditRuleBody:
    def _hc(self) -> Any:
        from core.host_config import minimal_host_config

        return minimal_host_config("sandbox", MachinectlAuth.SUDO)

    def test_canonical_body_no_violation(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr(
            "core.setup.l0_identity.resolve_systemd_run_path", lambda hc: "/usr/bin/systemd-run"
        )
        body = render_sudoers_rule("/usr/bin/systemd-run", "alice", socket.gethostname(), "sandbox")
        v: list[str] = []
        m._audit_rule_body(self._hc(), "alice", body, v)
        assert v == []

    def test_double_quote_flagged(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr(
            "core.setup.l0_identity.resolve_systemd_run_path", lambda hc: "/usr/bin/systemd-run"
        )
        body = render_sudoers_rule("/usr/bin/systemd-run", "alice", socket.gethostname(), "sandbox")
        tampered = body.replace("auth-probe", 'auth-"probe')
        v: list[str] = []
        m._audit_rule_body(self._hc(), "alice", tampered, v)
        assert any("double-quote in a Cmnd_Spec" in x for x in v)

    def test_op_enum_drift_detected(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr(
            "core.setup.l0_identity.resolve_systemd_run_path", lambda hc: "/usr/bin/systemd-run"
        )
        body = render_sudoers_rule("/usr/bin/systemd-run", "alice", socket.gethostname(), "sandbox")
        first_op = next(iter(Op)).value
        stale = "\n".join(
            line for line in body.splitlines() if f"{_DISPATCH_BINARY}\\ {first_op}" not in line
        )
        v: list[str] = []
        m._audit_rule_body(self._hc(), "alice", stale, v)
        assert any("installed op set != core.dispatch.Op" in x for x in v)
        assert any(first_op in x for x in v)

    def test_stale_host_missing_fwd_spec_warns_with_setup_remedy(self, monkeypatch: Any) -> None:
        """C-010 migration surface: a drop-in predating the ``fwd`` op WARNs.

        A host provisioned before ``Op.FWD`` existed has every other op spec but
        lacks the ``dispatch\\ fwd\\ *`` Cmnd_Spec — exactly the stale state that
        silently breaks separate-user SUDO attach (the ``fwd`` crossing is
        denied). The op-enum-drift audit must name ``fwd`` as missing and point
        at ``sudo sandbox setup`` (the named remedy — re-renders from the live
        enum). This is the doctor-side analogue of cli-attach's "Stale sudoers
        rule surfaces as doctor drift" scenario.
        """
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr(
            "core.setup.l0_identity.resolve_systemd_run_path", lambda hc: "/usr/bin/systemd-run"
        )
        body = render_sudoers_rule("/usr/bin/systemd-run", "alice", socket.gethostname(), "sandbox")
        # Drop ONLY the fwd spec (the streaming op's lone ``\\ *`` Cmnd_Spec).
        stale = "\n".join(
            line for line in body.splitlines() if f"{_DISPATCH_BINARY}\\ {Op.FWD.value}\\ *" not in line
        )
        assert f"{_DISPATCH_BINARY}\\ {Op.FWD.value}" not in stale
        v: list[str] = []
        m._audit_rule_body(self._hc(), "alice", stale, v)
        assert any("installed op set != core.dispatch.Op" in x for x in v)
        assert any(Op.FWD.value in x for x in v)
        assert any("sudo sandbox setup" in x for x in v)

    def test_resolution_error_skips_body(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m
        from core.setup.l0_identity import SystemdRunResolutionError

        def boom(hc: Any) -> str:
            raise SystemdRunResolutionError("zero launcher")

        monkeypatch.setattr("core.setup.l0_identity.resolve_systemd_run_path", boom)
        v: list[str] = []
        m._audit_rule_body(self._hc(), "alice", "irrelevant", v)
        assert v == []  # body comparison skipped; stability audit owns the resolution error

    def test_render_error_surfaced_as_warn(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr(
            "core.setup.l0_identity.resolve_systemd_run_path", lambda hc: "/usr/bin/systemd-run"
        )

        from core.setup.l3_sudoers import RuleRenderError

        def boom(*a: Any, **k: Any) -> str:
            raise RuleRenderError("bad op name")

        monkeypatch.setattr("core.setup.l3_sudoers.render_sudoers_rule", boom)
        v: list[str] = []
        m._audit_rule_body(self._hc(), "alice", "x", v)
        assert any("op-enum drift" in x for x in v)

    def test_body_differs_but_ops_match(self, monkeypatch: Any) -> None:
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr(
            "core.setup.l0_identity.resolve_systemd_run_path", lambda hc: "/usr/bin/systemd-run"
        )
        body = render_sudoers_rule("/usr/bin/systemd-run", "alice", socket.gethostname(), "sandbox")
        v: list[str] = []
        m._audit_rule_body(self._hc(), "alice", body + "\n# stray comment\n", v)
        assert any("installed body differs" in x for x in v)

    def test_body_audit_keys_on_systemd_run_not_machinectl_path(self, monkeypatch: Any) -> None:
        """Regression (signature-change orphan): the body audit renders against
        ``resolve_systemd_run_path``, NOT ``resolve_machinectl_path``.

        With the two resolvers mocked to DIFFERENT paths (every real host:
        ``/usr/bin/machinectl`` != ``/usr/bin/systemd-run``) the installed rule
        is the systemd-run-keyed pipe spec (L3 → ``resolve_systemd_run_path``).
        The audit must render its expected body from the SAME systemd-run path
        and report NO drift. The prior bug passed ``machinectl_path``
        positionally into the launcher slot, so the expected body was keyed on
        the machinectl abspath and the audit false-flagged drift on every host.
        The single-mocked-path test could not catch it (F-014: a mock can't see
        what it erases).
        """
        from core.doctor.checks import setup_invariants as m

        monkeypatch.setattr(
            "core.setup.l0_identity.resolve_machinectl_path", lambda hc: "/usr/bin/machinectl"
        )
        monkeypatch.setattr(
            "core.setup.l0_identity.resolve_systemd_run_path", lambda hc: "/usr/bin/systemd-run"
        )
        # The INSTALLED rule is the systemd-run-keyed pipe spec (what L3 writes).
        installed = render_sudoers_rule(
            "/usr/bin/systemd-run", "alice", socket.gethostname(), "sandbox"
        )
        v: list[str] = []
        m._audit_rule_body(self._hc(), "alice", installed, v)
        assert v == []  # audit keys on the systemd-run path → no false drift

        # A rule keyed on the MACHINECTL path as launcher (the buggy expected
        # body the orphaned positional arg produced) WOULD be flagged as drift.
        machinectl_keyed = render_sudoers_rule(
            "/usr/bin/machinectl", "alice", socket.gethostname(), "sandbox"
        )
        assert machinectl_keyed != installed
        v2: list[str] = []
        m._audit_rule_body(self._hc(), "alice", machinectl_keyed, v2)
        assert any("installed body differs" in x for x in v2)


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
        extracted = _extract_ops(body)
        assert extracted == {op.value for op in Op}
        # C-010: the canonical op set is the twelve-op enum, including the
        # streaming ``fwd`` op — the audit's expected set must contain it.
        assert "fwd" in extracted
        assert len(extracted) == 12


class TestWarnAggregation:
    def test_multiple_violations_joined(self, monkeypatch: Any) -> None:
        from core.doctor.checks.setup_invariants import check_setup_invariants

        monkeypatch.setattr("core.setup.l0_identity.resolve_operator", lambda: "alice")
        monkeypatch.setattr(f"{_MOD}._audit_reserved_dir", lambda v: v.append("A bad"))
        monkeypatch.setattr(f"{_MOD}._audit_subid_and_group", lambda u, op, g, v: v.append("B bad"))
        monkeypatch.setattr(f"{_MOD}._audit_daemon_user_no_admin", lambda hc, v: None)
        monkeypatch.setattr(f"{_MOD}._audit_machinectl_stability", lambda hc, v: None)
        monkeypatch.setattr(f"{_MOD}._audit_systemd_run_stability", lambda hc, t, v: None)
        monkeypatch.setattr(f"{_MOD}._audit_rule_body", lambda hc, op, t, v: None)
        monkeypatch.setattr(f"{_MOD}._audit_sudo_floor", lambda v: None)
        monkeypatch.setattr("pathlib.Path.read_text", lambda self: "x")
        result = check_setup_invariants("sandbox", None)
        assert result.status == "warn"
        assert "A bad" in result.detail
        assert "B bad" in result.detail
        assert "; " in result.detail
