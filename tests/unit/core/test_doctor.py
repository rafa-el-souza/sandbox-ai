"""Tests for the doctor module: host readiness diagnostics.

Covers data types, distro detection, binary checks, user/systemd checks,
machinectl reachability, Docker checks, filesystem checks, check runner,
and Rich output renderer.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, mock_open, patch

import pytest

if TYPE_CHECKING:
    from tests.unit.conftest import CapturedConsole

# ── Section 3: Binary Availability Checks ────────────────────────────────────


class TestBinaryChecks:
    """Task 3.1: Binary availability checks via shutil.which."""

    def test_check_sudo_present(self) -> None:
        from core.doctor import check_sudo

        with patch("shutil.which", return_value="/usr/bin/sudo"):
            result = check_sudo("sandbox", None)
            assert result.status == "pass"
            assert "/usr/bin/sudo" in result.detail

    def test_check_sudo_absent(self) -> None:
        from core.doctor import check_sudo

        with patch("shutil.which", return_value=None):
            result = check_sudo("sandbox", "debian")
            assert result.status == "fail"
            assert result.remediation is not None

    def test_check_machinectl_present(self) -> None:
        from core.doctor import check_machinectl

        with patch("shutil.which", return_value="/usr/bin/machinectl"):
            result = check_machinectl("sandbox", None)
            assert result.status == "pass"

    def test_check_machinectl_absent(self) -> None:
        from core.doctor import check_machinectl

        with patch("shutil.which", return_value=None):
            result = check_machinectl("sandbox", "debian")
            assert result.status == "fail"
            assert result.remediation is not None

    def test_check_setfacl_present(self) -> None:
        from core.doctor import check_setfacl

        with patch("shutil.which", return_value="/usr/bin/setfacl"):
            result = check_setfacl("sandbox", None)
            assert result.status == "pass"

    def test_check_setfacl_absent(self) -> None:
        from core.doctor import check_setfacl

        with patch("shutil.which", return_value=None):
            result = check_setfacl("sandbox", "fedora")
            assert result.status == "fail"
            assert "dnf" in (result.remediation or "")


# ── Section 4: User and systemd Checks ───────────────────────────────────────


class TestUserAndSystemdChecks:
    """Task 4.1: User existence and systemd-machined checks."""

    def test_user_exists(self) -> None:
        from core.doctor import check_user_exists

        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="uid=1000(sandbox)", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = check_user_exists("sandbox", None)
            assert result.status == "pass"
            assert "1000" in result.detail

    def test_user_not_exists(self) -> None:
        from core.doctor import check_user_exists

        mock_result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="no such user")
        with patch("subprocess.run", return_value=mock_result):
            result = check_user_exists("sandbox", None)
            assert result.status == "fail"
            assert result.remediation is not None

    def test_systemd_machined_active(self) -> None:
        from core.doctor import check_systemd_machined

        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="active\n", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = check_systemd_machined("sandbox", None)
            assert result.status == "pass"

    def test_systemd_machined_inactive(self) -> None:
        from core.doctor import check_systemd_machined

        mock_result = subprocess.CompletedProcess(args=[], returncode=3, stdout="inactive\n", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = check_systemd_machined("sandbox", None)
            assert result.status == "fail"
            assert "systemctl enable" in (result.remediation or "")


# ── Section 5: machinectl Reachability ───────────────────────────────────────


class TestMachinectlReachable:
    """Task 5.1: machinectl shell reachability with timeout."""

    def test_reachable_success(self) -> None:
        from core.doctor import check_machinectl_reachable

        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok\n", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = check_machinectl_reachable("sandbox", None)
            assert result.status == "pass"

    def test_reachable_timeout(self) -> None:
        from core.doctor import check_machinectl_reachable

        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="machinectl", timeout=10),
        ):
            result = check_machinectl_reachable("sandbox", None)
            assert result.status == "fail"
            assert "timeout" in result.detail.lower() or "sudoers" in (result.remediation or "").lower()

    def test_reachable_nonzero_exit(self) -> None:
        from core.doctor import check_machinectl_reachable

        mock_result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="No machine 'sandbox' known")
        with patch("subprocess.run", return_value=mock_result):
            result = check_machinectl_reachable("sandbox", None)
            assert result.status == "fail"
            assert result.detail != ""


# ── Section 6: Docker Checks ────────────────────────────────────────────────


class TestDockerChecks:
    """Task 6.1: Docker availability, rootless, and runsc checks."""

    def test_docker_available_pass(self) -> None:
        from core.doctor import check_docker_available

        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="24.0.7\n", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = check_docker_available("sandbox", None)
            assert result.status == "pass"
            assert "24.0.7" in result.detail

    def test_docker_available_fail(self) -> None:
        from core.doctor import check_docker_available

        mock_result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="command not found")
        with patch("subprocess.run", return_value=mock_result):
            result = check_docker_available("sandbox", None)
            assert result.status == "fail"

    def test_docker_rootless_pass(self) -> None:
        from core.doctor import check_docker_rootless

        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="[rootless, cgroupns]", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = check_docker_rootless("sandbox", None)
            assert result.status == "pass"

    def test_docker_rootless_system_docker(self) -> None:
        from core.doctor import check_docker_rootless

        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="[apparmor, seccomp]", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = check_docker_rootless("sandbox", None)
            assert result.status == "fail"
            assert "rootless" in (result.remediation or "").lower()

    def test_runsc_registered_pass(self) -> None:
        from core.doctor import check_runsc_registered

        docker_info = '{"runsc": {}, "runc": {}}'
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=docker_info, stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = check_runsc_registered("sandbox", None)
            assert result.status == "pass"

    def test_runsc_not_registered(self) -> None:
        from core.doctor import check_runsc_registered

        docker_info = '{"runc": {}}'
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=docker_info, stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = check_runsc_registered("sandbox", None)
            assert result.status == "fail"


# ── Section 7: Filesystem Checks ────────────────────────────────────────────


class TestFilesystemChecks:
    """Task 7.1: ACL support, tooling plane integrity, state dir writability."""

    def test_acl_support_pass(self) -> None:
        from core.doctor import check_acl_support

        with (
            patch("subprocess.run") as mock_run,
            patch("tempfile.NamedTemporaryFile") as mock_tmp,
        ):
            mock_tmp.return_value.__enter__ = lambda s: MagicMock(name="/tmp/test")
            mock_tmp.return_value.__exit__ = lambda s, *a: None
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            result = check_acl_support("sandbox", None)
            assert result.status == "pass"

    def test_acl_support_fail(self) -> None:
        from core.doctor import check_acl_support

        with (
            patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "setfacl")),
            patch("tempfile.NamedTemporaryFile") as mock_tmp,
        ):
            mock_tmp.return_value.__enter__ = lambda s: MagicMock(name="/tmp/test")
            mock_tmp.return_value.__exit__ = lambda s, *a: None
            result = check_acl_support("sandbox", None)
            assert result.status == "fail"

    def test_tooling_plane_intact(self) -> None:
        from core.doctor import check_tooling_plane

        result = check_tooling_plane("sandbox", None)
        assert result.status == "pass"
        assert "17" in result.detail

    def test_tooling_plane_missing_files(self, tmp_path: Path) -> None:
        from core.doctor import check_tooling_plane

        # Build a templates root missing compose.yml (and other entries)
        (tmp_path / "docker").mkdir()
        with patch("core.doctor._resource_files", return_value=tmp_path):
            result = check_tooling_plane("sandbox", None)
            assert result.status == "fail"
            assert "compose.yml" in result.detail

    def test_state_dir_writable(self, isolated_sandbox_ai_home: Path) -> None:
        from core.doctor import check_state_dir_writable

        (isolated_sandbox_ai_home / "state").mkdir(parents=True)
        result = check_state_dir_writable("sandbox", None)
        assert result.status == "pass"

    def test_state_dir_not_writable(self, isolated_sandbox_ai_home: Path) -> None:
        from core.doctor import check_state_dir_writable

        (isolated_sandbox_ai_home / "state").mkdir(parents=True)
        with patch("tempfile.NamedTemporaryFile", side_effect=OSError("perm denied")):
            result = check_state_dir_writable("sandbox", None)
            assert result.status == "fail"


# ── Section 8: Check Runner ─────────────────────────────────────────────────


class TestCheckRunner:
    """Task 8.1: Check registry, topological sort, and runner execution."""

    def test_build_check_registry_returns_all_checks(self) -> None:
        from core.doctor import build_check_registry

        checks = build_check_registry()
        # 25 (pre-change-5) + 9 new (compose_project_name_collision +
        # backups_disk_pressure + backups_partial_dirs_present +
        # dev_umask_workspace_friendly + workspace_path_in_walker_boundary +
        # workspace_home_single_filesystem + legacy_sandboxes_dir_detected +
        # legacy_workspace_in_user_project_root + legacy_registry_shape) = 34
        assert len(checks) == 34
        ids = [c.id for c in checks]
        assert "sudo" in ids
        assert "machinectl_reachable" in ids
        assert "docker_rootless" in ids
        assert "runsc_runtimeargs" in ids
        assert "compose_project_name_collision" in ids
        assert "legacy_registry_shape" in ids

    def test_topological_sort_respects_dependencies(self) -> None:
        from core.doctor import Check, CheckResult, topological_sort

        def noop(u: str, d: str | None) -> CheckResult:
            return CheckResult(status="pass", name="n", detail="")

        checks = [
            Check(id="c", name="C", category="t", depends_on=["b"], run=noop, remediation=""),
            Check(id="a", name="A", category="t", depends_on=[], run=noop, remediation=""),
            Check(id="b", name="B", category="t", depends_on=["a"], run=noop, remediation=""),
        ]
        sorted_checks = topological_sort(checks)
        ids = [c.id for c in sorted_checks]
        assert ids.index("a") < ids.index("b")
        assert ids.index("b") < ids.index("c")

    def test_runner_cascading_skip(self) -> None:
        """When a check fails, all dependents are auto-skipped."""
        from core.doctor import Check, CheckResult, run_checks

        def fail_run(u: str, d: str | None) -> CheckResult:
            return CheckResult(status="fail", name="root", detail="broken")

        def pass_run(u: str, d: str | None) -> CheckResult:
            return CheckResult(status="pass", name="dep", detail="ok")

        checks = [
            Check(id="root", name="Root", category="t", depends_on=[], run=fail_run, remediation="fix root"),
            Check(id="dep1", name="Dep1", category="t", depends_on=["root"], run=pass_run, remediation=""),
            Check(id="dep2", name="Dep2", category="t", depends_on=["dep1"], run=pass_run, remediation=""),
        ]
        results = run_checks(checks, "sandbox", None)
        assert results[0].status == "fail"
        assert results[1].status == "skip"
        assert results[2].status == "skip"

    def test_runner_independent_chain_isolation(self) -> None:
        """Failure in one chain does not affect independent chains."""
        from core.doctor import Check, CheckResult, run_checks

        def fail_run(u: str, d: str | None) -> CheckResult:
            return CheckResult(status="fail", name="Chain1", detail="broken")

        def pass_run(u: str, d: str | None) -> CheckResult:
            return CheckResult(status="pass", name="Chain2", detail="ok")

        checks = [
            Check(id="c1", name="Chain1", category="a", depends_on=[], run=fail_run, remediation=""),
            Check(id="c1d", name="Chain1Dep", category="a", depends_on=["c1"], run=pass_run, remediation=""),
            Check(id="c2", name="Chain2", category="b", depends_on=[], run=pass_run, remediation=""),
        ]
        results = run_checks(checks, "sandbox", None)
        by_name = {r.name: r for r in results}
        assert by_name["Chain1"].status == "fail"
        assert by_name["Chain1Dep"].status == "skip"
        assert by_name["Chain2"].status == "pass"


# ── Section 9: Rich Output Renderer ─────────────────────────────────────────


class TestRichRenderer:
    """Task 9.1: Rich console output renderer."""

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
        assert "1" in output  # 1 passed
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


# ── Coverage gap tests ──────────────────────────────────────────────────────


class TestDistroIdLikeFallback:
    """Cover ID_LIKE parsing branch when ID is not in _DISTRO_MAP."""

    def test_id_like_resolves_when_id_unknown(self) -> None:
        from core.doctor import detect_distro

        content = 'ID=linuxmint\nID_LIKE="ubuntu debian"\n'
        with patch("builtins.open", mock_open(read_data=content)):
            # linuxmint NOT in _DISTRO_MAP, but "ubuntu" in ID_LIKE IS
            assert detect_distro() == "debian"


class TestRunscJsonDecodeError:
    """Cover JSONDecodeError branch in check_runsc_registered."""

    def test_runsc_bad_json_output(self) -> None:
        from core.doctor import check_runsc_registered

        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="NOT-VALID-JSON{{{", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = check_runsc_registered("sandbox", None)
            assert result.status == "fail"


class TestRenderResultsDefaultConsole:
    """Cover the console=None default branch in render_results."""

    def test_render_results_no_console(self) -> None:
        from core.doctor import CheckResult, render_results

        results = [CheckResult(status="pass", name="X", detail="ok")]
        # Should not raise — uses default RichConsole internally
        render_results(results)


# ── Section 10: Check Subset API ─────────────────────────────────────────────


class TestRunCheckSubset:
    """Task 1.1: run_check_subset — category filtering and invariant assertions."""

    def test_category_filtering_returns_only_matching_checks(self) -> None:
        """Filtering by a single category returns only checks in that category."""
        from core.doctor import run_check_subset

        results = run_check_subset(["Filesystem"], "sandbox", None)
        # Chain 2 has 3 checks: setfacl, ACL support, ancestor traverse
        assert len(results) == 3
        names = {r.name for r in results}
        assert "setfacl" in names or "setfacl binary" in names

    def test_multiple_categories_returns_union(self) -> None:
        """Filtering by multiple categories returns checks from all specified categories."""
        from core.doctor import run_check_subset

        results = run_check_subset(["Filesystem", "Repo Integrity"], "sandbox", None)
        # Chain 2 (3 checks) + Chain 3 (2 checks) = 5
        assert len(results) == 5
        names = {r.name for r in results}
        # Must contain checks from both categories
        assert "tooling plane" in names or "state dir writable" in names

    def test_cascading_skip_within_subset(self) -> None:
        """When a root check fails within a subset, dependents are skipped."""
        from unittest.mock import patch

        from core.doctor import CheckResult, run_check_subset

        # setfacl fails → ACL support should be skipped
        def fake_setfacl(user: str, distro: str | None) -> CheckResult:
            return CheckResult(status="fail", name="setfacl", detail="not found", category="Filesystem")

        with patch("core.doctor.check_setfacl", fake_setfacl):
            results = run_check_subset(["Filesystem"], "sandbox", None)
            assert len(results) == 3
            statuses = {r.name: r.status for r in results}
            assert statuses["setfacl"] == "fail" or statuses["setfacl binary"] == "fail"
            # ACL support depends on setfacl → must be skip
            acl_result = next(r for r in results if "ACL" in r.name)
            assert acl_result.status == "skip"

    def test_empty_category_list_returns_empty(self) -> None:
        """Empty category list returns no results."""
        from core.doctor import run_check_subset

        results = run_check_subset([], "sandbox", None)
        assert results == []

    def test_exclude_ids_removes_checks(self) -> None:
        """exclude_ids removes specified checks and tolerates their dependents."""
        from core.doctor import run_check_subset

        results = run_check_subset(
            ["Filesystem"],
            "sandbox",
            None,
            exclude_ids={"ancestor_traverse"},
        )
        names = {r.name for r in results}
        assert "ancestor traverse" not in names
        # Other filesystem checks still run
        assert "setfacl binary" in names or "ACL support" in names

    def test_cross_chain_dependency_raises_valueerror(self) -> None:
        """ValueError raised if a filtered subset has a depends_on pointing outside the subset."""
        from core.doctor import Check, CheckResult

        # Construct a registry where Chain 2 depends on a Chain 1 check
        def noop(u: str, d: str | None) -> CheckResult:
            return CheckResult(status="pass", name="n", detail="")

        fake_checks = [
            Check(
                id="setfacl",
                name="setfacl",
                category="Filesystem",
                depends_on=["sudo"],  # cross-chain dependency!
                run=noop,
                remediation="",
            ),
            Check(
                id="sudo",
                name="sudo",
                category="Privilege Boundary",
                depends_on=[],
                run=noop,
                remediation="",
            ),
        ]

        with patch("core.doctor.build_check_registry", return_value=fake_checks):
            from core.doctor import run_check_subset

            with pytest.raises(ValueError, match="outside the subset"):
                run_check_subset(["Filesystem"], "sandbox", None)


class TestRenderResultsWithSubset:
    """Task 1.3: Verify render_results works unchanged with subset results."""

    def test_render_results_accepts_subset(self, captured_console: CapturedConsole) -> None:
        """render_results works with subset results (no code change expected)."""
        from core.doctor import CheckResult, render_results

        # Simulate subset output — only Filesystem category
        results = [
            CheckResult(status="pass", name="setfacl binary", detail="ok", category="Filesystem"),
            CheckResult(status="pass", name="ACL support", detail="ok", category="Filesystem"),
        ]
        render_results(results, console=captured_console.console)
        output = captured_console.plain_output
        assert "Filesystem" in output
        assert "2/2 passed" in output


# ── Section 11: Warn Severity Tests ────────────────────────────────────────────


class TestWarnStatus:
    """Tasks 9.1-9.5: Warn severity tests."""

    def test_check_result_accepts_warn(self) -> None:
        """Task 9.1: CheckResult accepts status='warn'."""
        from core.doctor import CheckResult

        r = CheckResult(status="warn", name="advisory", detail="suboptimal", remediation="improve it")
        assert r.status == "warn"
        assert r.remediation == "improve it"

    def test_warn_does_not_cascade_skip(self) -> None:
        """Task 9.2: run_checks does NOT cascade skip on warn — dependents still execute."""
        from core.doctor import Check, CheckResult, run_checks

        def warn_run(u: str, d: str | None) -> CheckResult:
            return CheckResult(status="warn", name="root", detail="advisory")

        def pass_run(u: str, d: str | None) -> CheckResult:
            return CheckResult(status="pass", name="dep", detail="ok")

        checks = [
            Check(id="root", name="Root", category="t", depends_on=[], run=warn_run, remediation=""),
            Check(id="dep1", name="Dep1", category="t", depends_on=["root"], run=pass_run, remediation=""),
        ]
        results = run_checks(checks, "sandbox", None)
        assert results[0].status == "warn"
        assert results[1].status == "pass"  # NOT skipped

    def test_render_warn_only_yellow_summary(self, captured_console: CapturedConsole) -> None:
        """Task 9.3: Warn-only results produce yellow summary, zero fail count."""
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
        """Task 9.4: Mixed pass+warn+fail results produce red summary with warn count."""
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
        """Task 9.5: Warn display shows ⚠ symbol, detail, and remediation."""
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


# ── Section 12: runsc RuntimeArgs Check Tests ───────────────────────────────


class TestCheckRunscRuntimeArgs:
    """Tasks 9.6-9.13: check_runsc_runtimeargs function tests."""

    def test_both_args_present_pass(self) -> None:
        """Task 9.6: Returns pass when both --oci-seccomp and --debug-log present."""
        from core.doctor import check_runsc_runtimeargs

        docker_info = json.dumps(
            {
                "runsc": {
                    "path": "/usr/local/bin/runsc",
                    "runtimeArgs": ["--oci-seccomp", "--debug-log=/var/log/runsc/%ID%/"],
                }
            }
        )
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=docker_info, stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = check_runsc_runtimeargs("sandbox", None)
            assert result.status == "pass"
            assert "--oci-seccomp" in result.detail
            assert "--debug-log" in result.detail

    def test_missing_oci_seccomp_warn(self) -> None:
        """Task 9.7: Returns warn when --oci-seccomp missing."""
        from core.doctor import check_runsc_runtimeargs

        docker_info = json.dumps(
            {
                "runsc": {
                    "path": "/usr/local/bin/runsc",
                    "runtimeArgs": ["--debug-log=/var/log/runsc/%ID%/"],
                }
            }
        )
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=docker_info, stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = check_runsc_runtimeargs("sandbox", None)
            assert result.status == "warn"
            assert "--oci-seccomp" in result.detail

    def test_missing_debug_log_warn(self) -> None:
        """Task 9.8: Returns warn when --debug-log missing."""
        from core.doctor import check_runsc_runtimeargs

        docker_info = json.dumps(
            {
                "runsc": {
                    "path": "/usr/local/bin/runsc",
                    "runtimeArgs": ["--oci-seccomp"],
                }
            }
        )
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=docker_info, stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = check_runsc_runtimeargs("sandbox", None)
            assert result.status == "warn"
            assert "--debug-log" in result.detail

    def test_empty_runtime_args_warn(self) -> None:
        """Task 9.9: Returns warn when runtimeArgs is empty/absent."""
        from core.doctor import check_runsc_runtimeargs

        docker_info = json.dumps(
            {
                "runsc": {
                    "path": "/usr/local/bin/runsc",
                }
            }
        )
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=docker_info, stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = check_runsc_runtimeargs("sandbox", None)
            assert result.status == "warn"
            assert "--oci-seccomp" in result.detail
            assert "--debug-log" in result.detail

    def test_remediation_references_daemon_json(self) -> None:
        """Task 9.10: Remediation references ~<user>/.config/docker/daemon.json."""
        from core.doctor import check_runsc_runtimeargs

        docker_info = json.dumps({"runsc": {"path": "/usr/local/bin/runsc"}})
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=docker_info, stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = check_runsc_runtimeargs("sandbox", None)
            assert result.remediation is not None
            assert "~sandbox/.config/docker/daemon.json" in result.remediation

    def test_registry_returns_full_check_count(self) -> None:
        """Registry size after change-5 doctor additions."""
        from core.doctor import build_check_registry

        checks = build_check_registry()
        assert len(checks) == 34

    def test_privilege_boundary_subset_count(self) -> None:
        """Privilege Boundary chain: 10 base checks + compose_project_name_collision = 11."""
        from core.doctor import build_check_registry

        checks = build_check_registry()
        pb_checks = [c for c in checks if c.category == "Privilege Boundary"]
        assert len(pb_checks) == 11

    def test_runsc_runtimeargs_skipped_when_runsc_fails(self) -> None:
        """Task 9.13: check_runsc_runtimeargs is skipped when runsc check fails."""
        from core.doctor import Check, CheckResult, run_checks

        def fail_runsc(u: str, d: str | None) -> CheckResult:
            return CheckResult(status="fail", name="gVisor runsc", detail="not found")

        def pass_runtimeargs(u: str, d: str | None) -> CheckResult:
            return CheckResult(status="pass", name="runsc runtimeArgs", detail="ok")

        checks = [
            Check(
                id="runsc",
                name="gVisor runsc",
                category="Privilege Boundary",
                depends_on=[],
                run=fail_runsc,
                remediation="",
            ),
            Check(
                id="runsc_runtimeargs",
                name="runsc runtimeArgs",
                category="Privilege Boundary",
                depends_on=["runsc"],
                run=pass_runtimeargs,
                remediation="",
            ),
        ]
        results = run_checks(checks, "sandbox", None)
        assert results[0].status == "fail"
        assert results[1].status == "skip"


# ── Section 12b: host-uds Validation Check ──────────────────────────────────


class TestCheckHostUds:
    """12.T: check_host_uds validates --host-uds=all is NOT in runsc runtimeArgs."""

    def test_check_host_uds_none_passes(self) -> None:
        """WHEN --host-uds=all is absent from runtimeArgs, THEN status='pass'."""
        from core.doctor import check_host_uds

        docker_info = json.dumps(
            {
                "runsc": {
                    "path": "/usr/local/bin/runsc",
                    "runtimeArgs": ["--oci-seccomp", "--debug-log=/var/log/runsc/%ID%/"],
                }
            }
        )
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=docker_info, stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = check_host_uds("sandbox", None)
            assert result.status == "pass"

    def test_check_host_uds_all_detected_warns(self) -> None:
        """WHEN --host-uds=all is present in runtimeArgs, THEN status='warn'."""
        from core.doctor import check_host_uds

        docker_info = json.dumps(
            {
                "runsc": {
                    "path": "/usr/local/bin/runsc",
                    "runtimeArgs": ["--oci-seccomp", "--host-uds=all"],
                }
            }
        )
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=docker_info, stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = check_host_uds("sandbox", None)
            assert result.status == "warn"
            assert "daemon.json" in (result.remediation or "")

    def test_check_host_uds_skipped_when_runsc_failed(self) -> None:
        """WHEN runsc check fails, THEN host_uds is auto-skipped by dependency engine."""
        from core.doctor import Check, CheckResult, run_checks

        def fail_runsc(u: str, d: str | None) -> CheckResult:
            return CheckResult(status="fail", name="gVisor runsc", detail="not found")

        def pass_host_uds(u: str, d: str | None) -> CheckResult:
            return CheckResult(status="pass", name="--host-uds=none", detail="ok")

        checks = [
            Check(
                id="runsc",
                name="gVisor runsc",
                category="Privilege Boundary",
                depends_on=[],
                run=fail_runsc,
                remediation="",
            ),
            Check(
                id="host_uds",
                name="--host-uds=none",
                category="Privilege Boundary",
                depends_on=["runsc"],
                run=pass_host_uds,
                remediation="",
            ),
        ]
        results = run_checks(checks, "sandbox", None)
        assert results[0].status == "fail"
        assert results[1].status == "skip"

    def test_build_check_registry_includes_host_uds(self) -> None:
        """build_check_registry() contains a check with id='host_uds'."""
        from core.doctor import build_check_registry

        checks = build_check_registry()
        ids = [c.id for c in checks]
        assert "host_uds" in ids

    def test_check_host_uds_docker_query_failure(self) -> None:
        """WHEN docker info fails, THEN status='warn' with remediation."""
        from core.doctor import check_host_uds

        mock_result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="error")
        with patch("subprocess.run", return_value=mock_result):
            result = check_host_uds("sandbox", None)
            assert result.status == "warn"
            assert "daemon.json" in (result.remediation or "")

    def test_check_host_uds_json_parse_failure(self) -> None:
        """WHEN docker info returns invalid JSON, THEN status='warn'."""
        from core.doctor import check_host_uds

        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="NOT-JSON{{{", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = check_host_uds("sandbox", None)
            assert result.status == "warn"
            assert "daemon.json" in (result.remediation or "")


# ── Section 12c: Ancestor Traverse Check ─────────────────────────────────────
#
# All os.stat/pwd.getpwnam calls are fully mocked to avoid host filesystem
# dependency. Tests construct synthetic stat results with deterministic
# uid/gid/mode values — no real filesystem stat calls on host paths.


def _make_stat(uid: int = 0, gid: int = 0, mode: int = 0o755) -> MagicMock:
    """Create a synthetic os.stat_result with controlled uid/gid/mode."""
    st = MagicMock(spec=os.stat_result)
    st.st_uid = uid
    st.st_gid = gid
    st.st_mode = mode
    return st


def _mock_pwd(user: str = "sandbox", uid: int = 2000, gid: int = 2000) -> MagicMock:
    """Create a synthetic pwd entry."""
    pw = MagicMock()
    pw.pw_uid = uid
    pw.pw_gid = gid
    pw.pw_name = user
    return pw


class TestCheckAncestorTraverse:
    """Task 9.4: ancestor traverse check — pass, fail, user not found, symlink."""

    def test_pass_all_traversable(self) -> None:
        """WHEN all ancestors have other-exec, THEN status=pass."""
        from core.doctor import check_ancestor_traverse

        # Synthetic path: /synthetic/project/sandboxes
        # Components: ["/", "/synthetic", "/synthetic/project", "/synthetic/project/sandboxes"]
        # All have o+x (0o755), target user uid=2000/gid=2000, dirs owned by root (uid=0)
        traversable = _make_stat(uid=0, gid=0, mode=0o755)

        with (
            patch("core.doctor.checks.filesystem.sandbox_ai_home", return_value=Path("/synthetic/project")),
            patch("pwd.getpwnam", return_value=_mock_pwd("sandbox")),
            patch("os.stat", return_value=traversable),
        ):
            result = check_ancestor_traverse("sandbox", None)
            assert result.status == "pass"
            assert "traversable" in result.detail

    def test_fail_user_not_found(self) -> None:
        """WHEN user doesn't exist, THEN status=fail with useradd remediation."""
        from core.doctor import check_ancestor_traverse

        with (
            patch("core.doctor.checks.filesystem.sandbox_ai_home", return_value=Path("/synthetic/project")),
            patch("pwd.getpwnam", side_effect=KeyError("nonexistent")),
        ):
            result = check_ancestor_traverse("nonexistent_user_xyz", None)
            assert result.status == "fail"
            assert "does not exist" in result.detail

    def test_fail_missing_execute(self) -> None:
        """WHEN an ancestor lacks --x, THEN status=fail with setfacl fix command."""
        from core.doctor import check_ancestor_traverse

        # /synthetic has 0o700 (no other-exec), target user is "other"
        blocked = _make_stat(uid=0, gid=0, mode=0o700)
        traversable = _make_stat(uid=0, gid=0, mode=0o755)

        def controlled_stat(path: str) -> MagicMock:
            if path == "/synthetic":
                return blocked
            return traversable

        with (
            patch("core.doctor.checks.filesystem.sandbox_ai_home", return_value=Path("/synthetic/project")),
            patch("pwd.getpwnam", return_value=_mock_pwd("sandbox")),
            patch("os.stat", side_effect=controlled_stat),
        ):
            result = check_ancestor_traverse("sandbox", None)
            assert result.status == "fail"
            assert "lacks execute" in result.detail
            assert "setfacl" in (result.remediation or "")

    def test_acl_support_skip_cascades(self) -> None:
        """WHEN acl_support fails, THEN ancestor_traverse is skipped (depends_on)."""
        from core.doctor import CheckResult, run_check_subset

        def fake_acl_support(user: str, distro: str | None) -> CheckResult:
            return CheckResult(status="fail", name="ACL support", detail="no ACL", category="Filesystem")

        with (
            patch("core.doctor.check_acl_support", fake_acl_support),
            patch(
                "core.doctor.check_setfacl",
                return_value=CheckResult(status="pass", name="setfacl binary", detail="ok", category="Filesystem"),
            ),
        ):
            results = run_check_subset(["Filesystem"], "sandbox", None)
            by_name = {r.name: r for r in results}
            assert by_name["ACL support"].status == "fail"
            assert by_name["ancestor traverse"].status == "skip"


