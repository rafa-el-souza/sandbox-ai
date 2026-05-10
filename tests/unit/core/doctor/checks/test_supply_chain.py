"""Tests for core.doctor.checks.supply_chain.

Per-check coverage lives in tests/unit/core/test_doctor.py during the
in-flight refactor; this mirror asserts module surface and import wiring.
"""

from __future__ import annotations


def test_module_exposes_image_digests_check() -> None:
    from core.doctor.checks import supply_chain

    assert set(supply_chain.__all__) == {"check_image_digests"}


def test_public_re_export_resolves_to_topic_module() -> None:
    import core.doctor as doctor_pkg
    from core.doctor.checks import supply_chain

    assert doctor_pkg.check_image_digests is supply_chain.check_image_digests
