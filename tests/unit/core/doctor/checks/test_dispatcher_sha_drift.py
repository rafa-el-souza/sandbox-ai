# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for core.doctor.checks.dispatcher_sha_drift.

Covers the PASS / SKIP / WARN verdicts. The check reuses setup L6.5's
single-source helpers (``_read_manifest`` / ``_file_sha512`` /
``_source_bundle_sha512``) — orchestrator decision 1 / F-011 — so the tests
patch those bound names on the check module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_MOD = "core.doctor.checks.dispatcher_sha_drift"


def test_module_exposes_single_check() -> None:
    from core.doctor.checks import dispatcher_sha_drift

    assert set(dispatcher_sha_drift.__all__) == {"check_dispatcher_sha_drift"}


def test_check_reuses_l65_single_source_helpers() -> None:
    """The source-bundle sha512 MUST be the L6.5 helper, not a re-implemented
    ``{go.mod, go.sum, main.go, vendor/**}`` subset (orchestrator dec. 1)."""
    from core.doctor.checks import dispatcher_sha_drift
    from core.setup import l65_dispatcher

    for name in ("_source_bundle_sha512", "_file_sha512", "_read_manifest", "_manifest_path"):
        assert getattr(dispatcher_sha_drift, name) is getattr(l65_dispatcher, name)


class TestCheckDispatcherShaDrift:
    def test_manifest_absent_reports_skip(self, monkeypatch: Any) -> None:
        from core.doctor.checks.dispatcher_sha_drift import check_dispatcher_sha_drift

        monkeypatch.setattr(f"{_MOD}._read_manifest", lambda: None)
        result = check_dispatcher_sha_drift("sandbox", None)
        assert result.status == "skip"
        assert "manifest absent" in result.detail
        assert result.remediation == "run 'sudo sandbox setup' to install the dispatcher"

    def test_binary_absent_reports_skip(self, monkeypatch: Any) -> None:
        from core.doctor.checks.dispatcher_sha_drift import check_dispatcher_sha_drift

        monkeypatch.setattr(
            f"{_MOD}._read_manifest",
            lambda: {"compiled_sha512": "x", "source_bundle_sha512": "y", "compile_timestamp": "t"},
        )
        monkeypatch.setattr(f"{_MOD}._file_sha512", lambda p: None)
        result = check_dispatcher_sha_drift("sandbox", None)
        assert result.status == "skip"
        assert "binary absent" in result.detail

    def test_both_match_reports_pass(self, monkeypatch: Any) -> None:
        from core.doctor.checks.dispatcher_sha_drift import check_dispatcher_sha_drift

        monkeypatch.setattr(
            f"{_MOD}._read_manifest",
            lambda: {
                "compiled_sha512": "a" * 128,
                "source_bundle_sha512": "b" * 128,
                "compile_timestamp": "2026-05-18T00:00:00+00:00",
            },
        )
        monkeypatch.setattr(f"{_MOD}._file_sha512", lambda p: "a" * 128)
        monkeypatch.setattr(f"{_MOD}._source_bundle_sha512", lambda: "b" * 128)
        result = check_dispatcher_sha_drift("sandbox", None)
        assert result.status == "pass"
        assert "match the manifest" in result.detail
        assert "2026-05-18T00:00:00+00:00" in result.detail

    def test_binary_sha_mismatch_reports_warn_tamper(self, monkeypatch: Any) -> None:
        from core.doctor.checks.dispatcher_sha_drift import check_dispatcher_sha_drift

        monkeypatch.setattr(
            f"{_MOD}._read_manifest",
            lambda: {"compiled_sha512": "a" * 128, "source_bundle_sha512": "b" * 128, "compile_timestamp": "t"},
        )
        # binary differs from recorded compiled sha; source still matches.
        monkeypatch.setattr(f"{_MOD}._file_sha512", lambda p: "9" * 128)
        monkeypatch.setattr(f"{_MOD}._source_bundle_sha512", lambda: "b" * 128)
        result = check_dispatcher_sha_drift("sandbox", None)
        assert result.status == "warn"
        assert "differs from setup's recorded sha" in result.detail
        assert "investigate tampering" in result.detail

    def test_source_sha_mismatch_reports_warn_wheel_upgrade(self, monkeypatch: Any) -> None:
        from core.doctor.checks.dispatcher_sha_drift import check_dispatcher_sha_drift

        monkeypatch.setattr(
            f"{_MOD}._read_manifest",
            lambda: {"compiled_sha512": "a" * 128, "source_bundle_sha512": "b" * 128, "compile_timestamp": "t"},
        )
        # binary matches; source bundle drifted (wheel upgrade).
        monkeypatch.setattr(f"{_MOD}._file_sha512", lambda p: "a" * 128)
        monkeypatch.setattr(f"{_MOD}._source_bundle_sha512", lambda: "7" * 128)
        result = check_dispatcher_sha_drift("sandbox", None)
        assert result.status == "warn"
        assert "older source bundle" in result.detail
        assert "wheel upgrade" in result.detail

    def test_manifest_path_surfaced_in_skip_detail(self, monkeypatch: Any, tmp_path: Path) -> None:
        from core.doctor.checks.dispatcher_sha_drift import check_dispatcher_sha_drift

        monkeypatch.setattr(f"{_MOD}._read_manifest", lambda: None)
        monkeypatch.setattr(f"{_MOD}._manifest_path", lambda: tmp_path / "m.json")
        result = check_dispatcher_sha_drift("sandbox", None)
        assert str(tmp_path / "m.json") in result.detail
