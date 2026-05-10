"""Tests for core.doctor.render.

Exhaustive renderer coverage lives in tests/unit/core/test_doctor.py
during the in-flight refactor; this mirror file currently asserts module
surface and import wiring. Per-renderer test classes will be migrated here
in the §11 finalization step.
"""

from __future__ import annotations


def test_module_exposes_render_results() -> None:
    from core.doctor import render

    assert set(render.__all__) == {"render_results"}


def test_public_re_export_resolves_to_render_module() -> None:
    import core.doctor as doctor_pkg
    from core.doctor import render

    assert doctor_pkg.render_results is render.render_results