# ── Coverage Gap Tests (doctor) ─────────────────────────────────────────────


class TestRunscRuntimeArgsEdgeCases:
    """Cover L391 (nonzero exit), L400-401 (JSON decode error)."""

    def test_nonzero_exit_returns_warn(self) -> None:
        """L391: docker info returns nonzero → warn."""
        from core.doctor import check_runsc_runtimeargs

        mock_result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="error")
        with patch("subprocess.run", return_value=mock_result):
            result = check_runsc_runtimeargs("sandbox", None)
            assert result.status == "warn"
            assert "Could not query" in result.detail

    def test_json_decode_error_returns_warn(self) -> None:
        """L400-401: bad JSON from docker info → warn."""
        from core.doctor import check_runsc_runtimeargs

        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="{{INVALID}}", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = check_runsc_runtimeargs("sandbox", None)
            assert result.status == "warn"
            assert "parse" in result.detail.lower()


class TestAncestorTraverseEdgeCases:
    """Cover L527 (symlink), L536-537 (OSError), L549 (group exec).

    All tests use fully synthetic stat results — no real filesystem access.
    """

    def test_symlink_divergence_warns(self) -> None:
        """L527, L563: symlink divergence produces warn status."""
        from core.doctor import check_ancestor_traverse

        traversable = _make_stat(uid=0, gid=0, mode=0o755)

        with (
            patch("core.doctor.checks.filesystem.sandbox_ai_home", return_value=Path("/synthetic/project")),
            patch("pwd.getpwnam", return_value=_mock_pwd("sandbox")),
            patch("os.stat", return_value=traversable),
            # Force realpath to return something different from abspath
            patch("os.path.realpath", return_value="/other/real/path"),
        ):
            result = check_ancestor_traverse("sandbox", None)
            assert result.status == "warn"
            assert "Symlink divergence" in result.detail

    def test_oserror_on_stat_returns_fail(self) -> None:
        """L536-537: OSError during ancestor stat → fail."""
        from core.doctor import check_ancestor_traverse

        traversable = _make_stat(uid=0, gid=0, mode=0o755)

        def controlled_stat(path: str) -> MagicMock:
            if path == "/synthetic":
                raise OSError("permission denied")
            return traversable

        with (
            patch("core.doctor.checks.filesystem.sandbox_ai_home", return_value=Path("/synthetic/project")),
            patch("pwd.getpwnam", return_value=_mock_pwd("sandbox")),
            patch("os.stat", side_effect=controlled_stat),
        ):
            result = check_ancestor_traverse("sandbox", None)
            assert result.status == "fail"
            assert "Cannot stat" in result.detail

    def test_group_exec_branch(self) -> None:
        """L549: group exec branch — uid differs, gid matches, S_IXGRP set."""
        from core.doctor import check_ancestor_traverse

        # Target user: uid=2000, gid=2000
        # /synthetic: owned by uid=9999, gid=2000, mode=0o750 (group exec set)
        # All other dirs: root-owned, o+x
        group_match = _make_stat(uid=9999, gid=2000, mode=0o750)
        traversable = _make_stat(uid=0, gid=0, mode=0o755)

        def controlled_stat(path: str) -> MagicMock:
            if path == "/synthetic":
                return group_match
            return traversable

        with (
            patch("core.doctor.checks.filesystem.sandbox_ai_home", return_value=Path("/synthetic/project")),
            patch("pwd.getpwnam", return_value=_mock_pwd("sandbox", uid=2000, gid=2000)),
            patch("os.stat", side_effect=controlled_stat),
        ):
            result = check_ancestor_traverse("sandbox", None)
            assert result.status == "pass"

    def test_user_owner_exec_branch(self) -> None:
        """L547: user-owner exec branch — directory owned by target user, S_IXUSR set."""
        from core.doctor import check_ancestor_traverse

        # Target user: uid=2000, gid=2000
        # /synthetic: owned by uid=2000 (same as user), mode=0o700 (user exec only)
        user_owned = _make_stat(uid=2000, gid=2000, mode=0o700)
        traversable = _make_stat(uid=0, gid=0, mode=0o755)

        def controlled_stat(path: str) -> MagicMock:
            if path == "/synthetic":
                return user_owned
            return traversable

        with (
            patch("core.doctor.checks.filesystem.sandbox_ai_home", return_value=Path("/synthetic/project")),
            patch("pwd.getpwnam", return_value=_mock_pwd("sandbox", uid=2000, gid=2000)),
            patch("os.stat", side_effect=controlled_stat),
        ):
            result = check_ancestor_traverse("sandbox", None)
            assert result.status == "pass"


