"""Tests for core.doctor.checks.repo_integrity.

Per-check coverage lives in tests/unit/core/test_doctor.py during the
in-flight refactor; this mirror asserts module surface and import wiring.
"""

from __future__ import annotations


def test_module_exposes_two_check_functions_and_unconditional_files() -> None:
    from core.doctor.checks import repo_integrity

    assert set(repo_integrity.__all__) == {
        "check_state_dir_writable",
        "check_tooling_plane",
    }
    assert isinstance(repo_integrity._UNCONDITIONAL_FILES, list)
    assert len(repo_integrity._UNCONDITIONAL_FILES) == 17
    assert callable(repo_integrity._resource_files)


def test_public_re_exports_resolve_to_topic_module() -> None:
    import core.doctor as doctor_pkg
    from core.doctor.checks import repo_integrity

    for name in repo_integrity.__all__:
        assert getattr(doctor_pkg, name) is getattr(repo_integrity, name)
    assert doctor_pkg._UNCONDITIONAL_FILES is repo_integrity._UNCONDITIONAL_FILES
    assert doctor_pkg._resource_files is repo_integrity._resource_files
