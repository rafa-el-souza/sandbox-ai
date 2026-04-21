"""Tests for the doctor module: host readiness diagnostics.

Covers data types, distro detection, binary checks, user/systemd checks,
machinectl reachability, Docker checks, filesystem checks, check runner,
and Rich output renderer.
"""

import subprocess
from typing import Any
from unittest.mock import MagicMock, mock_open, patch

import pytest

# ── Section 1: Data Types ────────────────────────────────────────────────────


class TestCheckResult:
    """Task 1.1: CheckResult dataclass validation."""

    def test_check_result_pass(self) -> None:
        from core.doctor import CheckResult

        r = CheckResult(status="pass", name="test", detail="ok")
        assert r.status == "pass"
        assert r.name == "test"
        assert r.detail == "ok"
        assert r.remediation is None
        assert r.doc_ref is None

    def test_check_result_fail_with_remediation(self) -> None:
        from core.doctor import CheckResult

        r = CheckResult(
            status="fail",
            name="test",
            detail="not ok",
            remediation="fix it",
            doc_ref="https://docs.example.com",
        )
        assert r.status == "fail"
        assert r.remediation == "fix it"
        assert r.doc_ref == "https://docs.example.com"

    def test_check_result_skip(self) -> None:
        from core.doctor import CheckResult

        r = CheckResult(status="skip", name="skipped-check", detail="dep failed")
        assert r.status == "skip"

    def test_check_result_status_literal(self) -> None:
        """Status must be one of pass/fail/skip."""
        from core.doctor import CheckResult

        # Valid statuses should not raise
        for s in ("pass", "fail", "skip"):
            r = CheckResult(status=s, name="t", detail="d")
            assert r.status == s


class TestCheckDataclass:
    """Task 1.1: Check dataclass validation."""

    def test_check_fields(self) -> None:
        from core.doctor import Check, CheckResult

        def dummy_run(user: str, distro: str | None) -> CheckResult:
            return CheckResult(status="pass", name="dummy", detail="ok")

        c = Check(
            id="chk-1",
            name="Dummy Check",
            category="test",
            depends_on=[],
            run=dummy_run,
            remediation="do nothing",
            doc_ref="https://example.com",
        )
        assert c.id == "chk-1"
        assert c.name == "Dummy Check"
        assert c.category == "test"
        assert c.depends_on == []
        assert c.remediation == "do nothing"
        assert c.doc_ref == "https://example.com"
        result = c.run("sandbox", None)
        assert result.status == "pass"

    def test_check_with_dependencies(self) -> None:
        from core.doctor import Check, CheckResult

        def dummy_run(user: str, distro: str | None) -> CheckResult:
            return CheckResult(status="pass", name="dep", detail="ok")

        c = Check(
            id="chk-2",
            name="Dep Check",
            category="test",
            depends_on=["chk-1"],
            run=dummy_run,
            remediation="fix dep",
        )
        assert c.depends_on == ["chk-1"]


# ── Section 2: Distro Detection ─────────────────────────────────────────────


class TestDetectDistro:
    """Task 2.1: Distro detection from /etc/os-release."""

    def test_debian_detected(self) -> None:
        from core.doctor import detect_distro

        content = 'ID=debian\nID_LIKE=""\n'
        with patch("builtins.open", mock_open(read_data=content)):
            assert detect_distro() == "debian"

    def test_ubuntu_detected_via_id_like(self) -> None:
        from core.doctor import detect_distro

        content = 'ID=ubuntu\nID_LIKE="debian"\n'
        with patch("builtins.open", mock_open(read_data=content)):
            assert detect_distro() == "debian"

    def test_fedora_detected(self) -> None:
        from core.doctor import detect_distro

        content = "ID=fedora\n"
        with patch("builtins.open", mock_open(read_data=content)):
            assert detect_distro() == "fedora"

    def test_arch_detected(self) -> None:
        from core.doctor import detect_distro

        content = "ID=arch\n"
        with patch("builtins.open", mock_open(read_data=content)):
            assert detect_distro() == "arch"

    def test_unknown_distro(self) -> None:
        from core.doctor import detect_distro

        content = "ID=gentoo\n"
        with patch("builtins.open", mock_open(read_data=content)):
            assert detect_distro() is None

    def test_missing_os_release(self) -> None:
        from core.doctor import detect_distro

        with patch("builtins.open", side_effect=FileNotFoundError):
            assert detect_distro() is None