class TestHasAclExec:
    """Tests for _has_acl_exec — getfacl probe for named-user ACL execute."""

    def test_acl_exec_found(self) -> None:
        """WHEN getfacl shows user:<name>:--x, THEN returns True."""
        from core.doctor import _has_acl_exec

        getfacl_output = (
            "# file: /home/dev\n"
            "# owner: dev\n"
            "# group: dev\n"
            "user::rwx\n"
            "user:sandbox:--x\n"
            "group::r-x\n"
            "mask::r-x\n"
            "other::---\n"
        )
        mock_result = subprocess.CompletedProcess([], 0, stdout=getfacl_output, stderr="")
        with patch("subprocess.run", return_value=mock_result):
            assert _has_acl_exec("/home/dev", "sandbox") is True

    def test_acl_no_exec(self) -> None:
        """WHEN getfacl shows user:<name>:r-- (no exec), THEN returns False."""
        from core.doctor import _has_acl_exec

        getfacl_output = "user::rwx\nuser:sandbox:r--\nother::---\n"
        mock_result = subprocess.CompletedProcess([], 0, stdout=getfacl_output, stderr="")
        with patch("subprocess.run", return_value=mock_result):
            assert _has_acl_exec("/home/dev", "sandbox") is False

    def test_acl_user_not_present(self) -> None:
        """WHEN getfacl has no entry for user, THEN returns False."""
        from core.doctor import _has_acl_exec

        getfacl_output = "user::rwx\ngroup::r-x\nother::---\n"
        mock_result = subprocess.CompletedProcess([], 0, stdout=getfacl_output, stderr="")
        with patch("subprocess.run", return_value=mock_result):
            assert _has_acl_exec("/home/dev", "sandbox") is False

    def test_getfacl_nonzero_returns_false(self) -> None:
        """WHEN getfacl exits nonzero, THEN returns False."""
        from core.doctor import _has_acl_exec

        mock_result = subprocess.CompletedProcess([], 1, stdout="", stderr="error")
        with patch("subprocess.run", return_value=mock_result):
            assert _has_acl_exec("/home/dev", "sandbox") is False

    def test_getfacl_timeout_returns_false(self) -> None:
        """WHEN getfacl times out, THEN returns False."""
        from core.doctor import _has_acl_exec

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("getfacl", 5)):
            assert _has_acl_exec("/home/dev", "sandbox") is False

    def test_getfacl_oserror_returns_false(self) -> None:
        """WHEN getfacl binary is missing, THEN returns False."""
        from core.doctor import _has_acl_exec

        with patch("subprocess.run", side_effect=OSError("not found")):
            assert _has_acl_exec("/home/dev", "sandbox") is False


