"""Tests for core.doctor.checks.privilege_boundary.

Covers the 12 privilege-boundary checks: sudo, machinectl, tlog, user_exists,
systemd_machined, machinectl_reachable, docker_available, docker_rootless,
runsc_registered, runsc_runtimeargs, host_uds, compose_project_name_collision.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any
from unittest.mock import mock_open, patch

from core.exceptions import SandboxExecutionError


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    """A successful ``core.dispatch.invoke`` return (returncode 0 always — a
    non-zero inner exit surfaces as ``SandboxExecutionError`` from the sterile
    Executor, never as a returncode-bearing CompletedProcess)."""
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _exec_error(*, timeout: bool = False) -> SandboxExecutionError:
    """Build the ``SandboxExecutionError`` ``core.dispatch.invoke`` raises.

    The sterile Executor chains the originating ``subprocess.TimeoutExpired`` /
    ``CalledProcessError`` via ``raise ... from e``; the refactored checks
    discriminate timeout-vs-other on ``exc.__cause__``."""
    err = SandboxExecutionError("[FATAL] Sandbox Execution Fault")
    if timeout:
        err.__cause__ = subprocess.TimeoutExpired(cmd="dispatch", timeout=15)
    else:
        err.__cause__ = subprocess.CalledProcessError(returncode=1, cmd="dispatch")
    return err


def test_module_exposes_twelve_check_functions() -> None:
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
        "check_tlog",
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


class TestTlogBinary:
    def test_check_tlog_present(self) -> None:
        from core.doctor import check_tlog

        with patch("shutil.which", return_value="/usr/bin/tlog-rec"):
            result = check_tlog("sandbox", None)
            assert result.status == "pass"
            assert result.name == "tlog"
            assert "/usr/bin/tlog-rec" in result.detail

    def test_check_tlog_absent(self) -> None:
        from core.doctor import check_tlog
        from core.doctor.types import _BINARY_PACKAGES

        with patch("shutil.which", return_value=None):
            result = check_tlog("sandbox", "debian")
            assert result.status == "fail"
            assert result.name == "tlog"
            assert result.detail == "tlog-rec not found on PATH"
            assert result.remediation is not None
            assert _BINARY_PACKAGES["tlog"] in result.remediation


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

        with patch("core.dispatch.invoke", return_value=_ok("ok\n")) as inv:
            result = check_machinectl_reachable("sandbox", None)
            assert result.status == "pass"
            (op, args, _hc), kw = inv.call_args
            assert op == "auth-probe"
            assert list(args) == []
            assert kw["timeout"] == 10

    def test_reachable_timeout(self) -> None:
        from core.doctor import check_machinectl_reachable

        with patch("core.dispatch.invoke", side_effect=_exec_error(timeout=True)):
            result = check_machinectl_reachable("sandbox", None)
            assert result.status == "fail"
            assert "timeout" in result.detail.lower() or "sudoers" in (result.remediation or "").lower()

    def test_reachable_nonzero_exit(self) -> None:
        from core.doctor import check_machinectl_reachable

        with patch("core.dispatch.invoke", side_effect=_exec_error()):
            result = check_machinectl_reachable("sandbox", None)
            assert result.status == "fail"
            assert result.detail != ""


class TestDockerChecks:
    def test_docker_available_pass(self) -> None:
        from core.doctor import check_docker_available

        with patch("core.dispatch.invoke", return_value=_ok("24.0.7\n")) as inv:
            result = check_docker_available("sandbox", None)
            assert result.status == "pass"
            assert "24.0.7" in result.detail
            (op, args, _hc), kw = inv.call_args
            assert op == "docker-version"
            assert list(args) == []
            assert kw["timeout"] == 15

    def test_docker_available_fail(self) -> None:
        from core.doctor import check_docker_available

        with patch("core.dispatch.invoke", side_effect=_exec_error()):
            result = check_docker_available("sandbox", None)
            assert result.status == "fail"

    def test_docker_available_empty_stdout_fail(self) -> None:
        from core.doctor import check_docker_available

        with patch("core.dispatch.invoke", return_value=_ok("")):
            result = check_docker_available("sandbox", None)
            assert result.status == "fail"

    def test_docker_rootless_pass(self) -> None:
        from core.doctor import check_docker_rootless

        with patch("core.dispatch.invoke", return_value=_ok("[rootless, cgroupns]")) as inv:
            result = check_docker_rootless("sandbox", None)
            assert result.status == "pass"
            (op, args, _hc), kw = inv.call_args
            assert op == "docker-info"
            assert list(args) == ["security-options"]
            assert kw["timeout"] == 15

    def test_docker_rootless_system_docker(self) -> None:
        from core.doctor import check_docker_rootless

        with patch("core.dispatch.invoke", return_value=_ok("[apparmor, seccomp]")):
            result = check_docker_rootless("sandbox", None)
            assert result.status == "fail"
            assert "rootless" in (result.remediation or "").lower()

    def test_docker_rootless_invoke_error_fail(self) -> None:
        from core.doctor import check_docker_rootless

        with patch("core.dispatch.invoke", side_effect=_exec_error()):
            result = check_docker_rootless("sandbox", None)
            assert result.status == "fail"

    def test_runsc_registered_pass(self) -> None:
        from core.doctor import check_runsc_registered

        with patch("core.dispatch.invoke", return_value=_ok('{"runsc": {}, "runc": {}}')) as inv:
            result = check_runsc_registered("sandbox", None)
            assert result.status == "pass"
            (op, args, _hc), kw = inv.call_args
            assert op == "docker-info"
            assert list(args) == ["runtimes"]
            assert kw["timeout"] == 15

    def test_runsc_not_registered(self) -> None:
        from core.doctor import check_runsc_registered

        with patch("core.dispatch.invoke", return_value=_ok('{"runc": {}}')):
            result = check_runsc_registered("sandbox", None)
            assert result.status == "fail"

    def test_runsc_registered_invoke_error_fail(self) -> None:
        from core.doctor import check_runsc_registered

        with patch("core.dispatch.invoke", side_effect=_exec_error()):
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

        with patch("core.dispatch.invoke", return_value=_ok("NOT-VALID-JSON{{{")):
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
        with patch("core.dispatch.invoke", return_value=_ok(docker_info)) as inv:
            result = check_runsc_runtimeargs("sandbox", None)
            assert result.status == "pass"
            assert "--oci-seccomp" in result.detail
            assert "--debug-log" in result.detail
            (op, args, _hc), kw = inv.call_args
            assert op == "docker-info"
            assert list(args) == ["runtimes"]
            assert kw["timeout"] == 15

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
        with patch("core.dispatch.invoke", return_value=_ok(docker_info)):
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
        with patch("core.dispatch.invoke", return_value=_ok(docker_info)):
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
        with patch("core.dispatch.invoke", return_value=_ok(docker_info)):
            result = check_runsc_runtimeargs("sandbox", None)
            assert result.status == "warn"
            assert "--oci-seccomp" in result.detail
            assert "--debug-log" in result.detail

    def test_remediation_references_daemon_json(self) -> None:
        from core.doctor import check_runsc_runtimeargs

        docker_info = json.dumps({"runsc": {"path": "/usr/local/bin/runsc"}})
        with patch("core.dispatch.invoke", return_value=_ok(docker_info)):
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
        with patch("core.dispatch.invoke", return_value=_ok(docker_info)) as inv:
            result = check_host_uds("sandbox", None)
            assert result.status == "pass"
            (op, args, _hc), kw = inv.call_args
            assert op == "docker-info"
            assert list(args) == ["runtimes"]
            assert kw["timeout"] == 15

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
        with patch("core.dispatch.invoke", return_value=_ok(docker_info)):
            result = check_host_uds("sandbox", None)
            assert result.status == "warn"
            assert "daemon.json" in (result.remediation or "")

    def test_check_host_uds_docker_query_failure(self) -> None:
        from core.doctor import check_host_uds

        with patch("core.dispatch.invoke", side_effect=_exec_error()):
            result = check_host_uds("sandbox", None)
            assert result.status == "warn"
            assert "daemon.json" in (result.remediation or "")

    def test_check_host_uds_json_parse_failure(self) -> None:
        from core.doctor import check_host_uds

        with patch("core.dispatch.invoke", return_value=_ok("NOT-JSON{{{")):
            result = check_host_uds("sandbox", None)
            assert result.status == "warn"
            assert "daemon.json" in (result.remediation or "")


class TestRunscRuntimeArgsEdgeCases:
    def test_nonzero_exit_returns_warn(self) -> None:
        from core.doctor import check_runsc_runtimeargs

        with patch("core.dispatch.invoke", side_effect=_exec_error()):
            result = check_runsc_runtimeargs("sandbox", None)
            assert result.status == "warn"
            assert "Could not query" in result.detail

    def test_json_decode_error_returns_warn(self) -> None:
        from core.doctor import check_runsc_runtimeargs

        with patch("core.dispatch.invoke", return_value=_ok("{{INVALID}}")):
            result = check_runsc_runtimeargs("sandbox", None)
            assert result.status == "warn"
            assert "parse" in result.detail.lower()


class TestAuthModeThreadedToDispatch:
    """The checks delegate boundary-prefix construction to ``core.dispatch.invoke``;
    the doctor-level contract is that the auth mode + user are threaded into the
    ``HostConfig`` ``invoke`` receives (the sudo/polkit prefix shape itself is
    asserted in ``tests/unit/core/test_dispatch.py``, not here)."""

    def test_machinectl_reachable_threads_auth_mode_into_host_config(self) -> None:
        from core.doctor import check_machinectl_reachable
        from core.host_config import MachinectlAuth

        captured: dict[str, Any] = {}

        def capture(op: str, args: Any, host_config: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            captured["op"] = op
            captured["host_config"] = host_config
            captured["timeout"] = kwargs.get("timeout")
            return _ok("ok\n")

        with patch("core.dispatch.invoke", side_effect=capture):
            result = check_machinectl_reachable("sandbox", None, auth_mode=MachinectlAuth.POLKIT)

        assert result.status == "pass"
        assert captured["op"] == "auth-probe"
        assert captured["timeout"] == 10
        assert captured["host_config"].host.docker_unprivileged_user == "sandbox"
        assert captured["host_config"].host.machinectl_authentication == MachinectlAuth.POLKIT

    def test_machinectl_reachable_sudo_mode_host_config(self) -> None:
        from core.doctor import check_machinectl_reachable
        from core.host_config import MachinectlAuth

        captured: dict[str, Any] = {}

        def capture(op: str, args: Any, host_config: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            captured["host_config"] = host_config
            return _ok("ok\n")

        with patch("core.dispatch.invoke", side_effect=capture):
            check_machinectl_reachable("sandbox", None, auth_mode=MachinectlAuth.SUDO)

        assert captured["host_config"].host.machinectl_authentication == MachinectlAuth.SUDO

    def test_polkit_timeout_remediation_mentions_polkit(self) -> None:
        from core.doctor import check_machinectl_reachable
        from core.host_config import MachinectlAuth

        with patch("core.dispatch.invoke", side_effect=_exec_error(timeout=True)):
            result = check_machinectl_reachable("sandbox", None, auth_mode=MachinectlAuth.POLKIT)

        assert result.status == "fail"
        assert "polkit" in (result.remediation or "").lower()
        assert "sudoers" not in (result.remediation or "").lower()

    def test_sudo_timeout_remediation_mentions_sudoers(self) -> None:
        from core.doctor import check_machinectl_reachable
        from core.host_config import MachinectlAuth

        with patch("core.dispatch.invoke", side_effect=_exec_error(timeout=True)):
            result = check_machinectl_reachable("sandbox", None, auth_mode=MachinectlAuth.SUDO)

        assert result.status == "fail"
        assert "sudoers" in (result.remediation or "").lower()

    def test_docker_available_threads_auth_mode_into_host_config(self) -> None:
        from core.doctor import check_docker_available
        from core.host_config import MachinectlAuth

        captured: dict[str, Any] = {}

        def capture(op: str, args: Any, host_config: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            captured["host_config"] = host_config
            return _ok("24.0.7\n")

        with patch("core.dispatch.invoke", side_effect=capture):
            check_docker_available("sandbox", None, auth_mode=MachinectlAuth.POLKIT)

        assert captured["host_config"].host.machinectl_authentication == MachinectlAuth.POLKIT


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
            raise _exec_error(timeout=True)

        monkeypatch.setattr("core.dispatch.invoke", boom)
        result = check_compose_project_name_collision("u", None)
        assert result.status == "skip"
        assert "timed out" in result.detail

    def test_skip_on_nonzero_exit(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_compose_project_name_collision

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True)
        (state / "instances.json").write_text(json.dumps({"foo": {"instance_dir": "/x"}}))

        def boom(*a: Any, **k: Any) -> Any:
            raise _exec_error()

        monkeypatch.setattr("core.dispatch.invoke", boom)
        out = check_compose_project_name_collision("u", None)
        assert out.status == "skip"
        assert "failed" in out.detail

    def test_skip_on_unparseable_output(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_compose_project_name_collision

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True)
        (state / "instances.json").write_text(json.dumps({"foo": {"instance_dir": "/x"}}))

        monkeypatch.setattr("core.dispatch.invoke", lambda *a, **k: _ok("not-json"))
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

        captured: dict[str, Any] = {}

        def capture(op: str, args: Any, host_config: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            captured["op"] = op
            captured["timeout"] = kwargs.get("timeout")
            return _ok("[]")

        monkeypatch.setattr("core.dispatch.invoke", capture)
        out = check_compose_project_name_collision("u", None)
        assert out.status == "pass"
        assert captured["op"] == "compose-ls"
        assert captured["timeout"] == 15
