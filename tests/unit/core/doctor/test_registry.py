"""Tests for core.doctor.registry.

Exhaustive runner/topo coverage lives in tests/unit/core/test_doctor.py
during the in-flight refactor; this mirror file currently asserts module
surface and import wiring. Per-runner test classes will be migrated here
in the §11 finalization step.
"""

from __future__ import annotations


def test_module_exposes_expected_runner_surface() -> None:
    from core.doctor import registry

    assert set(registry.__all__) == {
        "build_check_registry",
        "run_check_subset",
        "run_checks",
        "topological_sort",
    }


def test_public_re_exports_resolve_to_registry_module() -> None:
    import core.doctor as doctor_pkg
    from core.doctor import registry

    for name in registry.__all__:
        assert getattr(doctor_pkg, name) is getattr(registry, name)