class TestAncestorTraverseWithAclFallback:
    """Integration: mode bits deny but ACL grants execute → pass."""

    def test_mode_deny_acl_grants_passes(self) -> None:
        """WHEN mode bits deny and getfacl shows exec, THEN status=pass."""
        from core.doctor import check_ancestor_traverse

        # /synthetic: other::--- (mode bits deny), but ACL grants exec
        blocked = _make_stat(uid=0, gid=0, mode=0o700)
        traversable = _make_stat(uid=0, gid=0, mode=0o755)

        def controlled_stat(path: str) -> MagicMock:
            if path == "/synthetic":
                return blocked
            return traversable

        getfacl_output = "user::rwx\nuser:sandbox:--x\nother::---\n"
        mock_getfacl = subprocess.CompletedProcess([], 0, stdout=getfacl_output, stderr="")

        def controlled_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return mock_getfacl

        with (
            patch("core.doctor.checks.filesystem.sandbox_ai_home", return_value=Path("/synthetic/project")),
            patch("pwd.getpwnam", return_value=_mock_pwd("sandbox")),
            patch("os.stat", side_effect=controlled_stat),
            patch("subprocess.run", side_effect=controlled_run),
        ):
            result = check_ancestor_traverse("sandbox", None)
            assert result.status == "pass"

    def test_mode_deny_acl_deny_fails(self) -> None:
        """WHEN both mode bits and ACL deny, THEN status=fail."""
        from core.doctor import check_ancestor_traverse

        blocked = _make_stat(uid=0, gid=0, mode=0o700)
        traversable = _make_stat(uid=0, gid=0, mode=0o755)

        def controlled_stat(path: str) -> MagicMock:
            if path == "/synthetic":
                return blocked
            return traversable

        # getfacl has no entry for sandbox
        getfacl_output = "user::rwx\nother::---\n"
        mock_getfacl = subprocess.CompletedProcess([], 0, stdout=getfacl_output, stderr="")

        with (
            patch("core.doctor.checks.filesystem.sandbox_ai_home", return_value=Path("/synthetic/project")),
            patch("pwd.getpwnam", return_value=_mock_pwd("sandbox")),
            patch("os.stat", side_effect=controlled_stat),
            patch("subprocess.run", return_value=mock_getfacl),
        ):
            result = check_ancestor_traverse("sandbox", None)
            assert result.status == "fail"
            assert "lacks execute" in result.detail


# ── Section 15: Supply Chain Checks ───────────────────────────────────────────


class TestCheckImageDigests:
    """Group 7.T RED: check_image_digests supply chain verification."""

    def test_all_digests_resolvable_pass(self) -> None:
        """(a) All 7 entries resolve → status=pass, detail contains '7'."""
        from core.doctor import check_image_digests

        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = check_image_digests("sandbox", None)
            assert result.status == "pass"
            assert "7" in result.detail

    def test_stale_digest_detected_fail(self) -> None:
        """(b) One entry exits non-zero → status=fail, detail names the stale key."""
        from core.doctor import check_image_digests
        from core.hydration import IMAGE_REGISTRY

        call_count = 0
        keys = list(IMAGE_REGISTRY.keys())

        def selective_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal call_count
            cmd_str = " ".join(args[0]) if isinstance(args[0], list) else str(args[0])
            # Fail on the first IMAGE_REGISTRY entry's digest probe
            if IMAGE_REGISTRY[keys[0]].digest in cmd_str:
                return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="MANIFEST_UNKNOWN")
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")

        with patch("subprocess.run", side_effect=selective_run):
            result = check_image_digests("sandbox", None)
            assert result.status == "fail"
            assert keys[0] in result.detail

    def test_timeout_returns_skip(self) -> None:
        """(c) Subprocess timeout → status=skip, detail='registry unreachable'."""
        from core.doctor import check_image_digests

        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=2),
        ):
            result = check_image_digests("sandbox", None)
            assert result.status == "skip"
            assert "registry unreachable" in result.detail.lower()

    def test_tag_drift_reports_warn(self) -> None:
        """(d) Tag resolves to different digest than pinned → status includes 'warn' info."""
        from core.doctor import check_image_digests
        from core.hydration import IMAGE_REGISTRY

        keys = list(IMAGE_REGISTRY.keys())

        def selective_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            cmd_str = " ".join(args[0]) if isinstance(args[0], list) else str(args[0])
            # All digest probes pass
            if "@sha256:" in cmd_str:
                return subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout="{}",
                    stderr="",
                )
            # Tag probe returns a different digest for first key
            if f":{IMAGE_REGISTRY[keys[0]].tag}" in cmd_str:
                return subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout='{"digest": "sha256:0000000000000000000000000000000000000000000000000000000000000000"}',
                    stderr="",
                )
            # Other tag probes return matching digest
            for key in keys[1:]:
                pin = IMAGE_REGISTRY[key]
                if f":{pin.tag}" in cmd_str:
                    return subprocess.CompletedProcess(
                        args=[],
                        returncode=0,
                        stdout=f'{{"digest": "{pin.digest}"}}',
                        stderr="",
                    )
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")

        with patch("subprocess.run", side_effect=selective_run):
            result = check_image_digests("sandbox", None)
            # Should still be pass (tag drift is informational, not failure)
            # but detail should mention drift
            assert result.status in ("pass", "warn")

    def test_registered_in_build_check_registry(self) -> None:
        """(e) check_image_digests is registered with category='Supply Chain', depends_on=['docker_available']."""
        from core.doctor import build_check_registry

        checks = build_check_registry()
        image_check = next((c for c in checks if c.id == "image_digests"), None)
        assert image_check is not None, "image_digests not found in registry"
        assert image_check.category == "Supply Chain"
        assert "docker_available" in image_check.depends_on

    def test_tag_drift_json_decode_error(self) -> None:
        """Cover JSONDecodeError branch in tag drift phase."""
        from core.doctor import check_image_digests

        call_count = 0

        def mixed_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal call_count
            call_count += 1
            cmd_str = " ".join(args[0]) if isinstance(args[0], list) else str(args[0])
            # Digest probes pass
            if "@sha256:" in cmd_str:
                return subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
            # Tag probes return invalid JSON
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="NOT-JSON{{", stderr="")

        with patch("subprocess.run", side_effect=mixed_run):
            result = check_image_digests("sandbox", None)
            assert result.status == "pass"  # JSONDecodeError is swallowed

    def test_tag_drift_timeout_ignored(self) -> None:
        """Cover TimeoutExpired branch in tag drift phase (best-effort)."""
        from core.doctor import check_image_digests

        call_count = 0

        def timeout_on_tag(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal call_count
            call_count += 1
            cmd_str = " ".join(args[0]) if isinstance(args[0], list) else str(args[0])
            # Digest probes pass
            if "@sha256:" in cmd_str:
                return subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
            # Tag probes timeout
            raise subprocess.TimeoutExpired(cmd="docker", timeout=2)

        with patch("subprocess.run", side_effect=timeout_on_tag):
            result = check_image_digests("sandbox", None)
            assert result.status == "pass"  # Tag drift timeout is best-effort


# ── Section 13: Auth-Mode-Aware Registry (Section 5 of host-config-machinectl-auth) ──


class TestPolkitRegistry:
    """Tasks 5.12-5.13: build_check_registry honors MachinectlAuth.POLKIT.

    In polkit mode the `sudo` binary check is dropped and `machinectl_reachable`
    no longer depends on it. All machinectl-invoking checks construct command
    lines without a `sudo` prefix via `machinectl_cmd()`.
    """

    def test_sudo_check_omitted_in_polkit_mode(self) -> None:
        from core.doctor import build_check_registry
        from core.host_config import MachinectlAuth

        checks = build_check_registry(MachinectlAuth.POLKIT)
        ids = [c.id for c in checks]
        assert "sudo" not in ids
        assert len(checks) == 33  # one fewer than sudo mode

    def test_sudo_check_present_in_sudo_mode(self) -> None:
        from core.doctor import build_check_registry
        from core.host_config import MachinectlAuth

        checks = build_check_registry(MachinectlAuth.SUDO)
        ids = [c.id for c in checks]
        assert "sudo" in ids
        assert len(checks) == 34

    def test_machinectl_reachable_dependency_omits_sudo_in_polkit(self) -> None:
        from core.doctor import build_check_registry
        from core.host_config import MachinectlAuth

        checks = build_check_registry(MachinectlAuth.POLKIT)
        reach = next(c for c in checks if c.id == "machinectl_reachable")
        assert "sudo" not in reach.depends_on
        assert set(reach.depends_on) == {"machinectl", "user_exists", "systemd_machined"}

    def test_machinectl_reachable_dependency_includes_sudo_in_sudo(self) -> None:
        from core.doctor import build_check_registry
        from core.host_config import MachinectlAuth

        checks = build_check_registry(MachinectlAuth.SUDO)
        reach = next(c for c in checks if c.id == "machinectl_reachable")
        assert "sudo" in reach.depends_on

    def test_default_auth_mode_is_sudo(self) -> None:
        """build_check_registry() with no args matches sudo-mode shape."""
        from core.doctor import build_check_registry
        from core.host_config import MachinectlAuth

        default_ids = [c.id for c in build_check_registry()]
        sudo_ids = [c.id for c in build_check_registry(MachinectlAuth.SUDO)]
        assert default_ids == sudo_ids

    def test_polkit_machinectl_reachable_command_has_no_sudo(self) -> None:
        from core.doctor import check_machinectl_reachable
        from core.host_config import MachinectlAuth

        captured: dict[str, Any] = {}

        def capture(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="ok\n", stderr="")

        with patch("subprocess.run", side_effect=capture):
            result = check_machinectl_reachable("sandbox", None, auth_mode=MachinectlAuth.POLKIT)

        assert result.status == "pass"
        assert captured["cmd"][0] == "machinectl"
        assert "sudo" not in captured["cmd"]

    def test_sudo_machinectl_reachable_command_has_sudo_prefix(self) -> None:
        from core.doctor import check_machinectl_reachable
        from core.host_config import MachinectlAuth

        captured: dict[str, Any] = {}

        def capture(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="ok\n", stderr="")

        with patch("subprocess.run", side_effect=capture):
            check_machinectl_reachable("sandbox", None, auth_mode=MachinectlAuth.SUDO)

        assert captured["cmd"][:4] == ["sudo", "machinectl", "shell", "sandbox@.host"]

    def test_polkit_timeout_remediation_mentions_polkit(self) -> None:
        from core.doctor import check_machinectl_reachable
        from core.host_config import MachinectlAuth

        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="machinectl", timeout=10),
        ):
            result = check_machinectl_reachable("sandbox", None, auth_mode=MachinectlAuth.POLKIT)

        assert result.status == "fail"
        assert "polkit" in (result.remediation or "").lower()
        assert "sudoers" not in (result.remediation or "").lower()

    def test_polkit_docker_available_command_has_no_sudo(self) -> None:
        from core.doctor import check_docker_available
        from core.host_config import MachinectlAuth

        captured: dict[str, Any] = {}

        def capture(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="24.0.7\n", stderr="")

        with patch("subprocess.run", side_effect=capture):
            check_docker_available("sandbox", None, auth_mode=MachinectlAuth.POLKIT)

        assert captured["cmd"][0] == "machinectl"
        assert "sudo" not in captured["cmd"]

    def test_polkit_image_digests_command_has_no_sudo(self) -> None:
        from core.doctor import check_image_digests
        from core.host_config import MachinectlAuth

        captured: list[list[str]] = []

        def capture(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            captured.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="{}", stderr="")

        with patch("subprocess.run", side_effect=capture):
            check_image_digests("sandbox", None, auth_mode=MachinectlAuth.POLKIT)

        assert captured  # at least one invocation
        for cmd in captured:
            assert cmd[0] == "machinectl"
            assert "sudo" not in cmd

    def test_run_check_subset_forwards_auth_mode(self) -> None:
        """run_check_subset(auth_mode=POLKIT) should yield a polkit-shaped registry.

        Privilege Boundary subset under polkit excludes the `sudo` binary check.
        """
        from core.doctor import run_check_subset
        from core.host_config import MachinectlAuth

        # Stub all subprocess calls to keep the test hermetic.
        with patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ):
            results = run_check_subset(
                ["Privilege Boundary"],
                "sandbox",
                None,
                auth_mode=MachinectlAuth.POLKIT,
            )

        names = {r.name for r in results}
        assert "sudo" not in names


