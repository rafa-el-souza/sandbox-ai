# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for core.doctor.render.

Covers the Rich progressive-disclosure renderer for doctor results.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tests.unit.conftest import CapturedConsole


def test_module_exposes_render_results() -> None:
    from core.doctor import render

    assert set(render.__all__) == {"render_results"}


def test_public_re_export_resolves_to_render_module() -> None:
    import core.doctor as doctor_pkg
    from core.doctor import render

    assert doctor_pkg.render_results is render.render_results


class TestRichRenderer:
    def test_pass_marker(self, captured_console: CapturedConsole) -> None:
        from core.doctor import CheckResult, render_results

        results = [CheckResult(status="pass", name="Test Check", detail="ok")]
        render_results(results, console=captured_console.console)
        output = captured_console.plain_output
        assert "✓" in output
        assert "Test Check" in output

    def test_fail_marker_with_expansion(self, captured_console: CapturedConsole) -> None:
        from core.doctor import CheckResult, render_results

        results = [
            CheckResult(
                status="fail",
                name="Broken Check",
                detail="something broke",
                remediation="sudo fix-it",
                doc_ref="https://docs.example.com",
            )
        ]
        render_results(results, console=captured_console.console)
        output = captured_console.plain_output
        assert "✗" in output
        assert "Broken Check" in output
        assert "sudo fix-it" in output

    def test_skip_marker(self, captured_console: CapturedConsole) -> None:
        from core.doctor import CheckResult, render_results

        results = [CheckResult(status="skip", name="Skipped Check", detail="requires: root")]
        render_results(results, console=captured_console.console)
        output = captured_console.plain_output
        assert "⊘" in output
        assert "Skipped Check" in output

    def test_summary_line_format(self, captured_console: CapturedConsole) -> None:
        from core.doctor import CheckResult, render_results

        results = [
            CheckResult(status="pass", name="A", detail="ok"),
            CheckResult(status="fail", name="B", detail="bad", remediation="fix"),
            CheckResult(status="skip", name="C", detail="skip"),
        ]
        render_results(results, console=captured_console.console)
        output = captured_console.plain_output
        assert "1" in output
        assert "failed" in output.lower() or "fail" in output.lower()

    def test_category_grouping(self, captured_console: CapturedConsole) -> None:
        from core.doctor import CheckResult, render_results

        results = [
            CheckResult(status="pass", name="A", detail="ok", category="Group 1"),
            CheckResult(status="pass", name="B", detail="ok", category="Group 2"),
        ]
        render_results(results, console=captured_console.console)
        output = captured_console.plain_output
        assert "Group 1" in output
        assert "Group 2" in output


class TestRenderResultsDefaultConsole:
    def test_render_results_no_console(self) -> None:
        from core.doctor import CheckResult, render_results

        results = [CheckResult(status="pass", name="X", detail="ok")]
        # Should not raise — uses default RichConsole internally
        render_results(results)


class TestRenderResultsWithSubset:
    def test_render_results_accepts_subset(self, captured_console: CapturedConsole) -> None:
        from core.doctor import CheckResult, render_results

        results = [
            CheckResult(status="pass", name="setfacl binary", detail="ok", category="Filesystem"),
            CheckResult(status="pass", name="ACL support", detail="ok", category="Filesystem"),
        ]
        render_results(results, console=captured_console.console)
        output = captured_console.plain_output
        assert "Filesystem" in output
        assert "2/2 passed" in output


class TestWarnRendering:
    def test_check_result_accepts_warn(self) -> None:
        from core.doctor import CheckResult

        r = CheckResult(status="warn", name="advisory", detail="suboptimal", remediation="improve it")
        assert r.status == "warn"
        assert r.remediation == "improve it"

    def test_render_warn_only_yellow_summary(self, captured_console: CapturedConsole) -> None:
        from core.doctor import CheckResult, render_results

        results = [
            CheckResult(status="pass", name="A", detail="ok", category="Test"),
            CheckResult(status="warn", name="B", detail="advisory", remediation="fix", category="Test"),
        ]
        render_results(results, console=captured_console.console)
        output = captured_console.plain_output
        assert "1/2 passed" in output
        assert "1 warnings" in output
        assert "failed" not in output.lower()

    def test_render_mixed_pass_warn_fail_red_summary(self, captured_console: CapturedConsole) -> None:
        from core.doctor import CheckResult, render_results

        results = [
            CheckResult(status="pass", name="A", detail="ok", category="Test"),
            CheckResult(status="warn", name="B", detail="advisory", remediation="fix", category="Test"),
            CheckResult(status="fail", name="C", detail="broken", remediation="fix", category="Test"),
        ]
        render_results(results, console=captured_console.console)
        output = captured_console.plain_output
        assert "1/3 passed" in output
        assert "1 warnings" in output
        assert "1 failed" in output

    def test_render_warn_display(self, captured_console: CapturedConsole) -> None:
        from core.doctor import CheckResult, render_results

        results = [
            CheckResult(
                status="warn",
                name="Advisory Check",
                detail="something suboptimal",
                remediation="do better",
                category="Test",
            ),
        ]
        render_results(results, console=captured_console.console)
        output = captured_console.plain_output
        assert "⚠" in output
        assert "Advisory Check" in output
        assert "something suboptimal" in output
        assert "do better" in output
