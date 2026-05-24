"""Tests for core.doctor.checks.runsc_pinned_match.

Covers the PASS / SKIP / WARN verdicts of ``check_runsc_pinned_match``, which
routes through ``core.binary_install.verify_only`` (read-only, no network).
"""

from __future__ import annotations

from typing import Any, Literal

from core.binary_install import DriftResult


def _verify(
    status: Literal["absent", "match", "drift"], installed: str | None, pinned: str
) -> DriftResult:
    return DriftResult(status=status, installed_sha=installed, pinned_sha=pinned)


def test_module_exposes_single_check() -> None:
    from core.doctor.checks import runsc_pinned_match

    assert set(runsc_pinned_match.__all__) == {"check_runsc_pinned_match"}


class TestCheckRunscPinnedMatch:
    def test_match_reports_pass(self, monkeypatch: Any) -> None:
        from core.doctor.checks.runsc_pinned_match import check_runsc_pinned_match

        monkeypatch.setattr(
            "core.binary_install.verify_only",
            lambda name, hc: _verify("match", "a" * 128, "a" * 128),
        )
        result = check_runsc_pinned_match("sandbox", None)
        assert result.status == "pass"
        assert "matches pinned sha" in result.detail
        assert "aaaaaaaaaaaaaaaa" in result.detail

    def test_absent_reports_skip(self, monkeypatch: Any) -> None:
        from core.doctor.checks.runsc_pinned_match import check_runsc_pinned_match

        monkeypatch.setattr(
            "core.binary_install.verify_only",
            lambda name, hc: _verify("absent", None, "b" * 128),
        )
        result = check_runsc_pinned_match("sandbox", None)
        assert result.status == "skip"
        assert "not installed" in result.detail
        assert result.remediation == "run 'sudo sandbox setup' to install runsc"

    def test_drift_reports_warn_with_both_shas(self, monkeypatch: Any) -> None:
        from core.doctor.checks.runsc_pinned_match import check_runsc_pinned_match

        monkeypatch.setattr(
            "core.binary_install.verify_only",
            lambda name, hc: _verify("drift", "c" * 128, "d" * 128),
        )
        result = check_runsc_pinned_match("sandbox", None)
        assert result.status == "warn"
        assert "cccccccccccccccc" in result.detail
        assert "dddddddddddddddd" in result.detail
        assert result.remediation is not None
        assert "--update-runsc" in result.remediation

    def test_drift_with_none_installed_sha_does_not_raise(self, monkeypatch: Any) -> None:
        from core.doctor.checks.runsc_pinned_match import check_runsc_pinned_match

        monkeypatch.setattr(
            "core.binary_install.verify_only",
            lambda name, hc: _verify("drift", None, "e" * 128),
        )
        result = check_runsc_pinned_match("sandbox", None)
        assert result.status == "warn"
        assert "eeeeeeeeeeeeeeee" in result.detail

    def test_match_with_none_installed_sha_does_not_raise(self, monkeypatch: Any) -> None:
        from core.doctor.checks.runsc_pinned_match import check_runsc_pinned_match

        monkeypatch.setattr(
            "core.binary_install.verify_only",
            lambda name, hc: _verify("match", None, "f" * 128),
        )
        result = check_runsc_pinned_match("sandbox", None)
        assert result.status == "pass"

    def test_auth_mode_threaded_into_host_config(self, monkeypatch: Any) -> None:
        from core.doctor.checks.runsc_pinned_match import check_runsc_pinned_match
        from core.host_config import MachinectlAuth

        captured: dict[str, Any] = {}

        def capture(name: str, hc: Any) -> DriftResult:
            captured["hc"] = hc
            return _verify("match", "a" * 128, "a" * 128)

        monkeypatch.setattr("core.binary_install.verify_only", capture)
        check_runsc_pinned_match("sandbox", None, auth_mode=MachinectlAuth.POLKIT)
        assert captured["hc"].host.docker_unprivileged_user == "sandbox"
        assert captured["hc"].host.machinectl_authentication == MachinectlAuth.POLKIT