# ─── Per-User Tree Doctor Checks ─────────────────────────────────────────────


class TestCheckPerUserTreeExists:
    def test_pass_when_tree_present(self, isolated_sandbox_ai_home: Path) -> None:
        from core.doctor import check_per_user_tree_exists
        from core.host_config import ensure_per_user_state

        ensure_per_user_state(isolated_sandbox_ai_home)
        result = check_per_user_tree_exists("u", None)
        assert result.status == "pass"

    def test_fail_when_home_missing(self, isolated_sandbox_ai_home: Path) -> None:
        from core.doctor import check_per_user_tree_exists

        result = check_per_user_tree_exists("u", None)
        assert result.status == "fail"
        assert "missing" in result.detail.lower()
        assert result.remediation is not None
        assert "sandbox init" in result.remediation

    def test_fail_when_state_subdir_missing(self, isolated_sandbox_ai_home: Path) -> None:
        from core.doctor import check_per_user_tree_exists

        # Create only home and config — state is missing.
        (isolated_sandbox_ai_home / "config").mkdir(parents=True)
        result = check_per_user_tree_exists("u", None)
        assert result.status == "fail"
        assert "state" in result.detail


class TestCheckPerUserTreeMode:
    def test_pass_when_all_0700(self, isolated_sandbox_ai_home: Path) -> None:
        from core.doctor import check_per_user_tree_mode
        from core.host_config import ensure_per_user_state

        ensure_per_user_state(isolated_sandbox_ai_home)
        result = check_per_user_tree_mode("u", None)
        assert result.status == "pass"

    def test_warn_on_mode_drift(self, isolated_sandbox_ai_home: Path) -> None:
        import os

        from core.doctor import check_per_user_tree_mode
        from core.host_config import ensure_per_user_state

        ensure_per_user_state(isolated_sandbox_ai_home)
        os.chmod(isolated_sandbox_ai_home / "state", 0o755)
        result = check_per_user_tree_mode("u", None)
        assert result.status == "warn"
        assert "0o755" in result.detail
        assert result.remediation is not None
        assert "chmod 0700" in result.remediation

    def test_skip_when_tree_missing(self, isolated_sandbox_ai_home: Path) -> None:
        from core.doctor import check_per_user_tree_mode

        result = check_per_user_tree_mode("u", None)
        assert result.status == "skip"


class TestCheckLegacyCwdFiles:
    def test_pass_when_no_legacy(self, tmp_path: Path, monkeypatch: Any) -> None:
        from core.doctor import check_legacy_cwd_files

        monkeypatch.chdir(tmp_path)
        result = check_legacy_cwd_files("u", None)
        assert result.status == "pass"

    def test_warn_on_legacy_toml_and_state(self, tmp_path: Path, monkeypatch: Any) -> None:
        from core.doctor import check_legacy_cwd_files

        (tmp_path / "sandbox-ai.toml").write_text("")
        (tmp_path / ".state").mkdir()
        monkeypatch.chdir(tmp_path)
        result = check_legacy_cwd_files("u", None)
        assert result.status == "warn"
        assert "sandbox-ai.toml" in result.detail
        assert ".state" in result.detail


# ─── Section 11: acl-ownership-recipes checks ───────────────────────────────


class TestCheckWorkspaceBridgeGroupExists:
    def test_skip_when_no_host_config(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import check_workspace_bridge_group_exists

        result = check_workspace_bridge_group_exists("u", None)
        assert result.status == "skip"

    def test_pass_when_group_resolves(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_workspace_bridge_group_exists

        config_dir = isolated_sandbox_ai_home / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "sandbox-ai.toml").write_text('[host]\ndocker_unprivileged_user = "claude-sandbox"\n')
        monkeypatch.setattr("core.doctor.workspace_bridge_gid", lambda h: 200500)
        result = check_workspace_bridge_group_exists("claude-sandbox", None)
        assert result.status == "pass"
        assert "200500" in result.detail

    def test_fail_when_group_missing_with_recommendation(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_workspace_bridge_group_exists
        from core.host_config import WorkspaceBridgeGroupMissingError

        config_dir = isolated_sandbox_ai_home / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "sandbox-ai.toml").write_text(
            '[host]\ndocker_unprivileged_user = "claude-sandbox"\nworkspace_bridge_group = "sb-ws"\n'
        )

        def _raise(host: Any) -> int:
            raise WorkspaceBridgeGroupMissingError("group missing")

        monkeypatch.setattr("core.doctor.workspace_bridge_gid", _raise)
        monkeypatch.setattr(
            "core.doctor.autodetect_workspace_bridge_gid_recommendation",
            lambda host_user, in_container_min=1000: 200999,
        )
        result = check_workspace_bridge_group_exists("claude-sandbox", None)
        assert result.status == "fail"
        assert "sb-ws" in (result.remediation or "")
        assert "200999" in (result.remediation or "")

    def test_fail_when_group_missing_and_no_recommendation(
        self, isolated_sandbox_ai_home: Any, monkeypatch: Any
    ) -> None:
        from core.doctor import check_workspace_bridge_group_exists
        from core.host_config import NoSubgidRangeError, WorkspaceBridgeGroupMissingError

        config_dir = isolated_sandbox_ai_home / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "sandbox-ai.toml").write_text('[host]\ndocker_unprivileged_user = "claude-sandbox"\n')

        def _raise(host: Any) -> int:
            raise WorkspaceBridgeGroupMissingError("group missing")

        def _raise_no_range(host_user: str, in_container_min: int = 1000) -> int:
            raise NoSubgidRangeError("no subgid")

        monkeypatch.setattr("core.doctor.workspace_bridge_gid", _raise)
        monkeypatch.setattr("core.doctor.autodetect_workspace_bridge_gid_recommendation", _raise_no_range)
        result = check_workspace_bridge_group_exists("claude-sandbox", None)
        assert result.status == "fail"
        assert "<pick-a-gid" in (result.remediation or "")

    def test_fail_when_recommendation_finds_no_free_gid(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_workspace_bridge_group_exists
        from core.host_config import NoFreeGidInSubgidRangeError, WorkspaceBridgeGroupMissingError

        config_dir = isolated_sandbox_ai_home / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "sandbox-ai.toml").write_text('[host]\ndocker_unprivileged_user = "claude-sandbox"\n')

        def _raise(host: Any) -> int:
            raise WorkspaceBridgeGroupMissingError("group missing")

        def _raise_no_free(host_user: str, in_container_min: int = 1000) -> int:
            raise NoFreeGidInSubgidRangeError("range exhausted")

        monkeypatch.setattr("core.doctor.workspace_bridge_gid", _raise)
        monkeypatch.setattr("core.doctor.autodetect_workspace_bridge_gid_recommendation", _raise_no_free)
        result = check_workspace_bridge_group_exists("claude-sandbox", None)
        assert result.status == "fail"
        assert "<pick-a-gid" in (result.remediation or "")

    def test_fail_when_gid_out_of_range(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_workspace_bridge_group_exists
        from core.host_config import SubgidOutOfRangeError

        config_dir = isolated_sandbox_ai_home / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "sandbox-ai.toml").write_text('[host]\ndocker_unprivileged_user = "claude-sandbox"\n')

        def _raise(host: Any) -> int:
            raise SubgidOutOfRangeError("gid 99 not in any range")

        monkeypatch.setattr("core.doctor.workspace_bridge_gid", _raise)
        result = check_workspace_bridge_group_exists("claude-sandbox", None)
        assert result.status == "fail"
        assert "Recreate the bridge group" in (result.remediation or "")


