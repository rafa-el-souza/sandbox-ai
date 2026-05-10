"""Tests for core.doctor.checks.privilege_boundary.

Covers the 11 privilege-boundary checks: sudo, machinectl, user_exists,
systemd_machined, machinectl_reachable, docker_available, docker_rootless,
runsc_registered, runsc_runtimeargs, host_uds, compose_project_name_collision.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any
from unittest.mock import mock_open, patch


def test_module_exposes_eleven_check_functions() -> None:
    from core.doctor.checks import privilege_boundary

    expected = {
        "check_compose_project_name_collision",
        "check_docker_available",
        "check_docker_rootless",
        "check_host_uds",
        "check_machinectl",
        "check_machinectl_reachable",
        "check_runsc_registered",
        "check_runsc_runtimeargs",
        "check_sudo",
        "check_systemd_machined",
        "check_user_exists",
    }
    assert expected.issubset(set(dir(privilege_boundary)))
    assert set(privilege_boundary.__all__) == expected


def test_public_re_exports_resolve_to_topic_module() -> None:
    import core.doctor as doctor_pkg
    from core.doctor.checks import privilege_boundary

    for name in privilege_boundary.__all__:
        assert getattr(doctor_pkg, name) is getattr(privilege_boundary, name)


# ── sudo / machinectl binary checks ─────────────────────────────────────────


class TestSudoAndMachinectlBinaries:
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


class TestUserAndSystemdChecks:
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


class TestMachinectlReachable:
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


class TestDockerChecks:
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


class TestDistroIdLikeFallback:
    """Cover ID_LIKE parsing branch when ID is not in _DISTRO_MAP."""

    def test_id_like_resolves_when_id_unknown(self) -> None:
        from core.doctor import detect_distro

        content = 'ID=linuxmint\nID_LIKE="ubuntu debian"\n'
        with patch("builtins.open", mock_open(read_data=content)):
            assert detect_distro() == "debian"


class TestRunscJsonDecodeError:
    def test_runsc_bad_json_output(self) -> None:
        from core.doctor import check_runsc_registered

        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="NOT-VALID-JSON{{{", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = check_runsc_registered("sandbox", None)
            assert result.status == "fail"


class TestCheckRunscRuntimeArgs:
    def test_both_args_present_pass(self) -> None:
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
        from core.doctor import check_runsc_runtimeargs

        docker_info = json.dumps({"runsc": {"path": "/usr/local/bin/runsc"}})
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=docker_info, stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = check_runsc_runtimeargs("sandbox", None)
            assert result.remediation is not None
            assert "~sandbox/.config/docker/daemon.json" in result.remediation


class TestCheckHostUds:
    def test_check_host_uds_none_passes(self) -> None:
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

    def test_check_host_uds_docker_query_failure(self) -> None:
        from core.doctor import check_host_uds

        mock_result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="error")
        with patch("subprocess.run", return_value=mock_result):
            result = check_host_uds("sandbox", None)
            assert result.status == "warn"
            assert "daemon.json" in (result.remediation or "")

    def test_check_host_uds_json_parse_failure(self) -> None:
        from core.doctor import check_host_uds

        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="NOT-JSON{{{", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = check_host_uds("sandbox", None)
            assert result.status == "warn"
            assert "daemon.json" in (result.remediation or "")


class TestRunscRuntimeArgsEdgeCases:
    def test_nonzero_exit_returns_warn(self) -> None:
        from core.doctor import check_runsc_runtimeargs

        mock_result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="error")
        with patch("subprocess.run", return_value=mock_result):
            result = check_runsc_runtimeargs("sandbox", None)
            assert result.status == "warn"
            assert "Could not query" in result.detail

    def test_json_decode_error_returns_warn(self) -> None:
        from core.doctor import check_runsc_runtimeargs

        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="{{INVALID}}", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = check_runsc_runtimeargs("sandbox", None)
            assert result.status == "warn"
            assert "parse" in result.detail.lower()


class TestPolkitMachinectlCommandShape:
    """Polkit-mode machinectl/docker commands omit 'sudo' prefix."""

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

        monkeypatch.setattr("core.doctor.checks.privilege_boundary.subprocess.run", boom)
        result = check_compose_project_name_collision("u", None)
        assert result.status == "skip"
        assert "timed out" in result.detail

    def test_skip_on_nonzero_exit(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_compose_project_name_collision

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True)
        (state / "instances.json").write_text(json.dumps({"foo": {"instance_dir": "/x"}}))

        result_obj = subprocess.CompletedProcess(["docker"], 1, stdout="", stderr="boom")
        monkeypatch.setattr("core.doctor.checks.privilege_boundary.subprocess.run", lambda *a, **k: result_obj)
        out = check_compose_project_name_collision("u", None)
        assert out.status == "skip"
        assert "failed" in out.detail

    def test_skip_on_unparseable_output(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_compose_project_name_collision

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True)
        (state / "instances.json").write_text(json.dumps({"foo": {"instance_dir": "/x"}}))

        result_obj = subprocess.CompletedProcess(["docker"], 0, stdout="not-json", stderr="")
        monkeypatch.setattr("core.doctor.checks.privilege_boundary.subprocess.run", lambda *a, **k: result_obj)
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
        monkeypatch.setattr("core.doctor.checks.privilege_boundary.subprocess.run", lambda *a, **k: result_obj)
        out = check_compose_project_name_collision("u", None)
        assert out.status == "pass"