class TestGetInstallCmd:
    """Task 2.1: Distro-aware install command mapping."""

    def test_debian_install(self) -> None:
        from core.doctor import get_install_cmd

        assert get_install_cmd("debian", "acl") == "sudo apt install acl"

    def test_fedora_install(self) -> None:
        from core.doctor import get_install_cmd

        assert get_install_cmd("fedora", "acl") == "sudo dnf install acl"

    def test_arch_install(self) -> None:
        from core.doctor import get_install_cmd

        assert get_install_cmd("arch", "acl") == "sudo pacman -S acl"

    def test_unknown_install(self) -> None:
        from core.doctor import get_install_cmd

        result = get_install_cmd(None, "acl")
        assert "acl" in result
        # Should not contain a package manager prefix
        assert "apt" not in result
        assert "dnf" not in result


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

        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="uid=1000(sandbox)", stderr=""
        )
        with patch("subprocess.run", return_value=mock_result):
            result = check_user_exists("sandbox", None)
            assert result.status == "pass"
            assert "1000" in result.detail

    def test_user_not_exists(self) -> None:
        from core.doctor import check_user_exists

        mock_result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="no such user"
        )
        with patch("subprocess.run", return_value=mock_result):
            result = check_user_exists("sandbox", None)
            assert result.status == "fail"
            assert result.remediation is not None

    def test_systemd_machined_active(self) -> None:
        from core.doctor import check_systemd_machined

        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="active\n", stderr=""
        )
        with patch("subprocess.run", return_value=mock_result):
            result = check_systemd_machined("sandbox", None)
            assert result.status == "pass"

    def test_systemd_machined_inactive(self) -> None:
        from core.doctor import check_systemd_machined

        mock_result = subprocess.CompletedProcess(
            args=[], returncode=3, stdout="inactive\n", stderr=""
        )
        with patch("subprocess.run", return_value=mock_result):
            result = check_systemd_machined("sandbox", None)
            assert result.status == "fail"
            assert "systemctl enable" in (result.remediation or "")


# ── Section 5: machinectl Reachability ───────────────────────────────────────


class TestMachinectlReachable:
    """Task 5.1: machinectl shell reachability with timeout."""

    def test_reachable_success(self) -> None:
        from core.doctor import check_machinectl_reachable

        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok\n", stderr=""
        )
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

        mock_result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="No machine 'sandbox' known"
        )
        with patch("subprocess.run", return_value=mock_result):
            result = check_machinectl_reachable("sandbox", None)
            assert result.status == "fail"
            assert result.detail != ""


# ── Section 6: Docker Checks ────────────────────────────────────────────────