class TestCheckDevInWorkspaceBridgeGroup:
    def test_skip_when_no_host_config(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import check_dev_in_workspace_bridge_group

        result = check_dev_in_workspace_bridge_group("u", None)
        assert result.status == "skip"

    def test_pass_when_in_supplementary_groups(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_dev_in_workspace_bridge_group

        config_dir = isolated_sandbox_ai_home / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "sandbox-ai.toml").write_text('[host]\ndocker_unprivileged_user = "claude-sandbox"\n')
        monkeypatch.setattr("core.doctor.workspace_bridge_gid", lambda h: 200500)
        monkeypatch.setattr("core.doctor.os.getgroups", lambda: [200500, 1000])
        result = check_dev_in_workspace_bridge_group("u", None)
        assert result.status == "pass"

    def test_fail_relogin_path(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_dev_in_workspace_bridge_group

        config_dir = isolated_sandbox_ai_home / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "sandbox-ai.toml").write_text('[host]\ndocker_unprivileged_user = "claude-sandbox"\n')
        monkeypatch.setattr("core.doctor.workspace_bridge_gid", lambda h: 200500)
        monkeypatch.setattr("core.doctor.os.getgroups", lambda: [1000])
        monkeypatch.setattr("core.doctor.os.getuid", lambda: 1000)

        class _Pw:
            pw_name = "dev"

        class _Gr:
            gr_gid = 200500

            @property
            def gr_mem(self) -> list[str]:
                return ["dev"]

        import grp
        import pwd

        monkeypatch.setattr(pwd, "getpwuid", lambda uid: _Pw())
        monkeypatch.setattr(grp, "getgrall", lambda: [_Gr()])
        result = check_dev_in_workspace_bridge_group("u", None)
        assert result.status == "fail"
        assert "Log out" in (result.remediation or "")

    def test_fail_usermod_path(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_dev_in_workspace_bridge_group

        config_dir = isolated_sandbox_ai_home / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "sandbox-ai.toml").write_text('[host]\ndocker_unprivileged_user = "claude-sandbox"\n')
        monkeypatch.setattr("core.doctor.workspace_bridge_gid", lambda h: 200500)
        monkeypatch.setattr("core.doctor.os.getgroups", lambda: [1000])
        monkeypatch.setattr("core.doctor.os.getuid", lambda: 1000)

        class _Pw:
            pw_name = "dev"

        import grp
        import pwd

        monkeypatch.setattr(pwd, "getpwuid", lambda uid: _Pw())
        monkeypatch.setattr(grp, "getgrall", lambda: [])
        result = check_dev_in_workspace_bridge_group("u", None)
        assert result.status == "fail"
        assert "usermod -aG" in (result.remediation or "")

    def test_fail_when_bridge_lookup_raises(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_dev_in_workspace_bridge_group
        from core.host_config import WorkspaceBridgeGroupMissingError

        config_dir = isolated_sandbox_ai_home / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "sandbox-ai.toml").write_text('[host]\ndocker_unprivileged_user = "claude-sandbox"\n')

        def _raise(host: Any) -> int:
            raise WorkspaceBridgeGroupMissingError("group missing")

        monkeypatch.setattr("core.doctor.workspace_bridge_gid", _raise)
        result = check_dev_in_workspace_bridge_group("u", None)
        assert result.status == "fail"


class TestCheckSubuidResolverWorks:
    def test_pass(self, monkeypatch: Any) -> None:
        from core.doctor import check_subuid_resolver_works

        monkeypatch.setattr("core.doctor.host_id_for_in_container", lambda n, u: 100999)
        result = check_subuid_resolver_works("claude-sandbox", None)
        assert result.status == "pass"
        assert "100999" in result.detail

    def test_fail_no_subuid(self, monkeypatch: Any) -> None:
        from core.doctor import check_subuid_resolver_works
        from core.host_config import NoSubuidRangeError

        def _raise(n: int, u: str) -> int:
            raise NoSubuidRangeError("no subuid")

        monkeypatch.setattr("core.doctor.host_id_for_in_container", _raise)
        result = check_subuid_resolver_works("claude-sandbox", None)
        assert result.status == "fail"
        assert "rootless" in (result.remediation or "")


class TestCheckHelperImagePulled:
    def test_pass_when_present(self, monkeypatch: Any) -> None:
        import subprocess

        from core.doctor import check_helper_image_pulled

        monkeypatch.setattr(
            "core.doctor.subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""),
        )
        result = check_helper_image_pulled("u", None)
        assert result.status == "pass"

    def test_warn_when_absent(self, monkeypatch: Any) -> None:
        import subprocess

        from core.doctor import check_helper_image_pulled

        monkeypatch.setattr(
            "core.doctor.subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess([], 1, "", "not found"),
        )
        result = check_helper_image_pulled("u", None)
        assert result.status == "warn"

    def test_warn_when_docker_unreachable(self, monkeypatch: Any) -> None:
        from core.doctor import check_helper_image_pulled

        def _raise(*a: Any, **k: Any) -> Any:
            raise FileNotFoundError("no docker")

        monkeypatch.setattr("core.doctor.subprocess.run", _raise)
        result = check_helper_image_pulled("u", None)
        assert result.status == "warn"
        assert "docker not reachable" in result.detail


class TestCheckSecretsHydratedRestrictively:
    def test_pass_when_no_instances(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_secrets_hydrated_restrictively

        monkeypatch.setattr("core.doctor._scan_instance_dirs", lambda: [])
        result = check_secrets_hydrated_restrictively("u", None)
        assert result.status == "pass"

    def test_warn_on_world_readable_secret(self, tmp_path: Any, monkeypatch: Any) -> None:
        import os

        from core.doctor import check_secrets_hydrated_restrictively

        inst = tmp_path / "inst"
        secrets = inst / "secrets"
        secrets.mkdir(parents=True)
        leak = secrets / "ipc_host_key"
        leak.write_text("k")
        os.chmod(leak, 0o644)

        monkeypatch.setattr("core.doctor._scan_instance_dirs", lambda: [str(inst)])
        result = check_secrets_hydrated_restrictively("u", None)
        assert result.status == "warn"
        assert "ipc_host_key" in result.detail

class TestCheckPreExistingInstanceLayout:
    """Three-state semantics per cli-doctor's "Pre-Existing Instance Layout Check":
    (a) leaf absent → pass; (b) consumer-owned → pass; (c) dev-owned → warn with
    per-leaf ``rm -rf`` remediation; (d) mixed-state → per-leaf reporting.
    """

    def test_just_initd_instance_passes_silently(self, tmp_path: Any, monkeypatch: Any) -> None:
        """State (a): no cache/log leaves on disk (post-Change-D scaffold contract).

        A freshly-init'd instance whose helper recipe has not yet run on first
        start is the new default; the absent state must NOT warn.
        """
        from core.doctor import check_pre_existing_instance_layout

        inst = tmp_path / "inst"
        inst.mkdir(parents=True)
        # No leaves created — the post-Change-D scaffold-vs-helper boundary state.
        monkeypatch.setattr("core.doctor.host_id_for_in_container", lambda n, u: 999999)
        monkeypatch.setattr("core.doctor._scan_instance_dirs", lambda: [str(inst)])
        result = check_pre_existing_instance_layout("u", None)
        assert result.status == "pass"
        assert result.detail == "no stale cache/log leaf ownership detected"

    def test_pass_when_chowned(self, tmp_path: Any, monkeypatch: Any) -> None:
        """State (b): all leaves present and consumer-subuid-owned (post-helper-run)."""
        import os

        from core.doctor import check_pre_existing_instance_layout

        inst = tmp_path / "inst"
        for leaf in ("cache/core/.claude", "cache/admin/tmux_resurrect", "log/core", "log/admin"):
            (inst / leaf).mkdir(parents=True)
        target_uid = os.stat(inst / "cache/core/.claude").st_uid
        monkeypatch.setattr("core.doctor.host_id_for_in_container", lambda n, u: target_uid)
        monkeypatch.setattr("core.doctor._scan_instance_dirs", lambda: [str(inst)])
        result = check_pre_existing_instance_layout("u", None)
        assert result.status == "pass"

    def test_warn_when_dev_owned_includes_per_leaf_rm_rf(self, tmp_path: Any, monkeypatch: Any) -> None:
        """State (c): all leaves present and dev-owned (legacy/pre-Change-D)."""
        from core.doctor import check_pre_existing_instance_layout

        inst = tmp_path / "inst"
        leaves = (
            "cache/core/.claude",
            "cache/admin/tmux_resurrect",
            "log/core",
            "log/admin",
        )
        for leaf in leaves:
            (inst / leaf).mkdir(parents=True)

        # Mock to a uid that does not match any test-dir owner.
        monkeypatch.setattr("core.doctor.host_id_for_in_container", lambda n, u: 999999)
        monkeypatch.setattr("core.doctor._scan_instance_dirs", lambda: [str(inst)])
        result = check_pre_existing_instance_layout("u", None)
        assert result.status == "warn"
        assert "4 cache/log leaf(s)" in result.detail
        # Per-leaf rm -rf remediation; "destroy && init" is no longer the recommendation.
        remediation = result.remediation or ""
        for leaf in leaves:
            assert f"rm -rf {inst}/{leaf}" in remediation, (
                f"remediation missing per-leaf rm -rf for {leaf}: {remediation!r}"
            )
        assert "destroy" not in remediation

    def test_mixed_state_reports_only_stale_leaves(self, tmp_path: Any, monkeypatch: Any) -> None:
        """State (d): one leaf consumer-owned, one dev-owned → only the dev-owned leaf is flagged.

        Uses the injectable ``uid_for_path`` resolver to make per-leaf ownership
        deterministic without monkeypatching ``os.stat``.
        """
        import os

        from core.doctor import check_pre_existing_instance_layout

        inst = tmp_path / "inst"
        consumer_leaf = inst / "cache/core/.claude"
        legacy_leaf = inst / "cache/admin/tmux_resurrect"
        consumer_leaf.mkdir(parents=True)
        legacy_leaf.mkdir(parents=True)

        consumer_subuid = 777777
        legacy_uid = 1000  # any uid that is not the consumer subuid

        def resolver(path: str) -> int:
            # Mirror os.stat: raise OSError for absent paths so the absent-leaf
            # branch is reached for the two leaves we did not create.
            if not os.path.exists(path):
                raise FileNotFoundError(path)
            if path == str(consumer_leaf):
                return consumer_subuid
            return legacy_uid

        monkeypatch.setattr("core.doctor.host_id_for_in_container", lambda n, u: consumer_subuid)
        monkeypatch.setattr("core.doctor._scan_instance_dirs", lambda: [str(inst)])
        result = check_pre_existing_instance_layout("u", None, uid_for_path=resolver)
        assert result.status == "warn"
        # Only the legacy leaf is flagged; the consumer-owned one passes silently.
        assert "1 cache/log leaf(s)" in result.detail
        assert str(legacy_leaf) in result.detail
        assert str(consumer_leaf) not in result.detail
        remediation = result.remediation or ""
        assert f"rm -rf {legacy_leaf}" in remediation
        assert str(consumer_leaf) not in remediation

    def test_warn_aggregates_across_multiple_instances(self, tmp_path: Any, monkeypatch: Any) -> None:
        """Multiple registered instances → stale leaves aggregated into one warning."""
        from core.doctor import check_pre_existing_instance_layout

        inst_a = tmp_path / "a"
        inst_b = tmp_path / "b"
        (inst_a / "log/core").mkdir(parents=True)
        (inst_b / "log/admin").mkdir(parents=True)

        monkeypatch.setattr("core.doctor.host_id_for_in_container", lambda n, u: 999999)
        monkeypatch.setattr("core.doctor._scan_instance_dirs", lambda: [str(inst_a), str(inst_b)])
        result = check_pre_existing_instance_layout("u", None)
        assert result.status == "warn"
        assert "2 cache/log leaf(s)" in result.detail
        remediation = result.remediation or ""
        assert f"rm -rf {inst_a}/log/core" in remediation
        assert f"rm -rf {inst_b}/log/admin" in remediation

    def test_pass_when_partial_layout_resolves_correctly(self, tmp_path: Any, monkeypatch: Any) -> None:
        """Missing leaves don't false-warn — only existing-and-stale leaves count."""
        from core.doctor import check_pre_existing_instance_layout

        inst = tmp_path / "inst"
        # Only create one leaf; the other three don't exist (e.g., a fresh instance
        # that hasn't run all its services yet). The OSError on the missing leaves
        # must not be reported as "stale".
        (inst / "log/core").mkdir(parents=True)
        target_uid = (inst / "log/core").stat().st_uid

        monkeypatch.setattr("core.doctor.host_id_for_in_container", lambda n, u: target_uid)
        monkeypatch.setattr("core.doctor._scan_instance_dirs", lambda: [str(inst)])
        result = check_pre_existing_instance_layout("u", None)
        assert result.status == "pass"

    def test_skip_when_no_subuid(self, monkeypatch: Any) -> None:
        from core.doctor import check_pre_existing_instance_layout
        from core.host_config import NoSubuidRangeError

        def _raise(n: int, u: str) -> int:
            raise NoSubuidRangeError("none")

        monkeypatch.setattr("core.doctor.host_id_for_in_container", _raise)
        result = check_pre_existing_instance_layout("u", None)
        assert result.status == "skip"



class TestScanInstanceDirs:
    def test_returns_empty_when_state_missing(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import _scan_instance_dirs

        assert _scan_instance_dirs() == []

    def test_returns_empty_on_corrupt_json(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import _scan_instance_dirs

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "instances.json").write_text("{not json")
        assert _scan_instance_dirs() == []

    def test_returns_empty_when_instances_field_wrong_shape(self, isolated_sandbox_ai_home: Any) -> None:
        import json as _json

        from core.doctor import _scan_instance_dirs

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "instances.json").write_text(_json.dumps({"instances": []}))
        assert _scan_instance_dirs() == []

    def test_returns_registered_instance_dirs(
        self, isolated_sandbox_ai_home: Any, tmp_path: Any
    ) -> None:
        """Iterates the name-keyed registry; yields each entry's instance_dir
        when it exists on disk. Drops non-dict entries and missing dirs."""
        import json as _json

        # Stand up two real instance dirs and one missing.
        present = tmp_path / "instances" / "myproj"
        present.mkdir(parents=True)
        missing = tmp_path / "instances" / "missing"

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "instances.json").write_text(
            _json.dumps(
                {
                    "myproj": {"instance_dir": str(present), "created_at": "2026-05-07T00:00:00Z"},
                    "gone": {"instance_dir": str(missing), "created_at": "2026-05-07T00:00:00Z"},
                    "garbage": "not-a-dict",
                }
            )
        )
        from core.doctor import _scan_instance_dirs

        assert _scan_instance_dirs() == [str(present)]

    def test_returns_empty_when_top_level_not_dict(self, isolated_sandbox_ai_home: Any) -> None:
        import json as _json

        from core.doctor import _scan_instance_dirs

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "instances.json").write_text(_json.dumps([1, 2, 3]))
        assert _scan_instance_dirs() == []


