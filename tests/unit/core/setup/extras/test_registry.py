"""Unit tests for :mod:`core.setup.extras.registry` — registry + sticky-opt-in.

These tests isolate the registry shape, the lazy loader, and the sticky-opt-in
predicate (design D11) from the concrete ``fapolicyd``/``aide`` modules by using
a STUB :class:`ExtraSpec`/``Phase`` — the contract those extra modules satisfy.
The modules ship in-tree; these tests stay deliberately decoupled from them
(the loader's absent-module behavior is keyed at a synthetic missing module,
not at a real extra).
"""

from __future__ import annotations

import pytest
from core.setup.extras.registry import EXTRAS, ExtraSpec, selected_extras


def test_registry_has_both_extras_with_spec_exact_paths() -> None:
    assert set(EXTRAS) == {"fapolicyd", "aide"}

    fap = EXTRAS["fapolicyd"]
    assert fap.name == "fapolicyd"
    assert fap.flag == "--enable-fapolicyd-integration"
    assert fap.dropin_path == "/etc/fapolicyd/trust.d/sandbox-ai.trust"
    assert fap.module == "core.setup.extras.fapolicyd"

    aide = EXTRAS["aide"]
    assert aide.name == "aide"
    assert aide.flag == "--enable-aide-integration"
    assert aide.dropin_path == "/etc/aide/aide.conf.d/sandbox-ai.conf"
    assert aide.module == "core.setup.extras.aide"


def test_extraspec_is_frozen() -> None:
    spec = EXTRAS["aide"]
    attr = "name"  # non-constant → avoids ruff B010 while still frozen-testing.
    with pytest.raises(AttributeError):
        setattr(spec, attr, "x")


def test_load_phase_is_lazy_and_raises_when_module_absent() -> None:
    # The lazy loader must fail loudly (ModuleNotFoundError), never silently,
    # when its target module is absent. Keyed at a deliberately-absent module
    # (the real fapolicyd/aide extras now exist post-F, so this invariant is
    # asserted against a synthetic missing target instead).
    spec = ExtraSpec(
        name="absent",
        flag="--enable-absent-integration",
        dropin_path="/etc/absent/sandbox-ai.conf",
        module="core.setup.extras._definitely_absent_module",
    )
    with pytest.raises(ModuleNotFoundError):
        spec.load_phase()


def test_load_phase_imports_module_and_returns_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Simulate Milestone F's module by registering a fake module in sys.modules
    # via a real importable target: point a stub spec at this test module's
    # PHASE sentinel.
    import sys
    import types

    fake = types.ModuleType("core.setup.extras._fake_extra")
    sentinel = object()
    vars(fake)["PHASE"] = sentinel
    monkeypatch.setitem(sys.modules, "core.setup.extras._fake_extra", fake)

    spec = ExtraSpec(
        name="fake",
        flag="--enable-fake-integration",
        dropin_path="/etc/fake/sandbox-ai.conf",
        module="core.setup.extras._fake_extra",
    )
    assert spec.load_phase() is sentinel


# ── selected_extras — sticky-opt-in predicate (design D11) ───────────────────


def test_selected_extras_none_when_no_flag_no_file() -> None:
    assert selected_extras({}, fs_check=lambda _p: False) == []


def test_selected_extras_flag_only() -> None:
    out = selected_extras(
        {"fapolicyd": True, "aide": False}, fs_check=lambda _p: False
    )
    assert out == ["fapolicyd"]


def test_selected_extras_sticky_file_only() -> None:
    # Flag NOT passed, but the owned drop-in exists on disk → sticky include.
    aide_path = "/etc/aide/aide.conf.d/sandbox-ai.conf"
    out = selected_extras(
        {"fapolicyd": False, "aide": False},
        fs_check=lambda p: p == aide_path,
    )
    assert out == ["aide"]


def test_selected_extras_flag_and_sticky_dedup_order() -> None:
    out = selected_extras(
        {"fapolicyd": True, "aide": True},
        fs_check=lambda _p: True,
    )
    # Registry order: fapolicyd then aide; each appears exactly once.
    assert out == ["fapolicyd", "aide"]


def test_selected_extras_missing_key_treated_false() -> None:
    # An absent mapping key is False (the flag was not passed).
    assert selected_extras({}, fs_check=lambda _p: False) == []


def test_selected_extras_default_fs_check_is_os_path_exists() -> None:
    # Default seam is os.path.exists; with no flags + nonexistent paths the
    # result is empty (the spec drop-in paths are absent in the test env).
    assert selected_extras({}) == []