class TestDockerChecks:
    """Task 6.1: Docker availability, rootless, and runsc checks."""

    def test_docker_available_pass(self) -> None:
        from core.doctor import check_docker_available

        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="24.0.7\n", stderr=""
        )
        with patch("subprocess.run", return_value=mock_result):
            result = check_docker_available("sandbox", None)
            assert result.status == "pass"
            assert "24.0.7" in result.detail

    def test_docker_available_fail(self) -> None:
        from core.doctor import check_docker_available

        mock_result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="command not found"
        )
        with patch("subprocess.run", return_value=mock_result):
            result = check_docker_available("sandbox", None)
            assert result.status == "fail"

    def test_docker_rootless_pass(self) -> None:
        from core.doctor import check_docker_rootless

        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="[rootless, cgroupns]", stderr=""
        )
        with patch("subprocess.run", return_value=mock_result):
            result = check_docker_rootless("sandbox", None)
            assert result.status == "pass"

    def test_docker_rootless_system_docker(self) -> None:
        from core.doctor import check_docker_rootless

        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="[apparmor, seccomp]", stderr=""
        )
        with patch("subprocess.run", return_value=mock_result):
            result = check_docker_rootless("sandbox", None)
            assert result.status == "fail"
            assert "rootless" in (result.remediation or "").lower()

    def test_runsc_registered_pass(self) -> None:
        from core.doctor import check_runsc_registered

        docker_info = '{"runsc": {}, "runc": {}}'
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=docker_info, stderr=""
        )
        with patch("subprocess.run", return_value=mock_result):
            result = check_runsc_registered("sandbox", None)
            assert result.status == "pass"

    def test_runsc_not_registered(self) -> None:
        from core.doctor import check_runsc_registered

        docker_info = '{"runc": {}}'
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=docker_info, stderr=""
        )
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
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
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

        with patch("os.path.exists", return_value=True):
            result = check_tooling_plane("sandbox", None)
            assert result.status == "pass"
            assert "15" in result.detail

    def test_tooling_plane_missing_files(self) -> None:
        from core.doctor import check_tooling_plane

        def selective_exists(path: str) -> bool:
            return "compose.yml" not in path

        with patch("os.path.exists", side_effect=selective_exists):
            result = check_tooling_plane("sandbox", None)
            assert result.status == "fail"
            assert "compose.yml" in result.detail

    def test_state_dir_writable(self, tmp_path: Any) -> None:
        from core.doctor import check_state_dir_writable

        state_dir = tmp_path / ".state"
        state_dir.mkdir()
        with patch("core.doctor._get_sandbox_ai_home", return_value=str(tmp_path)):
            result = check_state_dir_writable("sandbox", None)
            assert result.status == "pass"

    def test_state_dir_not_writable(self, tmp_path: Any) -> None:
        from core.doctor import check_state_dir_writable

        with (
            patch("core.doctor._get_sandbox_ai_home", return_value=str(tmp_path)),
            patch("tempfile.NamedTemporaryFile", side_effect=OSError("perm denied")),
        ):
            result = check_state_dir_writable("sandbox", None)
            assert result.status == "fail"


# ── Section 8: Check Runner ─────────────────────────────────────────────────


