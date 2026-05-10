"""Tests for core.doctor.checks.per_user_tree.

The exhaustive per-check coverage lives in tests/unit/core/test_doctor.py
during the in-flight refactor; this mirror file currently asserts module
surface and import wiring. Per-check test classes will be migrated here
in the §11 finalization step of the refactor.
"""

from __future__ import annotations


def test_module_exposes_six_check_functions() -> None:
    from core.doctor.checks import per_user_tree

    expected = {
        "check_legacy_cwd_files",
        "check_legacy_registry_shape",
        "check_legacy_sandboxes_dir_detected",
        "check_legacy_workspace_in_user_project_root",
        "check_per_user_tree_exists",
        "check_per_user_tree_mode",
    }
    assert set(per_user_tree.__all__) == expected


def test_public_re_exports_resolve_to_topic_module() -> None:
    import core.doctor as doctor_pkg
    from core.doctor.checks import per_user_tree

    for name in per_user_tree.__all__:
        assert getattr(doctor_pkg, name) is getattr(per_user_tree, name)
