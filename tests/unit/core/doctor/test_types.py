"""Tests for core.doctor.types — Check, CheckResult, distro detection, install-cmd helper."""

from __future__ import annotations

from unittest.mock import mock_open, patch

# ── Section 1: Data Types ────────────────────────────────────────────────────


class TestCheckResult:
    """CheckResult dataclass validation."""

    def test_check_result_pass(self) -> None:
        from core.doctor.types import CheckResult

        r = CheckResult(status="pass", name="test", detail="ok")
        assert r.status == "pass"
        assert r.name == "test"
        assert r.detail == "ok"
        assert r.remediation is None
        assert r.doc_ref is None

    def test_check_result_fail_with_remediation(self) -> None:
        from core.doctor.types import CheckResult

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
        from core.doctor.types import CheckResult

        r = CheckResult(status="skip", name="skipped-check", detail="dep failed")
        assert r.status == "skip"

    def test_check_result_status_literal(self) -> None:
        """Status must be one of pass/fail/skip/warn."""
        from core.doctor.types import CheckResult

        for s in ("pass", "fail", "skip", "warn"):
            r = CheckResult(status=s, name="t", detail="d")
            assert r.status == s


class TestCheckDataclass:
    """Check dataclass validation."""

    def test_check_fields(self) -> None:
        from core.doctor.types import Check, CheckResult

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
        from core.doctor.types import Check, CheckResult

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

    def test_applies_in_defaults_to_both_modes(self) -> None:
        """A check is mode-agnostic by default: ``applies_in`` is BOTH modes."""
        from core.doctor.types import Check, CheckResult
        from core.host_config import DockerExecutionMode

        def dummy_run(user: str, distro: str | None) -> CheckResult:
            return CheckResult(status="pass", name="d", detail="ok")

        c = Check(id="c", name="C", category="t", depends_on=[], run=dummy_run, remediation="")
        assert c.applies_in == frozenset(DockerExecutionMode)
        assert DockerExecutionMode.SEPARATE_USER in c.applies_in
        assert DockerExecutionMode.OPERATOR_ROOTLESS in c.applies_in

    def test_applies_in_can_gate_to_separate_user(self) -> None:
        from core.doctor.types import Check, CheckResult
        from core.host_config import DockerExecutionMode

        def dummy_run(user: str, distro: str | None) -> CheckResult:
            return CheckResult(status="pass", name="d", detail="ok")

        c = Check(
            id="c",
            name="C",
            category="t",
            depends_on=[],
            run=dummy_run,
            remediation="",
            applies_in=frozenset({DockerExecutionMode.SEPARATE_USER}),
        )
        assert DockerExecutionMode.OPERATOR_ROOTLESS not in c.applies_in


# ── Section 2: Distro Detection ─────────────────────────────────────────────


class TestDetectDistro:
    """Distro detection from /etc/os-release."""

    def test_debian_detected(self) -> None:
        from core.doctor.types import detect_distro

        content = 'ID=debian\nID_LIKE=""\n'
        with patch("builtins.open", mock_open(read_data=content)):
            assert detect_distro() == "debian"

    def test_ubuntu_detected_via_id_like(self) -> None:
        from core.doctor.types import detect_distro

        content = 'ID=ubuntu\nID_LIKE="debian"\n'
        with patch("builtins.open", mock_open(read_data=content)):
            assert detect_distro() == "debian"

    def test_fedora_detected(self) -> None:
        from core.doctor.types import detect_distro

        content = "ID=fedora\n"
        with patch("builtins.open", mock_open(read_data=content)):
            assert detect_distro() == "fedora"

    def test_arch_detected(self) -> None:
        from core.doctor.types import detect_distro

        content = "ID=arch\n"
        with patch("builtins.open", mock_open(read_data=content)):
            assert detect_distro() == "arch"

    def test_unknown_distro(self) -> None:
        from core.doctor.types import detect_distro

        content = "ID=gentoo\n"
        with patch("builtins.open", mock_open(read_data=content)):
            assert detect_distro() is None

    def test_missing_os_release(self) -> None:
        from core.doctor.types import detect_distro

        with patch("builtins.open", side_effect=FileNotFoundError):
            assert detect_distro() is None


class TestGetInstallCmd:
    """Distro-aware install command mapping."""

    def test_debian_install(self) -> None:
        from core.doctor.types import get_install_cmd

        assert get_install_cmd("debian", "acl") == "sudo apt install acl"

    def test_fedora_install(self) -> None:
        from core.doctor.types import get_install_cmd

        assert get_install_cmd("fedora", "acl") == "sudo dnf install acl"

    def test_arch_install(self) -> None:
        from core.doctor.types import get_install_cmd

        assert get_install_cmd("arch", "acl") == "sudo pacman -S acl"

    def test_unknown_install(self) -> None:
        from core.doctor.types import get_install_cmd

        result = get_install_cmd(None, "acl")
        assert "acl" in result
        assert "apt" not in result
        assert "dnf" not in result