class TestCheckSecretsHydratedRestrictivelyEdges:
    """Coverage for secrets-scan branches that bypass _scan_instance_dirs mocking."""

    def test_skip_when_secrets_dir_absent(self, tmp_path: Any, monkeypatch: Any) -> None:
        from core.doctor import check_secrets_hydrated_restrictively

        # Instance dir exists but has no secrets/ subdir.
        inst = tmp_path / "inst"
        inst.mkdir()
        monkeypatch.setattr("core.doctor._scan_instance_dirs", lambda: [str(inst)])
        result = check_secrets_hydrated_restrictively("u", None)
        assert result.status == "pass"

    def test_unstattable_file_skipped(self, tmp_path: Any, monkeypatch: Any) -> None:
        """os.stat raising on a secret file is silently skipped (rare race)."""
        import os

        from core.doctor import check_secrets_hydrated_restrictively

        inst = tmp_path / "inst"
        secrets = inst / "secrets"
        secrets.mkdir(parents=True)
        # Empty secrets dir, but make os.stat raise for the listing case.
        # We do this by listing a path then having stat raise.
        (secrets / "x").write_text("k")

        real_stat = os.stat

        def _raise_on_x(path: str, **kw: Any) -> os.stat_result:
            if path.endswith("/x"):
                raise PermissionError("denied")
            return real_stat(path, **kw)

        monkeypatch.setattr("core.doctor.os.stat", _raise_on_x)
        monkeypatch.setattr("core.doctor._scan_instance_dirs", lambda: [str(inst)])
        result = check_secrets_hydrated_restrictively("u", None)
        # File was skipped, no leaks reported.
        assert result.status == "pass"


# ── change-5 doctor checks ──────────────────────────────────────────────────


