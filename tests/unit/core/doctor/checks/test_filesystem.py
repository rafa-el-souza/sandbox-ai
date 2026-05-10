"""Tests for core.doctor.checks.filesystem.

Per-check coverage lives in tests/unit/core/test_doctor.py during the
in-flight refactor; this mirror asserts module surface and import wiring.
"""

from __future__ import annotations


def test_module_exposes_three_check_functions_and_private_helpers() -> None:
    from core.doctor.checks import filesystem

    assert set(filesystem.__all__) == {
        "check_acl_support",
        "check_ancestor_traverse",
        "check_setfacl",
    }
    assert callable(filesystem._has_acl_exec)
    assert isinstance(filesystem._ACL_PROBE_FAILURES, tuple)


def test_public_re_exports_resolve_to_topic_module() -> None:
    import core.doctor as doctor_pkg
    from core.doctor.checks import filesystem

    for name in filesystem.__all__:
        assert getattr(doctor_pkg, name) is getattr(filesystem, name)
    assert doctor_pkg._has_acl_exec is filesystem._has_acl_exec
    assert doctor_pkg._ACL_PROBE_FAILURES is filesystem._ACL_PROBE_FAILURES
