"""Tests for core.doctor.checks.privilege_boundary.

The exhaustive per-check coverage lives in tests/unit/core/test_doctor.py
during the in-flight refactor; this mirror file currently asserts module
surface and import wiring. Per-check test classes will be migrated here
in the §11 finalization step of the refactor.
"""

from __future__ import annotations


def test_module_exposes_eleven_check_functions() -> None:
    from core.doctor.checks import privilege_boundary

    expected = {
        "check_compose_project_name_collision",
        "check_docker_available",
        "check_docker_rootless",
        "check_host_uds",
        "check_machinectl",
        "check_machinectl_reachable",
        "check_runsc_registered",
        "check_runsc_runtimeargs",
        "check_sudo",
        "check_systemd_machined",
        "check_user_exists",
    }
    assert expected.issubset(set(dir(privilege_boundary)))
    assert set(privilege_boundary.__all__) == expected


def test_public_re_exports_resolve_to_topic_module() -> None:
    import core.doctor as doctor_pkg
    from core.doctor.checks import privilege_boundary

    for name in privilege_boundary.__all__:
        assert getattr(doctor_pkg, name) is getattr(privilege_boundary, name)
