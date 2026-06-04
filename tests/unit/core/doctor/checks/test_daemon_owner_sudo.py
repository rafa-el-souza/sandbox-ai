"""Tests for core.doctor.checks.daemon_owner_sudo (C-005 3.1 / design D4).

Operator-rootless-only WARN when the daemon-owning operator is a sudoer. The
owner is resolved via ``resolve_daemon_owner`` (the invoking operator in
operator-rootless, never the stale ``docker_unprivileged_user`` default — the
D7 convention guard). Admin-group membership is resolved through L2's
single-source ``_user_admin_groups`` (lazy import — the tests patch the origin
module attribute, not a bound name on the check module).
"""

from __future__ import annotations

from typing import Any

from core.host_config import DockerExecutionMode, MachinectlAuth

_MOD = "core.doctor.checks.daemon_owner_sudo"


def test_module_exposes_single_check() -> None:
    from core.doctor.checks import daemon_owner_sudo

    assert set(daemon_owner_sudo.__all__) == {"check_daemon_owner_sudo"}


def test_l2_imported_lazily_no_bound_name() -> None:
    """The ``core.setup.*`` import is function-local (cycle break): importing the
    check module must not bind ``l2_host_prereqs`` as a module attribute."""
    from core.doctor.checks import daemon_owner_sudo

    assert not hasattr(daemon_owner_sudo, "l2_host_prereqs")
    assert not hasattr(daemon_owner_sudo, "_user_admin_groups")


def test_sudoer_operator_warns_with_both_remedies(monkeypatch: Any) -> None:
    from core.doctor.checks.daemon_owner_sudo import check_daemon_owner_sudo

    monkeypatch.setattr("core.host_config.getpass.getuser", lambda: "alice")
    monkeypatch.setattr(
        "core.setup.l2_host_prereqs._user_admin_groups", lambda u: ["sudo"]
    )
    result = check_daemon_owner_sudo(
        "sandbox", None, mode=DockerExecutionMode.OPERATOR_ROOTLESS
    )
    assert result.status == "warn"
    assert "alice" in result.detail
    assert "sudoer" in result.detail
    assert "escalate to root" in result.detail
    assert result.remediation is not None
    # Both remedies named: (a) dedicated non-sudo operator, (b) separate-user.
    assert "non-sudo operator" in result.remediation
    assert "docker_execution_mode = separate-user" in result.remediation


def test_wheel_membership_warns(monkeypatch: Any) -> None:
    from core.doctor.checks.daemon_owner_sudo import check_daemon_owner_sudo

    monkeypatch.setattr("core.host_config.getpass.getuser", lambda: "bob")
    monkeypatch.setattr(
        "core.setup.l2_host_prereqs._user_admin_groups", lambda u: ["wheel", "admin"]
    )
    result = check_daemon_owner_sudo(
        "sandbox", None, mode=DockerExecutionMode.OPERATOR_ROOTLESS
    )
    assert result.status == "warn"
    assert "wheel, admin" in result.detail


def test_non_sudo_operator_passes(monkeypatch: Any) -> None:
    from core.doctor.checks.daemon_owner_sudo import check_daemon_owner_sudo

    monkeypatch.setattr("core.host_config.getpass.getuser", lambda: "carol")
    monkeypatch.setattr("core.setup.l2_host_prereqs._user_admin_groups", lambda u: [])
    result = check_daemon_owner_sudo(
        "sandbox", None, mode=DockerExecutionMode.OPERATOR_ROOTLESS
    )
    assert result.status == "pass"
    assert "carol" in result.detail
    assert "cannot escalate" in result.detail
    assert result.remediation is None


def test_owner_resolved_via_invoking_operator_not_dedicated_user(monkeypatch: Any) -> None:
    """D7: in operator-rootless the owner is the invoking operator (getpass), NOT
    the ``docker_unprivileged_user`` argument passed to the check."""
    from core.doctor.checks.daemon_owner_sudo import check_daemon_owner_sudo

    seen: list[str] = []

    def _record(u: str) -> list[str]:
        seen.append(u)
        return []

    monkeypatch.setattr("core.host_config.getpass.getuser", lambda: "alice")
    monkeypatch.setattr("core.setup.l2_host_prereqs._user_admin_groups", _record)
    # ``user`` arg is the dedicated-user name; it MUST be ignored for the owner.
    check_daemon_owner_sudo(
        "sandbox", None, mode=DockerExecutionMode.OPERATOR_ROOTLESS
    )
    assert seen == ["alice"]


def test_distro_arg_ignored(monkeypatch: Any) -> None:
    from core.doctor.checks.daemon_owner_sudo import check_daemon_owner_sudo

    monkeypatch.setattr("core.host_config.getpass.getuser", lambda: "alice")
    monkeypatch.setattr("core.setup.l2_host_prereqs._user_admin_groups", lambda u: [])
    result = check_daemon_owner_sudo(
        "sandbox", "debian", auth_mode=MachinectlAuth.POLKIT, mode=DockerExecutionMode.OPERATOR_ROOTLESS
    )
    assert result.status == "pass"