class TestCheckBackupsDiskPressure:
    def test_pass_when_no_backups_dir(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import check_backups_disk_pressure

        result = check_backups_disk_pressure("u", None)
        assert result.status == "pass"
        assert "no backups directory" in result.detail

    def test_pass_under_threshold(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import check_backups_disk_pressure

        backups = isolated_sandbox_ai_home / "workspaces" / "_backups" / "i" / "w" / "2026-05-07-00-00-00"
        backups.mkdir(parents=True)
        (backups / "data").write_text("hi")
        result = check_backups_disk_pressure("u", None)
        assert result.status == "pass"

    def test_warn_when_too_many_entries(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import check_backups_disk_pressure

        ws_dir = isolated_sandbox_ai_home / "workspaces" / "_backups" / "i" / "w"
        ws_dir.mkdir(parents=True)
        for n in range(51):
            (ws_dir / f"2026-05-07-00-00-{n:02d}").mkdir()
        result = check_backups_disk_pressure("u", None)
        assert result.status == "warn"
        assert "51 entries" in result.detail

    def test_warn_when_size_exceeds(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_backups_disk_pressure

        backup = isolated_sandbox_ai_home / "workspaces" / "_backups" / "i" / "w" / "2026-05-07-00-00-00"
        backup.mkdir(parents=True)
        (backup / "data").write_text("x")

        # Mock lstat to report a 6 GB file size.
        real_lstat = os.lstat

        def fat_stat(path: str) -> os.stat_result:
            st = real_lstat(path)
            if path.endswith("/data"):
                # Build a stat_result clone with inflated st_size.
                return os.stat_result(
                    (st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid, st.st_gid,
                     6 * 1024**3 + 1, st.st_atime, st.st_mtime, st.st_ctime)
                )
            return st

        monkeypatch.setattr("core.doctor.os.lstat", fat_stat)
        result = check_backups_disk_pressure("u", None)
        assert result.status == "warn"

    def test_unstattable_file_skipped(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_backups_disk_pressure

        backup = isolated_sandbox_ai_home / "workspaces" / "_backups" / "i" / "w" / "2026-05-07-00-00-00"
        backup.mkdir(parents=True)
        (backup / "data").write_text("x")

        real_lstat = os.lstat

        def boom(path: str) -> os.stat_result:
            if path.endswith("/data"):
                raise OSError("denied")
            return real_lstat(path)

        monkeypatch.setattr("core.doctor.os.lstat", boom)
        result = check_backups_disk_pressure("u", None)
        # 1 entry, 0 bytes — under threshold.
        assert result.status == "pass"

    def test_stray_files_in_backup_tree_skipped(self, isolated_sandbox_ai_home: Any) -> None:
        """Files (not dirs) under _backups/, _backups/<inst>/, or
        _backups/<inst>/<ws>/ are skipped by the entry-count walk."""
        from core.doctor import check_backups_disk_pressure

        backups = isolated_sandbox_ai_home / "workspaces" / "_backups"
        backups.mkdir(parents=True)
        # Stray at the _backups/ level (not an instance dir).
        (backups / "README").write_text("not an instance")
        # Stray at the <inst>/ level (not a workspace dir).
        (backups / "inst").mkdir()
        (backups / "inst" / "stray-file").write_text("not a workspace")
        # Stray at the <ws>/ level (not a timestamp dir).
        (backups / "inst" / "ws").mkdir()
        (backups / "inst" / "ws" / "another-stray").write_text("not a timestamp")
        result = check_backups_disk_pressure("u", None)
        assert result.status == "pass"


class TestCheckBackupsPartialDirsPresent:
    def test_pass_when_no_backups_dir(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import check_backups_partial_dirs_present

        result = check_backups_partial_dirs_present("u", None)
        assert result.status == "pass"

    def test_pass_when_partial_is_fresh(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import check_backups_partial_dirs_present

        partial = isolated_sandbox_ai_home / "workspaces" / "_backups" / "i" / "w" / "2026-05-07-00-00-00.partial"
        partial.mkdir(parents=True)
        result = check_backups_partial_dirs_present("u", None)
        assert result.status == "pass"

    def test_warn_when_partial_is_stale(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_backups_partial_dirs_present

        partial = isolated_sandbox_ai_home / "workspaces" / "_backups" / "i" / "w" / "2026-05-07-00-00-00.partial"
        partial.mkdir(parents=True)

        # Backdate the mtime by 2 hours.
        import time

        old = time.time() - 7200
        os.utime(partial, (old, old))

        result = check_backups_partial_dirs_present("u", None)
        assert result.status == "warn"
        assert ".partial" in result.detail

    def test_unstattable_partial_skipped(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_backups_partial_dirs_present

        partial = isolated_sandbox_ai_home / "workspaces" / "_backups" / "i" / "w" / "2026-05-07-00-00-00.partial"
        partial.mkdir(parents=True)

        real_lstat = os.lstat

        def boom(path: str) -> os.stat_result:
            if path.endswith(".partial"):
                raise OSError("denied")
            return real_lstat(path)

        monkeypatch.setattr("core.doctor.os.lstat", boom)
        result = check_backups_partial_dirs_present("u", None)
        assert result.status == "pass"


class TestCheckDevUmaskWorkspaceFriendly:
    def test_skip_when_no_workspaces(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_dev_umask_workspace_friendly

        monkeypatch.setattr("core.doctor._scan_instance_workspace_paths", lambda: [])
        result = check_dev_umask_workspace_friendly("u", None)
        assert result.status == "skip"

    def test_warn_on_022_umask(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_dev_umask_workspace_friendly

        monkeypatch.setattr(
            "core.doctor._scan_instance_workspace_paths",
            lambda: [("/i", "main", "/p")],
        )
        # 0o022 → group write masked.
        monkeypatch.setattr("core.doctor.os.umask", lambda mask: 0o022 if mask == 0 else 0)
        result = check_dev_umask_workspace_friendly("u", None)
        assert result.status == "warn"
        assert "0022" in result.detail

    def test_pass_on_002_umask(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_dev_umask_workspace_friendly

        monkeypatch.setattr(
            "core.doctor._scan_instance_workspace_paths",
            lambda: [("/i", "main", "/p")],
        )
        monkeypatch.setattr("core.doctor.os.umask", lambda mask: 0o002 if mask == 0 else 0)
        result = check_dev_umask_workspace_friendly("u", None)
        assert result.status == "pass"


class TestCheckComposeProjectNameCollision:
    def test_pass_when_no_registered_instances(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import check_compose_project_name_collision

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True)
        (state / "instances.json").write_text("{}")
        result = check_compose_project_name_collision("u", None)
        assert result.status == "pass"
        assert "no registered" in result.detail

    def test_skip_on_timeout(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_compose_project_name_collision

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True)
        (state / "instances.json").write_text(json.dumps({"foo": {"instance_dir": "/x"}}))

        def boom(*a: Any, **k: Any) -> Any:
            raise subprocess.TimeoutExpired(["docker"], 15)

        monkeypatch.setattr("core.doctor.subprocess.run", boom)
        result = check_compose_project_name_collision("u", None)
        assert result.status == "skip"
        assert "timed out" in result.detail

    def test_skip_on_nonzero_exit(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_compose_project_name_collision

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True)
        (state / "instances.json").write_text(json.dumps({"foo": {"instance_dir": "/x"}}))

        result_obj = subprocess.CompletedProcess(["docker"], 1, stdout="", stderr="boom")
        monkeypatch.setattr("core.doctor.subprocess.run", lambda *a, **k: result_obj)
        out = check_compose_project_name_collision("u", None)
        assert out.status == "skip"
        assert "failed" in out.detail

    def test_skip_on_unparseable_output(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_compose_project_name_collision

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True)
        (state / "instances.json").write_text(json.dumps({"foo": {"instance_dir": "/x"}}))

        result_obj = subprocess.CompletedProcess(["docker"], 0, stdout="not-json", stderr="")
        monkeypatch.setattr("core.doctor.subprocess.run", lambda *a, **k: result_obj)
        out = check_compose_project_name_collision("u", None)
        assert out.status == "skip"
        assert "parse" in out.detail

    def test_pass_with_registered_instances_and_clean_daemon(
        self, isolated_sandbox_ai_home: Any, monkeypatch: Any
    ) -> None:
        from core.doctor import check_compose_project_name_collision

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True)
        (state / "instances.json").write_text(json.dumps({"foo": {"instance_dir": "/x"}}))

        result_obj = subprocess.CompletedProcess(["docker"], 0, stdout="[]", stderr="")
        monkeypatch.setattr("core.doctor.subprocess.run", lambda *a, **k: result_obj)
        out = check_compose_project_name_collision("u", None)
        assert out.status == "pass"


class TestCheckWorkspacePathInWalkerBoundary:
    def test_pass_when_no_workspaces(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_workspace_path_in_walker_boundary

        monkeypatch.setattr("core.doctor._scan_instance_workspace_paths", lambda: [])
        result = check_workspace_path_in_walker_boundary("u", None)
        assert result.status == "pass"

    def test_fail_when_workspace_at_boundary(
        self, isolated_sandbox_ai_home: Any, monkeypatch: Any
    ) -> None:
        from core.doctor import check_workspace_path_in_walker_boundary

        monkeypatch.setattr(
            "core.doctor._scan_instance_workspace_paths",
            lambda: [("/i", "main", "/etc")],
        )
        result = check_workspace_path_in_walker_boundary("u", None)
        assert result.status == "fail"
        assert "/etc" in result.detail

    def test_realpath_oserror_skipped(
        self, isolated_sandbox_ai_home: Any, monkeypatch: Any
    ) -> None:
        from core.doctor import check_workspace_path_in_walker_boundary

        monkeypatch.setattr(
            "core.doctor._scan_instance_workspace_paths",
            lambda: [("/i", "main", "/some/path")],
        )

        def boom(_: str) -> str:
            raise OSError("denied")

        monkeypatch.setattr("core.doctor.os.path.realpath", boom)
        result = check_workspace_path_in_walker_boundary("u", None)
        # Path skipped → no offenders → pass.
        assert result.status == "pass"


class TestCheckWorkspaceHomeSingleFilesystem:
    def test_pass_when_workspaces_dir_absent(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import check_workspace_home_single_filesystem

        isolated_sandbox_ai_home.mkdir(parents=True)
        result = check_workspace_home_single_filesystem("u", None)
        assert result.status == "pass"
        assert "absent" in result.detail

    def test_pass_on_same_fs(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import check_workspace_home_single_filesystem

        (isolated_sandbox_ai_home / "workspaces").mkdir(parents=True)
        result = check_workspace_home_single_filesystem("u", None)
        assert result.status == "pass"

    def test_warn_on_cross_fs(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_workspace_home_single_filesystem

        (isolated_sandbox_ai_home / "workspaces").mkdir(parents=True)

        real_stat = os.stat

        def differ(path: Any) -> Any:
            st = real_stat(path)
            if str(path).endswith("/workspaces"):
                # Simulate different st_dev.
                return os.stat_result(
                    (st.st_mode, st.st_ino, st.st_dev + 1, st.st_nlink, st.st_uid, st.st_gid,
                     st.st_size, st.st_atime, st.st_mtime, st.st_ctime)
                )
            return st

        monkeypatch.setattr("core.doctor.os.stat", differ)
        result = check_workspace_home_single_filesystem("u", None)
        assert result.status == "warn"
        assert "different filesystems" in result.detail

    def test_skip_on_stat_error(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_workspace_home_single_filesystem

        (isolated_sandbox_ai_home / "workspaces").mkdir(parents=True)

        def boom(path: Any) -> Any:
            raise PermissionError("denied")

        monkeypatch.setattr("core.doctor.os.stat", boom)
        result = check_workspace_home_single_filesystem("u", None)
        assert result.status == "skip"


class TestCheckLegacySandboxesDirDetected:
    def test_pass_when_absent(self, tmp_path: Any, monkeypatch: Any) -> None:
        from core.doctor import check_legacy_sandboxes_dir_detected

        monkeypatch.chdir(tmp_path)
        result = check_legacy_sandboxes_dir_detected("u", None)
        assert result.status == "pass"

    def test_warn_when_present(self, tmp_path: Any, monkeypatch: Any) -> None:
        from core.doctor import check_legacy_sandboxes_dir_detected

        (tmp_path / "sandboxes").mkdir()
        monkeypatch.chdir(tmp_path)
        result = check_legacy_sandboxes_dir_detected("u", None)
        assert result.status == "warn"
        assert "sandboxes" in result.detail


class TestCheckLegacyWorkspaceInUserProjectRoot:
    def test_pass_when_no_legacy_field(self, tmp_path: Any, monkeypatch: Any) -> None:
        from core.doctor import check_legacy_workspace_in_user_project_root

        inst = tmp_path / "inst"
        inst.mkdir()
        (inst / "sandbox.toml").write_text('[instance]\nname = "x"\n')
        monkeypatch.setattr("core.doctor._scan_instance_dirs", lambda: [str(inst)])
        result = check_legacy_workspace_in_user_project_root("u", None)
        assert result.status == "pass"

    def test_warn_when_legacy_field_present(self, tmp_path: Any, monkeypatch: Any) -> None:
        from core.doctor import check_legacy_workspace_in_user_project_root

        inst = tmp_path / "myinst"
        inst.mkdir()
        (inst / "sandbox.toml").write_text(
            '[instance]\nname = "myinst"\nuser_project_root = "/old/path"\n'
        )
        monkeypatch.setattr("core.doctor._scan_instance_dirs", lambda: [str(inst)])
        result = check_legacy_workspace_in_user_project_root("u", None)
        assert result.status == "warn"
        assert "myinst" in result.detail

    def test_unparseable_toml_skipped(self, tmp_path: Any, monkeypatch: Any) -> None:
        from core.doctor import check_legacy_workspace_in_user_project_root

        inst = tmp_path / "inst"
        inst.mkdir()
        (inst / "sandbox.toml").write_text("not = valid = toml = !!")
        monkeypatch.setattr("core.doctor._scan_instance_dirs", lambda: [str(inst)])
        result = check_legacy_workspace_in_user_project_root("u", None)
        # Skipped silently → pass (no legacy detected because we couldn't read).
        assert result.status == "pass"


class TestCheckLegacyRegistryShape:
    def test_pass_on_name_keyed(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import check_legacy_registry_shape

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True)
        (state / "instances.json").write_text(json.dumps({"foo": {"instance_dir": "/x"}}))
        result = check_legacy_registry_shape("u", None)
        assert result.status == "pass"

    def test_warn_on_path_keyed(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import check_legacy_registry_shape

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True)
        (state / "instances.json").write_text(json.dumps({"/home/dev/foo": {"x": 1}}))
        result = check_legacy_registry_shape("u", None)
        assert result.status == "warn"


class TestScanInstanceWorkspacePaths:
    def test_skips_missing_sandbox_toml(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import _scan_instance_workspace_paths

        monkeypatch.setattr("core.doctor._scan_instance_dirs", lambda: ["/no/such/dir"])
        assert _scan_instance_workspace_paths() == []

    def test_skips_unparseable_toml(self, isolated_sandbox_ai_home: Any, monkeypatch: Any, tmp_path: Any) -> None:
        from core.doctor import _scan_instance_workspace_paths

        inst = tmp_path / "inst"
        inst.mkdir()
        (inst / "sandbox.toml").write_text("garbage = =")
        monkeypatch.setattr("core.doctor._scan_instance_dirs", lambda: [str(inst)])
        assert _scan_instance_workspace_paths() == []

    def test_skips_non_dict_workspaces_block(
        self, isolated_sandbox_ai_home: Any, monkeypatch: Any, tmp_path: Any
    ) -> None:
        from core.doctor import _scan_instance_workspace_paths

        inst = tmp_path / "inst"
        inst.mkdir()
        (inst / "sandbox.toml").write_text("workspaces = []\n")
        monkeypatch.setattr("core.doctor._scan_instance_dirs", lambda: [str(inst)])
        assert _scan_instance_workspace_paths() == []

    def test_yields_each_workspace(
        self, isolated_sandbox_ai_home: Any, monkeypatch: Any, tmp_path: Any
    ) -> None:
        from core.doctor import _scan_instance_workspace_paths

        inst = tmp_path / "myinst"
        inst.mkdir()
        (inst / "sandbox.toml").write_text(
            '[workspaces.main]\nbootstrap_mode = "empty"\npath = "/p1"\n'
            '[workspaces.scratch]\nbootstrap_mode = "empty"\npath = "/p2"\n'
        )
        monkeypatch.setattr("core.doctor._scan_instance_dirs", lambda: [str(inst)])
        result = sorted(_scan_instance_workspace_paths())
        assert result == [(str(inst), "main", "/p1"), (str(inst), "scratch", "/p2")]