class TestCheckRunner:
    """Task 8.1: Check registry, topological sort, and runner execution."""

    def test_build_check_registry_returns_all_checks(self) -> None:
        from core.doctor import build_check_registry

        checks = build_check_registry()
        assert len(checks) == 12
        ids = [c.id for c in checks]
        assert "sudo" in ids
        assert "machinectl_reachable" in ids
        assert "docker_rootless" in ids

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

    def test_pass_marker(self) -> None:
        from io import StringIO

        from core.doctor import CheckResult, render_results
        from rich.console import Console

        results = [CheckResult(status="pass", name="Test Check", detail="ok")]
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=120)
        render_results(results, console=console)
        output = buf.getvalue()
        assert "✓" in output
        assert "Test Check" in output

    def test_fail_marker_with_expansion(self) -> None:
        from io import StringIO

        from core.doctor import CheckResult, render_results
        from rich.console import Console

        results = [
            CheckResult(
                status="fail",
                name="Broken Check",
                detail="something broke",
                remediation="sudo fix-it",
                doc_ref="https://docs.example.com",
            )
        ]
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=120)
        render_results(results, console=console)
        output = buf.getvalue()
        assert "✗" in output
        assert "Broken Check" in output
        assert "sudo fix-it" in output

    def test_skip_marker(self) -> None:
        from io import StringIO

        from core.doctor import CheckResult, render_results
        from rich.console import Console

        results = [CheckResult(status="skip", name="Skipped Check", detail="requires: root")]
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=120)
        render_results(results, console=console)
        output = buf.getvalue()
        assert "⊘" in output
        assert "Skipped Check" in output

    def test_summary_line_format(self) -> None:
        from io import StringIO

        from core.doctor import CheckResult, render_results
        from rich.console import Console

        results = [
            CheckResult(status="pass", name="A", detail="ok"),
            CheckResult(status="fail", name="B", detail="bad", remediation="fix"),
            CheckResult(status="skip", name="C", detail="skip"),
        ]
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=120)
        render_results(results, console=console)
        output = buf.getvalue()
        assert "1" in output  # 1 passed
        assert "failed" in output.lower() or "fail" in output.lower()

    def test_category_grouping(self) -> None:
        from io import StringIO

        from core.doctor import CheckResult, render_results
        from rich.console import Console

        results = [
            CheckResult(status="pass", name="A", detail="ok", category="Group 1"),
            CheckResult(status="pass", name="B", detail="ok", category="Group 2"),
        ]
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=120, highlight=False)
        render_results(results, console=console)
        output = buf.getvalue()
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

        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="NOT-VALID-JSON{{{", stderr=""
        )
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
        # Chain 2 has 2 checks: setfacl, ACL support
        assert len(results) == 2
        names = {r.name for r in results}
        assert "setfacl" in names or "setfacl binary" in names

    def test_multiple_categories_returns_union(self) -> None:
        """Filtering by multiple categories returns checks from all specified categories."""
        from core.doctor import run_check_subset

        results = run_check_subset(["Filesystem", "Repo Integrity"], "sandbox", None)
        # Chain 2 (2 checks) + Chain 3 (2 checks) = 4
        assert len(results) == 4
        names = {r.name for r in results}
        # Must contain checks from both categories
        assert "tooling plane" in names or "state dir writable" in names

    def test_cascading_skip_within_subset(self) -> None:
        """When a root check fails within a subset, dependents are skipped."""
        from unittest.mock import patch

        from core.doctor import CheckResult, run_check_subset

        # setfacl fails → ACL support should be skipped
        def fake_setfacl(user: str, distro: str | None) -> CheckResult:
            return CheckResult(
                status="fail", name="setfacl", detail="not found", category="Filesystem"
            )

        with patch("core.doctor.check_setfacl", fake_setfacl):
            results = run_check_subset(["Filesystem"], "sandbox", None)
            assert len(results) == 2
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

    def test_cross_chain_dependency_raises_valueerror(self) -> None:
        """ValueError raised if a filtered subset has a depends_on pointing outside the subset."""
        from core.doctor import Check, CheckResult

        # Construct a registry where Chain 2 depends on a Chain 1 check
        def noop(u: str, d: str | None) -> CheckResult:
            return CheckResult(status="pass", name="n", detail="")

        fake_checks = [
            Check(
                id="setfacl", name="setfacl", category="Filesystem",
                depends_on=["sudo"],  # cross-chain dependency!
                run=noop, remediation="",
            ),
            Check(
                id="sudo", name="sudo", category="Privilege Boundary",
                depends_on=[], run=noop, remediation="",
            ),
        ]

        with patch("core.doctor.build_check_registry", return_value=fake_checks):
            from core.doctor import run_check_subset

            with pytest.raises(ValueError, match="outside the subset"):
                run_check_subset(["Filesystem"], "sandbox", None)


class TestRenderResultsWithSubset:
    """Task 1.3: Verify render_results works unchanged with subset results."""

    def test_render_results_accepts_subset(self) -> None:
        """render_results works with subset results (no code change expected)."""
        import re
        from io import StringIO

        from core.doctor import CheckResult, render_results
        from rich.console import Console

        # Simulate subset output — only Filesystem category
        results = [
            CheckResult(status="pass", name="setfacl binary", detail="ok", category="Filesystem"),
            CheckResult(status="pass", name="ACL support", detail="ok", category="Filesystem"),
        ]
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, no_color=True, width=120)
        render_results(results, console=console)
        output = buf.getvalue()
        # Strip ANSI escape sequences — force_terminal emits bold escapes even with no_color
        plain = re.sub(r"\x1b\[[0-9;]*m", "", output)
        assert "Filesystem" in plain
        assert "2/2 passed" in plain

