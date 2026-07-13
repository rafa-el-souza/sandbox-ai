# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for test-suite layout — assert tests/unit/<pkg>/ mirrors src/<pkg>/.

Walks every Python module under ``src/<pkg>/`` and asserts a corresponding
test file exists at ``tests/unit/<pkg>/test_<module>.py``. Catches layout
drift mechanically: a contributor who adds ``src/core/foo.py`` without a
``tests/unit/core/test_foo.py`` fails this test.

Exceptions live in ``_LAYOUT_ALLOWLIST`` with a one-line reason. Every
entry is reviewable; widening the allowlist is one line and visible in
the diff.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _REPO_ROOT / "src"
_TESTS_ROOT = _REPO_ROOT / "tests" / "unit"

# Modules in src/ that intentionally lack a tests/unit/<pkg>/test_<module>.py
# mirror. Each entry pairs the source path with a one-line reason.
_LAYOUT_ALLOWLIST: dict[str, str] = {
    # ── src/cli ──
    "cli/main.py": (
        "Tested via tests/unit/cli/test_cli.py (historical name) plus per-aspect "
        "files (test_compose_up_plan.py, test_load_config_validation.py, "
        "test_markup_safety.py, test_workspace_*.py)."
    ),
    # ── src/core ──
    "core/exceptions.py": (
        "Trivial module — `SandboxExecutionError` class definition only, no logic to test."
    ),
    # ── src/templates ──
    # (no tracked .py files — Jinja2 templates only)
}

# Test files that intentionally don't mirror a src/<pkg>/<module>.py — usually
# because their target lives outside src/ (e.g., developer scripts).
_ORPHAN_ALLOWLIST: dict[str, str] = {
    "scripts/test_rotate_pins.py": "Tests scripts/rotate_pins.py (developer tool, not under src/).",
}


def _src_modules() -> list[Path]:
    """Return every ``.py`` under ``src/`` excluding ``__init__.py`` / ``__main__.py``."""
    out: list[Path] = []
    for path in _SRC_ROOT.rglob("*.py"):
        if path.name in ("__init__.py", "__main__.py"):
            continue
        out.append(path)
    return out


def _expected_test_path(src_module: Path) -> Path:
    """``src/<pkg>/<module>.py`` → ``tests/unit/<pkg>/test_<module>.py``."""
    rel = src_module.relative_to(_SRC_ROOT)
    return _TESTS_ROOT / rel.parent / f"test_{rel.name}"


def test_every_src_module_has_a_test_mirror() -> None:
    missing: list[tuple[str, Path]] = []
    for src_module in _src_modules():
        rel_key = str(src_module.relative_to(_SRC_ROOT))
        if rel_key in _LAYOUT_ALLOWLIST:
            continue
        expected = _expected_test_path(src_module)
        if not expected.exists():
            missing.append((rel_key, expected))

    if missing:
        details = "\n".join(
            f"  {rel} expected at {expected.relative_to(_REPO_ROOT)}"
            for rel, expected in missing
        )
        pytest.fail(
            f"{len(missing)} src module(s) lack a test mirror.\n{details}\n\n"
            "Fix: create the missing test file, OR add the module to "
            "tests/unit/test_layout.py::_LAYOUT_ALLOWLIST with a one-line reason."
        )


def test_no_orphan_test_files() -> None:
    """Every ``tests/unit/<pkg>/test_*.py`` must point at a real ``src/<pkg>/<module>.py``.

    Catches the reverse drift: a test file lingering after its source
    module was deleted or renamed. Top-level ``tests/unit/test_*.py``
    (meta-tests like this one) are exempt.
    """
    orphans: list[Path] = []
    for test_path in _TESTS_ROOT.rglob("test_*.py"):
        # Skip top-level meta-tests (they don't mirror a src module).
        if test_path.parent == _TESTS_ROOT:
            continue
        # Skip aspect-tests (test_<module>_<aspect>.py) — they piggyback on
        # the base module, which is verified by the forward-direction test.
        rel = test_path.relative_to(_TESTS_ROOT)
        pkg_path = rel.parent  # e.g., "core"
        # Strip leading "test_" and trailing ".py".
        stem = test_path.stem[len("test_") :]  # e.g., "ipam_locking"
        # Try shrinking suffixes ("ipam_locking" → "ipam") to find the source.
        candidates = [stem]
        parts = stem.rsplit("_", 1)
        while len(parts) == 2:
            candidates.append(parts[0])
            parts = parts[0].rsplit("_", 1)
        if not any((_SRC_ROOT / pkg_path / f"{c}.py").exists() for c in candidates):
            # Forward-allowlisted modules satisfy the reverse check too.
            forward_allowed = any(
                k.startswith(f"{pkg_path.as_posix()}/") for k in _LAYOUT_ALLOWLIST
            )
            if forward_allowed:
                continue
            # Explicit orphan allowlist (scripts/ targets, etc.).
            if rel.as_posix() in _ORPHAN_ALLOWLIST:
                continue
            orphans.append(test_path)

    if orphans:
        details = "\n".join(f"  {p.relative_to(_REPO_ROOT)}" for p in orphans)
        pytest.fail(
            f"{len(orphans)} orphan test file(s) — no matching src module.\n{details}"
        )
