# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
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
from core.setup.l6_daemon_json import RESERVED_RUNTIME_KEY

# The runtime is registered under the reserved key "sandbox-ai-runsc" (F-024 —
# the doctor previously looked up the wrong literal "runsc"; these fixtures had
# encoded the same bug). Single-sourced from L6 so the test and the check agree.
_RUNSC = RESERVED_RUNTIME_KEY


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    """A successful boundary-crossing CompletedProcess (returncode 0 always — a
    non-zero inner exit surfaces as ``SandboxExecutionError`` from the sterile
    Executor, never as a returncode-bearing CompletedProcess). Used both as a
    patched ``Executor.run`` return and, wrapped by :func:`_okn`, as the
    ``_invoke_with_nonce`` ``(cp, nonce)`` return."""
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _okn(stdout: str = "") -> tuple[subprocess.CompletedProcess[str], None]:
    """A successful ``core.dispatch._invoke_with_nonce`` return.

    Doctor checks reach the boundary through ``dispatch.probe`` →
    :func:`core.dispatch._invoke_with_nonce`, which returns ``(cp, nonce)`` — the
    per-crossing preflight nonce, ``None`` for these single-op (non-preflight)
    checks (H-1)."""
    return _ok(stdout), None


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


def test_module_exposes_check_and_interpret_functions() -> None:
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
        # C-009 D6: the preflight-bundle interpret seams used by cli-start/init.
        "PreflightGate",
        "evaluate_preflight_gate",
        "interpret_compose_collision_segment",
        "interpret_preflight_bundle",
        "interpret_preflight_reachability",
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
        from core.doctor.types import BINARY_PACKAGES

        with patch("shutil.which", return_value=None):
            result = check_tlog("sandbox", "debian")
            assert result.status == "fail"
            assert result.name == "tlog"
            assert result.detail == "tlog-rec not found on PATH"
            assert result.remediation is not None
            assert BINARY_PACKAGES["tlog"] in result.remediation


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
    def test_reachable_success(self, monkeypatch: Any) -> None:
        from core.doctor import check_machinectl_reachable

        captured: dict[str, Any] = {}

        def capture(
            op: str, args: Any, host_config: Any, **kwargs: Any
        ) -> tuple[subprocess.CompletedProcess[str], None]:
            captured["op"] = op
            captured["args"] = args
            captured["timeout"] = kwargs.get("timeout")
            return _okn("ok\n")

        monkeypatch.setattr("core.dispatch._invoke_with_nonce", capture)
        result = check_machinectl_reachable("sandbox", None)
        assert result.status == "pass"
        assert captured["op"] == "auth-probe"
        assert list(captured["args"]) == []
        assert captured["timeout"] == 10

    def test_reachable_timeout(self, monkeypatch: Any) -> None:
        from core.doctor import check_machinectl_reachable

        def boom(*a: Any, **k: Any) -> Any:
            raise _exec_error(timeout=True)

        monkeypatch.setattr("core.dispatch._invoke_with_nonce", boom)
        result = check_machinectl_reachable("sandbox", None)
        assert result.status == "fail"
        assert "timeout" in result.detail.lower() or "sudoers" in (result.remediation or "").lower()

    def test_reachable_nonzero_exit(self, monkeypatch: Any) -> None:
        from core.doctor import check_machinectl_reachable

        def boom(*a: Any, **k: Any) -> Any:
            raise _exec_error()

        monkeypatch.setattr("core.dispatch._invoke_with_nonce", boom)
        result = check_machinectl_reachable("sandbox", None)
        assert result.status == "fail"
        # Restored pre-refactor wording: the failure context (now sourced
        # from ProbeOutcome.message) is interpolated into the detail.
        assert result.detail == "Shell probe failed: [FATAL] Sandbox Execution Fault"


class TestDockerChecks:
    def test_docker_available_pass(self, monkeypatch: Any) -> None:
        from core.doctor import check_docker_available

        captured: dict[str, Any] = {}

        def capture(
            op: str, args: Any, host_config: Any, **kwargs: Any
        ) -> tuple[subprocess.CompletedProcess[str], None]:
            captured["op"] = op
            captured["args"] = args
            captured["timeout"] = kwargs.get("timeout")
            return _okn("24.0.7\n")

        monkeypatch.setattr("core.dispatch._invoke_with_nonce", capture)
        result = check_docker_available("sandbox", None)
        assert result.status == "pass"
        assert "24.0.7" in result.detail
        assert captured["op"] == "docker-version"
        assert list(captured["args"]) == []
        assert captured["timeout"] == 15

    def test_docker_available_fail(self, monkeypatch: Any) -> None:
        from core.doctor import check_docker_available

        def boom(*a: Any, **k: Any) -> Any:
            raise _exec_error()

        monkeypatch.setattr("core.dispatch._invoke_with_nonce", boom)
        result = check_docker_available("sandbox", None)
        assert result.status == "fail"

    def test_docker_available_empty_stdout_fail(self, monkeypatch: Any) -> None:
        from core.doctor import check_docker_available

        monkeypatch.setattr("core.dispatch._invoke_with_nonce", lambda *a, **k: _okn(""))
        result = check_docker_available("sandbox", None)
        assert result.status == "fail"

    def test_docker_rootless_pass(self, monkeypatch: Any) -> None:
        from core.doctor import check_docker_rootless

        captured: dict[str, Any] = {}

        def capture(
            op: str, args: Any, host_config: Any, **kwargs: Any
        ) -> tuple[subprocess.CompletedProcess[str], None]:
            captured["op"] = op
            captured["args"] = args
            captured["timeout"] = kwargs.get("timeout")
            return _okn("[name=seccomp,profile=builtin name=rootless name=cgroupns]")

        monkeypatch.setattr("core.dispatch._invoke_with_nonce", capture)
        result = check_docker_rootless("sandbox", None)
        assert result.status == "pass"
        assert captured["op"] == "docker-info"
        assert list(captured["args"]) == ["security-options"]
        assert captured["timeout"] == 15

    def test_docker_rootless_system_docker(self, monkeypatch: Any) -> None:
        from core.doctor import check_docker_rootless

        monkeypatch.setattr("core.dispatch._invoke_with_nonce", lambda *a, **k: _okn("[apparmor, seccomp]"))
        result = check_docker_rootless("sandbox", None)
        assert result.status == "fail"
        assert "rootless" in (result.remediation or "").lower()

    def test_docker_rootless_invoke_error_fail(self, monkeypatch: Any) -> None:
        from core.doctor import check_docker_rootless

        def boom(*a: Any, **k: Any) -> Any:
            raise _exec_error()

        monkeypatch.setattr("core.dispatch._invoke_with_nonce", boom)
        result = check_docker_rootless("sandbox", None)
        assert result.status == "fail"

    def test_runsc_registered_pass(self, monkeypatch: Any) -> None:
        from core.doctor import check_runsc_registered

        captured: dict[str, Any] = {}

        def capture(
            op: str, args: Any, host_config: Any, **kwargs: Any
        ) -> tuple[subprocess.CompletedProcess[str], None]:
            captured["op"] = op
            captured["args"] = args
            captured["timeout"] = kwargs.get("timeout")
            return _okn(json.dumps({_RUNSC: {}, "runc": {}}))

        monkeypatch.setattr("core.dispatch._invoke_with_nonce", capture)
        result = check_runsc_registered("sandbox", None)
        assert result.status == "pass"
        assert captured["op"] == "docker-info"
        assert list(captured["args"]) == ["runtimes"]
        assert captured["timeout"] == 15

    def test_runsc_not_registered(self, monkeypatch: Any) -> None:
        from core.doctor import check_runsc_registered

        monkeypatch.setattr("core.dispatch._invoke_with_nonce", lambda *a, **k: _okn('{"runc": {}}'))
        result = check_runsc_registered("sandbox", None)
        assert result.status == "fail"

    def test_runsc_registered_requires_reserved_key_not_bare_runsc(
        self, monkeypatch: Any
    ) -> None:
        """F-024 regression: the check requires the reserved key, NOT a runtime
        literally named 'runsc'. A daemon advertising only a bare 'runsc' must
        NOT satisfy the check — the previous code did `if "runsc" in runtimes`
        and wrongly passed (or, with the real 'sandbox-ai-runsc' key, wrongly
        failed). This pins the reserved-key contract."""
        from core.doctor import check_runsc_registered

        assert _RUNSC == "sandbox-ai-runsc"
        monkeypatch.setattr(
            "core.dispatch._invoke_with_nonce", lambda *a, **k: _okn(json.dumps({"runsc": {}, "runc": {}}))
        )
        result = check_runsc_registered("sandbox", None)
        assert result.status == "fail"

    def test_runsc_registered_invoke_error_fail(self, monkeypatch: Any) -> None:
        from core.doctor import check_runsc_registered

        def boom(*a: Any, **k: Any) -> Any:
            raise _exec_error()

        monkeypatch.setattr("core.dispatch._invoke_with_nonce", boom)
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
    def test_runsc_bad_json_output(self, monkeypatch: Any) -> None:
        from core.doctor import check_runsc_registered

        monkeypatch.setattr("core.dispatch._invoke_with_nonce", lambda *a, **k: _okn("NOT-VALID-JSON{{{"))
        result = check_runsc_registered("sandbox", None)
        assert result.status == "fail"


class TestCheckRunscRuntimeArgs:
    def test_expected_arg_present_passes_extra_args_ok(self, monkeypatch: Any) -> None:
        # Expected args are single-sourced from l6.EXPECTED_RUNTIME
        # (["--oci-seccomp", "--ignore-cgroups"]); an extra --debug-log on disk is
        # fine — only the expected set must be present.
        from core.doctor import check_runsc_runtimeargs

        docker_info = json.dumps(
            {
                _RUNSC: {
                    "path": "/usr/local/bin/runsc",
                    "runtimeArgs": ["--oci-seccomp", "--ignore-cgroups", "--debug-log=/var/log/runsc/%ID%/"],
                }
            }
        )
        captured: dict[str, Any] = {}

        def capture(
            op: str, args: Any, host_config: Any, **kwargs: Any
        ) -> tuple[subprocess.CompletedProcess[str], None]:
            captured["op"] = op
            captured["args"] = args
            captured["timeout"] = kwargs.get("timeout")
            return _okn(docker_info)

        monkeypatch.setattr("core.dispatch._invoke_with_nonce", capture)
        result = check_runsc_runtimeargs("sandbox", None)
        assert result.status == "pass"
        assert "--oci-seccomp" in result.detail
        assert captured["op"] == "docker-info"
        assert list(captured["args"]) == ["runtimes"]
        assert captured["timeout"] == 15

    def test_missing_oci_seccomp_warn(self, monkeypatch: Any) -> None:
        from core.doctor import check_runsc_runtimeargs

        docker_info = json.dumps(
            {
                _RUNSC: {
                    "path": "/usr/local/bin/runsc",
                    "runtimeArgs": ["--debug-log=/var/log/runsc/%ID%/"],
                }
            }
        )
        monkeypatch.setattr("core.dispatch._invoke_with_nonce", lambda *a, **k: _okn(docker_info))
        result = check_runsc_runtimeargs("sandbox", None)
        assert result.status == "warn"
        assert "--oci-seccomp" in result.detail

    def test_debug_log_not_expected_default_set_passes(self, monkeypatch: Any) -> None:
        # --debug-log is NOT in l6.EXPECTED_RUNTIME (it is a deferred opt-in), so
        # the default L6 config (--oci-seccomp + --ignore-cgroups) satisfies the
        # check (no false "Missing --debug-log" WARN — the F-024-pattern single-source fix).
        from core.doctor import check_runsc_runtimeargs

        docker_info = json.dumps(
            {
                _RUNSC: {
                    "path": "/usr/local/bin/runsc",
                    "runtimeArgs": ["--oci-seccomp", "--ignore-cgroups"],
                }
            }
        )
        monkeypatch.setattr("core.dispatch._invoke_with_nonce", lambda *a, **k: _okn(docker_info))
        result = check_runsc_runtimeargs("sandbox", None)
        assert result.status == "pass"
        assert "--oci-seccomp" in result.detail
        assert "--debug-log" not in result.detail

    def test_missing_ignore_cgroups_warn(self, monkeypatch: Any) -> None:
        # --ignore-cgroups is required under rootless (F-057); a config carrying only
        # --oci-seccomp is now incomplete and must WARN (locks the L6 target change).
        from core.doctor import check_runsc_runtimeargs

        docker_info = json.dumps(
            {
                _RUNSC: {
                    "path": "/usr/local/bin/runsc",
                    "runtimeArgs": ["--oci-seccomp"],
                }
            }
        )
        monkeypatch.setattr("core.dispatch._invoke_with_nonce", lambda *a, **k: _okn(docker_info))
        result = check_runsc_runtimeargs("sandbox", None)
        assert result.status == "warn"
        assert "--ignore-cgroups" in result.detail

    def test_empty_runtime_args_warn(self, monkeypatch: Any) -> None:
        from core.doctor import check_runsc_runtimeargs

        docker_info = json.dumps(
            {
                _RUNSC: {
                    "path": "/usr/local/bin/runsc",
                }
            }
        )
        monkeypatch.setattr("core.dispatch._invoke_with_nonce", lambda *a, **k: _okn(docker_info))
        result = check_runsc_runtimeargs("sandbox", None)
        assert result.status == "warn"
        assert "--oci-seccomp" in result.detail

    def test_remediation_references_daemon_json(self, monkeypatch: Any) -> None:
        from core.doctor import check_runsc_runtimeargs

        docker_info = json.dumps({_RUNSC: {"path": "/usr/local/bin/runsc"}})
        monkeypatch.setattr("core.dispatch._invoke_with_nonce", lambda *a, **k: _okn(docker_info))
        result = check_runsc_runtimeargs("sandbox", None)
        assert result.remediation is not None
        assert "~sandbox/.config/docker/daemon.json" in result.remediation


class TestCheckHostUds:
    def test_check_host_uds_none_passes(self, monkeypatch: Any) -> None:
        from core.doctor import check_host_uds

        docker_info = json.dumps(
            {
                _RUNSC: {
                    "path": "/usr/local/bin/runsc",
                    "runtimeArgs": ["--oci-seccomp", "--debug-log=/var/log/runsc/%ID%/"],
                }
            }
        )
        captured: dict[str, Any] = {}

        def capture(
            op: str, args: Any, host_config: Any, **kwargs: Any
        ) -> tuple[subprocess.CompletedProcess[str], None]:
            captured["op"] = op
            captured["args"] = args
            captured["timeout"] = kwargs.get("timeout")
            return _okn(docker_info)

        monkeypatch.setattr("core.dispatch._invoke_with_nonce", capture)
        result = check_host_uds("sandbox", None)
        assert result.status == "pass"
        assert captured["op"] == "docker-info"
        assert list(captured["args"]) == ["runtimes"]
        assert captured["timeout"] == 15

    def test_check_host_uds_all_detected_warns(self, monkeypatch: Any) -> None:
        from core.doctor import check_host_uds

        docker_info = json.dumps(
            {
                _RUNSC: {
                    "path": "/usr/local/bin/runsc",
                    "runtimeArgs": ["--oci-seccomp", "--host-uds=all"],
                }
            }
        )
        monkeypatch.setattr("core.dispatch._invoke_with_nonce", lambda *a, **k: _okn(docker_info))
        result = check_host_uds("sandbox", None)
        assert result.status == "warn"
        assert "daemon.json" in (result.remediation or "")

    def test_check_host_uds_docker_query_failure(self, monkeypatch: Any) -> None:
        from core.doctor import check_host_uds

        def boom(*a: Any, **k: Any) -> Any:
            raise _exec_error()

        monkeypatch.setattr("core.dispatch._invoke_with_nonce", boom)
        result = check_host_uds("sandbox", None)
        assert result.status == "warn"
        assert "daemon.json" in (result.remediation or "")

    def test_check_host_uds_json_parse_failure(self, monkeypatch: Any) -> None:
        from core.doctor import check_host_uds

        monkeypatch.setattr("core.dispatch._invoke_with_nonce", lambda *a, **k: _okn("NOT-JSON{{{"))
        result = check_host_uds("sandbox", None)
        assert result.status == "warn"
        assert "daemon.json" in (result.remediation or "")


class TestRunscRuntimeArgsEdgeCases:
    def test_nonzero_exit_returns_warn(self, monkeypatch: Any) -> None:
        from core.doctor import check_runsc_runtimeargs

        def boom(*a: Any, **k: Any) -> Any:
            raise _exec_error()

        monkeypatch.setattr("core.dispatch._invoke_with_nonce", boom)
        result = check_runsc_runtimeargs("sandbox", None)
        assert result.status == "warn"
        assert "Could not query" in result.detail

    def test_json_decode_error_returns_warn(self, monkeypatch: Any) -> None:
        from core.doctor import check_runsc_runtimeargs

        monkeypatch.setattr("core.dispatch._invoke_with_nonce", lambda *a, **k: _okn("{{INVALID}}"))
        result = check_runsc_runtimeargs("sandbox", None)
        assert result.status == "warn"
        assert "parse" in result.detail.lower()


class TestAuthModeThreadedToDispatch:
    """The checks delegate boundary-prefix construction to ``core.dispatch.invoke``;
    the doctor-level contract is that the user is threaded into the ``HostConfig``
    ``invoke`` receives (the sudo prefix shape itself is asserted in
    ``tests/unit/core/test_dispatch.py``, not here)."""

    def test_machinectl_reachable_threads_user_into_host_config(self, monkeypatch: Any) -> None:
        from core.doctor import check_machinectl_reachable

        captured: dict[str, Any] = {}

        def capture(
            op: str, args: Any, host_config: Any, **kwargs: Any
        ) -> tuple[subprocess.CompletedProcess[str], None]:
            captured["op"] = op
            captured["host_config"] = host_config
            captured["timeout"] = kwargs.get("timeout")
            return _okn("ok\n")

        monkeypatch.setattr("core.dispatch._invoke_with_nonce", capture)
        result = check_machinectl_reachable("sandbox", None)

        assert result.status == "pass"
        assert captured["op"] == "auth-probe"
        assert captured["timeout"] == 10
        assert captured["host_config"].host.docker_unprivileged_user == "sandbox"

    def test_sudo_timeout_remediation_mentions_sudoers(self, monkeypatch: Any) -> None:
        from core.doctor import check_machinectl_reachable

        def boom(*a: Any, **k: Any) -> Any:
            raise _exec_error(timeout=True)

        monkeypatch.setattr("core.dispatch._invoke_with_nonce", boom)
        result = check_machinectl_reachable("sandbox", None)

        assert result.status == "fail"
        assert "sudoers" in (result.remediation or "").lower()

    def test_docker_available_threads_user_into_host_config(self, monkeypatch: Any) -> None:
        from core.doctor import check_docker_available

        captured: dict[str, Any] = {}

        def capture(
            op: str, args: Any, host_config: Any, **kwargs: Any
        ) -> tuple[subprocess.CompletedProcess[str], None]:
            captured["host_config"] = host_config
            return _okn("24.0.7\n")

        monkeypatch.setattr("core.dispatch._invoke_with_nonce", capture)
        check_docker_available("sandbox", None)

        assert captured["host_config"].host.docker_unprivileged_user == "sandbox"


class TestCheckComposeProjectNameCollision:
    def test_pass_when_no_registered_instances(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_compose_project_name_collision

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True)
        (state / "instances.json").write_text("{}")
        # The empty-registry PASS verdict is now owned solely by the interpret
        # fn (the public check no longer short-circuits on an empty registry, so
        # the registry is read once). The probe outcome is irrelevant to the
        # verdict — the interpret fn returns PASS before inspecting it — but the
        # crossing still fires, so it is mocked here.
        monkeypatch.setattr("core.dispatch._invoke_with_nonce", lambda *a, **k: _okn("[]"))
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

        monkeypatch.setattr("core.dispatch._invoke_with_nonce", boom)
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

        monkeypatch.setattr("core.dispatch._invoke_with_nonce", boom)
        out = check_compose_project_name_collision("u", None)
        assert out.status == "skip"
        # Restored pre-refactor wording with the failure context from
        # ProbeOutcome.message interpolated.
        assert out.detail == "docker compose ls failed: [FATAL] Sandbox Execution Fault"

    def test_skip_on_unparseable_output(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_compose_project_name_collision

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True)
        (state / "instances.json").write_text(json.dumps({"foo": {"instance_dir": "/x"}}))

        monkeypatch.setattr("core.dispatch._invoke_with_nonce", lambda *a, **k: _okn("not-json"))
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

        def capture(
            op: str, args: Any, host_config: Any, **kwargs: Any
        ) -> tuple[subprocess.CompletedProcess[str], None]:
            captured["op"] = op
            captured["timeout"] = kwargs.get("timeout")
            return _okn("[]")

        monkeypatch.setattr("core.dispatch._invoke_with_nonce", capture)
        out = check_compose_project_name_collision("u", None)
        assert out.status == "pass"
        assert captured["op"] == "compose-ls"
        assert captured["timeout"] == 15


# ── C-005 1.4: operator-rootless local routing ───────────────────────────────


class TestOperatorRootlessLocalRouting:
    """The docker/runsc/supply/compose checks route LOCALLY in operator-rootless
    purely by receiving the mode in their ``minimal_host_config(...)`` call —
    ``dispatch.probe`` takes the local (no-machinectl) branch (C-003)."""

    def test_docker_available_host_config_is_operator_rootless(self, monkeypatch: Any) -> None:
        from core.doctor import check_docker_available
        from core.host_config import DockerExecutionMode

        captured: dict[str, Any] = {}

        def capture(
            op: str, args: Any, host_config: Any, **kwargs: Any
        ) -> tuple[subprocess.CompletedProcess[str], None]:
            captured["host_config"] = host_config
            return _okn("24.0.7\n")

        monkeypatch.setattr("core.dispatch._invoke_with_nonce", capture)
        result = check_docker_available(
            "sandbox", None, mode=DockerExecutionMode.OPERATOR_ROOTLESS
        )
        assert result.status == "pass"
        assert captured["host_config"].host.docker_execution_mode is DockerExecutionMode.OPERATOR_ROOTLESS

    def test_probe_takes_local_no_machinectl_path_in_operator_rootless(self, monkeypatch: Any) -> None:
        """End-to-end through the real ``dispatch.probe`` → ``invoke`` → local
        branch: assert the argv that reaches the Executor carries NO machinectl
        crossing and runs ``framed=False`` (the local path)."""
        from core.doctor import check_docker_available
        from core.host_config import DockerExecutionMode

        captured: dict[str, Any] = {}

        def fake_run(self: Any, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            captured["argv"] = argv
            captured["framed"] = kwargs.get("framed")
            return _ok("24.0.7\n")

        monkeypatch.setattr("core.executor.Executor.run", fake_run)
        # Silence the journald audit side-effect the local path emits.
        monkeypatch.setattr("core.dispatch.emit_op_audit", lambda *a, **k: None)
        result = check_docker_available(
            "sandbox", None, mode=DockerExecutionMode.OPERATOR_ROOTLESS
        )
        assert result.status == "pass"
        assert captured["framed"] is False
        assert "machinectl" not in captured["argv"]
        assert "shell" not in captured["argv"]

    def test_probe_crosses_dispatcher_framed_in_separate_user_sudo(self, monkeypatch: Any) -> None:
        """Regression guard: SUDO separate-user crosses via the dispatcher with
        ``framed=True``. Post-C-009 the SUDO crossing rides the privileged
        byte-pipe (``sudo systemd-run --pipe``), NOT machinectl shell — but the
        exit is still recovered from the dispatcher frame (framed=True, D3)."""
        from core.doctor import check_docker_available
        from core.host_config import DockerExecutionMode

        captured: dict[str, Any] = {}

        def fake_run(self: Any, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            captured["argv"] = argv
            captured["framed"] = kwargs.get("framed")
            return _ok("24.0.7\n")

        monkeypatch.setattr("core.executor.Executor.run", fake_run)
        result = check_docker_available(
            "sandbox", None, mode=DockerExecutionMode.SEPARATE_USER
        )
        assert result.status == "pass"
        assert captured["framed"] is True
        assert "machinectl" not in captured["argv"]
        assert captured["argv"][:5] == ["sudo", "systemd-run", "-q", "--pipe", "--uid=sandbox"]


# ── C-009 M4b-2: direct interpret-fn contract tests ──────────────────────────
#
# Each ``_interpret_<name>`` is the SINGLE interpretation source for its check
# (the check fn = ``dispatch.probe(...)`` then ``return _interpret_<name>(...)``).
# A LATER milestone feeds these the same per-op ``ProbeOutcome``s reconstructed
# from ONE ``preflight`` crossing, so these tests lock the outcome→CheckResult
# contract independent of the crossing path. The crossing-path tests above stay.


def _outcome(*, ok: bool = False, timed_out: bool = False, stdout: str = "", message: str = "") -> Any:
    """Build a synthetic ``dispatch.ProbeOutcome`` for direct interpret-fn tests."""
    from core.dispatch import ProbeOutcome

    return ProbeOutcome(ok=ok, timed_out=timed_out, stdout=stdout, message=message)


class TestInterpretMachinectlReachable:
    def test_pass(self) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_machinectl_reachable

        result = _interpret_machinectl_reachable(_outcome(ok=True), "sandbox")
        assert result.status == "pass"
        assert "sandbox@.host" in result.detail

    def test_timeout_sudo_remediation(self) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_machinectl_reachable

        result = _interpret_machinectl_reachable(_outcome(timed_out=True), "sandbox")
        assert result.status == "fail"
        assert "sudo" in (result.detail + (result.remediation or "")).lower()
        assert "sudoers" in (result.remediation or "")

    def test_not_ok_interpolates_message(self) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_machinectl_reachable

        result = _interpret_machinectl_reachable(_outcome(message="boom"), "sandbox")
        assert result.status == "fail"
        assert "boom" in result.detail


class TestInterpretDockerAvailable:
    def test_pass(self) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_docker_available

        result = _interpret_docker_available(_outcome(ok=True, stdout="24.0.7\n"), "sandbox")
        assert result.status == "pass"
        assert "24.0.7" in result.detail

    def test_empty_stdout_fails(self) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_docker_available

        result = _interpret_docker_available(_outcome(ok=True, stdout="   "), "sandbox")
        assert result.status == "fail"

    def test_not_ok_fails(self) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_docker_available

        result = _interpret_docker_available(_outcome(message="x"), "sandbox")
        assert result.status == "fail"


class TestInterpretDockerRootless:
    def test_pass(self) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_docker_rootless

        result = _interpret_docker_rootless(_outcome(ok=True, stdout="[name=rootless]"), "sandbox")
        assert result.status == "pass"

    def test_not_rootless_fails(self) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_docker_rootless

        result = _interpret_docker_rootless(_outcome(ok=True, stdout="[name=seccomp]"), "sandbox")
        assert result.status == "fail"

    def test_not_ok_fails(self) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_docker_rootless

        result = _interpret_docker_rootless(_outcome(message="x"), "sandbox")
        assert result.status == "fail"


class TestInterpretRunscRegistered:
    def test_pass(self) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_runsc_registered

        result = _interpret_runsc_registered(_outcome(ok=True, stdout=json.dumps({_RUNSC: {}})))
        assert result.status == "pass"

    def test_missing_key_fails(self) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_runsc_registered

        result = _interpret_runsc_registered(_outcome(ok=True, stdout=json.dumps({"runsc": {}})))
        assert result.status == "fail"

    def test_bad_json_fails(self) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_runsc_registered

        result = _interpret_runsc_registered(_outcome(ok=True, stdout="not-json"))
        assert result.status == "fail"

    def test_not_ok_fails(self) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_runsc_registered

        result = _interpret_runsc_registered(_outcome(message="x"))
        assert result.status == "fail"


class TestInterpretRunscRuntimeargs:
    def test_pass(self) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_runsc_runtimeargs

        stdout = json.dumps({_RUNSC: {"runtimeArgs": ["--oci-seccomp", "--ignore-cgroups"]}})
        result = _interpret_runsc_runtimeargs(_outcome(ok=True, stdout=stdout), "sandbox")
        assert result.status == "pass"

    def test_missing_arg_warns(self) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_runsc_runtimeargs

        stdout = json.dumps({_RUNSC: {"runtimeArgs": ["--oci-seccomp"]}})
        result = _interpret_runsc_runtimeargs(_outcome(ok=True, stdout=stdout), "sandbox")
        assert result.status == "warn"
        assert "--ignore-cgroups" in result.detail

    def test_bad_json_warns(self) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_runsc_runtimeargs

        result = _interpret_runsc_runtimeargs(_outcome(ok=True, stdout="garbage"), "sandbox")
        assert result.status == "warn"
        assert "parse" in result.detail

    def test_not_ok_warns(self) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_runsc_runtimeargs

        result = _interpret_runsc_runtimeargs(_outcome(message="x"), "sandbox")
        assert result.status == "warn"
        assert "query" in result.detail


class TestInterpretHostUds:
    def test_none_passes(self) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_host_uds

        stdout = json.dumps({_RUNSC: {"runtimeArgs": ["--oci-seccomp"]}})
        result = _interpret_host_uds(_outcome(ok=True, stdout=stdout), "sandbox")
        assert result.status == "pass"

    def test_all_warns(self) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_host_uds

        stdout = json.dumps({_RUNSC: {"runtimeArgs": ["--host-uds=all"]}})
        result = _interpret_host_uds(_outcome(ok=True, stdout=stdout), "sandbox")
        assert result.status == "warn"
        assert "--host-uds=all" in result.detail

    def test_bad_json_warns(self) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_host_uds

        result = _interpret_host_uds(_outcome(ok=True, stdout="garbage"), "sandbox")
        assert result.status == "warn"
        assert "parse" in result.detail

    def test_not_ok_warns(self) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_host_uds

        result = _interpret_host_uds(_outcome(message="x"), "sandbox")
        assert result.status == "warn"
        assert "query" in result.detail


class TestInterpretComposeProjectNameCollision:
    """The collision interpret fn is self-contained — it reads the registry
    internally, so it takes ONLY the outcome (the next milestone feeds it a
    ``compose-ls``-derived outcome)."""

    def test_no_registered_instances_passes(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_compose_project_name_collision

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True)
        (state / "instances.json").write_text("{}")
        result = _interpret_compose_project_name_collision(_outcome(ok=True, stdout="[]"))
        assert result.status == "pass"
        assert "no registered" in result.detail

    def test_timeout_skips(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_compose_project_name_collision

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True)
        (state / "instances.json").write_text(json.dumps({"foo": {"instance_dir": "/x"}}))
        result = _interpret_compose_project_name_collision(_outcome(timed_out=True))
        assert result.status == "skip"
        assert "timed out" in result.detail

    def test_not_ok_skips_with_message(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_compose_project_name_collision

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True)
        (state / "instances.json").write_text(json.dumps({"foo": {"instance_dir": "/x"}}))
        result = _interpret_compose_project_name_collision(_outcome(message="boom"))
        assert result.status == "skip"
        assert result.detail == "docker compose ls failed: boom"

    def test_unparseable_skips(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_compose_project_name_collision

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True)
        (state / "instances.json").write_text(json.dumps({"foo": {"instance_dir": "/x"}}))
        result = _interpret_compose_project_name_collision(_outcome(ok=True, stdout="not-json"))
        assert result.status == "skip"
        assert "parse" in result.detail

    def test_clean_daemon_passes(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_compose_project_name_collision

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True)
        (state / "instances.json").write_text(json.dumps({"foo": {"instance_dir": "/x"}}))
        result = _interpret_compose_project_name_collision(_outcome(ok=True, stdout="[]"))
        assert result.status == "pass"
        assert "registered instance" in result.detail


# ─── content-trust hardening (C-009 Pass 2 — M-2 size-bound + strict shape) ───


class TestSafeLoadJson:
    """The shared size-bound + strict-shape defensive parse helper (M-2)."""

    def test_oversized_dict_fails_closed(self) -> None:
        from core.doctor.checks.privilege_boundary import _MAX_DAEMON_JSON_BYTES, _safe_load_json

        # A well-formed JSON object that exceeds the byte ceiling → None (never parsed).
        big = json.dumps({_RUNSC: {"runtimeArgs": ["x" * (_MAX_DAEMON_JSON_BYTES + 10)]}})
        assert len(big.encode()) > _MAX_DAEMON_JSON_BYTES
        assert _safe_load_json(big, dict) is None

    def test_oversized_list_fails_closed(self) -> None:
        from core.doctor.checks.privilege_boundary import _MAX_DAEMON_JSON_BYTES, _safe_load_json

        big = json.dumps([{"Name": "p" * (_MAX_DAEMON_JSON_BYTES + 10)}])
        assert _safe_load_json(big, list) is None

    def test_wrong_type_fails_closed(self) -> None:
        from core.doctor.checks.privilege_boundary import _safe_load_json

        # A valid JSON list when a dict is expected (and vice-versa) → None.
        assert _safe_load_json("[]", dict) is None
        assert _safe_load_json("{}", list) is None

    def test_malformed_fails_closed(self) -> None:
        from core.doctor.checks.privilege_boundary import _safe_load_json

        assert _safe_load_json("not-json", dict) is None

    def test_well_formed_returns_value(self) -> None:
        from core.doctor.checks.privilege_boundary import _safe_load_json

        assert _safe_load_json('{"a": 1}', dict) == {"a": 1}
        assert _safe_load_json("[1, 2]", list) == [1, 2]


class TestContentTrustFailClosed:
    """Each untrusted-daemon-JSON site fails closed on oversized / wrong-shape."""

    def test_rootless_substring_does_not_falsely_pass(self) -> None:
        """L-1: a non-``name=rootless`` security-options string that merely
        *contains* the substring ``rootless`` (e.g. a ``rootlesskit`` mention or
        a path) must NOT falsely PASS — only the structural ``name=rootless``
        token does.

        Pre-fix verification protocol (CLAUDE.md): against the pre-change
        ``"rootless" in outcome.stdout`` substring test this assertion was RED —
        the forged ``[name=seccomp,profile=/run/rootlesskit/builtin …]`` string
        falsely returned ``status == "pass"`` (observed: ``assert 'pass' ==
        'fail'``). The structural ``name=rootless`` token match makes it fail
        closed, proving the test catches the spoofed-PASS vector.
        """
        from core.doctor.checks.privilege_boundary import _interpret_docker_rootless

        # name=seccomp (no rootless option) plus a stray ``rootlesskit`` substring.
        stdout = "[name=seccomp,profile=/run/rootlesskit/builtin name=cgroupns]"
        result = _interpret_docker_rootless(_outcome(ok=True, stdout=stdout), "sandbox")
        assert result.status == "fail"

    def test_rootless_structural_token_passes(self) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_docker_rootless

        stdout = "[name=seccomp,profile=builtin name=rootless name=cgroupns]"
        result = _interpret_docker_rootless(_outcome(ok=True, stdout=stdout), "sandbox")
        assert result.status == "pass"

    def _oversized_runtimes(self) -> str:
        from core.doctor.checks.privilege_boundary import _MAX_DAEMON_JSON_BYTES

        return json.dumps({_RUNSC: {"runtimeArgs": ["x" * (_MAX_DAEMON_JSON_BYTES + 10)]}})

    def test_runsc_registered_oversized_fails_closed(self) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_runsc_registered

        result = _interpret_runsc_registered(_outcome(ok=True, stdout=self._oversized_runtimes()))
        assert result.status == "fail"

    def test_runsc_registered_non_dict_entry_fails(self) -> None:
        """A ``name=rootless``-shaped forgery where the runsc value is not a dict
        (e.g. a bare string) must NOT pass — PASS requires a well-formed entry."""
        from core.doctor.checks.privilege_boundary import _interpret_runsc_registered

        stdout = json.dumps({_RUNSC: "forged"})
        result = _interpret_runsc_registered(_outcome(ok=True, stdout=stdout))
        assert result.status == "fail"

    def test_runsc_registered_list_shape_fails(self) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_runsc_registered

        result = _interpret_runsc_registered(_outcome(ok=True, stdout="[]"))
        assert result.status == "fail"

    def test_runtimeargs_oversized_warns(self) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_runsc_runtimeargs

        result = _interpret_runsc_runtimeargs(_outcome(ok=True, stdout=self._oversized_runtimes()), "sandbox")
        assert result.status == "warn"
        assert "parse" in result.detail

    def test_runtimeargs_list_shape_warns(self) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_runsc_runtimeargs

        result = _interpret_runsc_runtimeargs(_outcome(ok=True, stdout="[]"), "sandbox")
        assert result.status == "warn"

    def test_runtimeargs_non_dict_entry_warns_missing(self) -> None:
        """A non-dict runsc entry yields empty args → the expected args are missing → WARN."""
        from core.doctor.checks.privilege_boundary import _interpret_runsc_runtimeargs

        stdout = json.dumps({_RUNSC: "forged"})
        result = _interpret_runsc_runtimeargs(_outcome(ok=True, stdout=stdout), "sandbox")
        assert result.status == "warn"

    def test_host_uds_oversized_warns(self) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_host_uds

        result = _interpret_host_uds(_outcome(ok=True, stdout=self._oversized_runtimes()), "sandbox")
        assert result.status == "warn"
        assert "parse" in result.detail

    def test_host_uds_list_shape_warns(self) -> None:
        from core.doctor.checks.privilege_boundary import _interpret_host_uds

        result = _interpret_host_uds(_outcome(ok=True, stdout="[]"), "sandbox")
        assert result.status == "warn"

    def test_host_uds_non_dict_entry_passes_default(self) -> None:
        """A non-dict runsc entry → empty args → ``--host-uds=all`` absent → PASS (default none)."""
        from core.doctor.checks.privilege_boundary import _interpret_host_uds

        stdout = json.dumps({_RUNSC: "forged"})
        result = _interpret_host_uds(_outcome(ok=True, stdout=stdout), "sandbox")
        assert result.status == "pass"

    def test_compose_collision_oversized_skips(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor.checks.privilege_boundary import (
            _MAX_DAEMON_JSON_BYTES,
            _interpret_compose_project_name_collision,
        )

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True)
        (state / "instances.json").write_text(json.dumps({"foo": {"instance_dir": "/x"}}))
        big = json.dumps([{"Name": "p" * (_MAX_DAEMON_JSON_BYTES + 10)}])
        result = _interpret_compose_project_name_collision(_outcome(ok=True, stdout=big))
        assert result.status == "skip"
        assert "parse" in result.detail

    def test_compose_collision_dict_shape_skips(self, isolated_sandbox_ai_home: Any) -> None:
        """A JSON object where a list was expected → fail closed (skip)."""
        from core.doctor.checks.privilege_boundary import _interpret_compose_project_name_collision

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True)
        (state / "instances.json").write_text(json.dumps({"foo": {"instance_dir": "/x"}}))
        result = _interpret_compose_project_name_collision(_outcome(ok=True, stdout="{}"))
        assert result.status == "skip"
        assert "parse" in result.detail


# ─── preflight-bundle interpret seams (C-009 D6 — cli-start / init wiring) ────


def _bundle_outcome(segments: dict[str, tuple[str, int]], *, ok: bool = True) -> Any:
    """A ``preflight``-bundle :class:`ProbeOutcome` from {name: (stdout, rc)}.

    Mirrors ``core.dispatch._preflight_inner``'s on-the-wire marker shape so the
    seam fns exercise the real :func:`core.dispatch.parse_preflight_outcome`
    split (not a stubbed dict).
    """
    from core.dispatch import ProbeOutcome

    nonce = "feedface00c0ffee"
    parts = [
        f"__PREFLIGHT_Q_{nonce}_{name}__\n{body}\n__PREFLIGHT_RC_{nonce}_{name}_{rc}__"
        for name, (body, rc) in segments.items()
    ]
    return ProbeOutcome(ok=ok, timed_out=False, stdout="\n".join(parts), message="", preflight_nonce=nonce)


_ALL_OK_SEGMENTS = {
    "auth-probe": ("ok", 0),
    "docker-version": ("29.5.3", 0),
    "docker-info-security-options": ("[name=rootless]", 0),
    "docker-info-runtimes": (json.dumps({_RUNSC: {"runtimeArgs": ["--oci-seccomp", "--ignore-cgroups"]}}), 0),
    "compose-ls": ("[]", 0),
}


class TestInterpretPreflightReachability:
    def test_pass_outcome_passes(self) -> None:
        from core.doctor import interpret_preflight_reachability

        result = interpret_preflight_reachability(_outcome(ok=True), "sandbox")
        assert result.status == "pass"
        assert result.name == "boundary reachable"

    def test_failed_outcome_fails_with_reachability_message(self) -> None:
        from core.doctor import interpret_preflight_reachability

        result = interpret_preflight_reachability(
            _outcome(ok=False, message="boom"), "sandbox"
        )
        assert result.status == "fail"
        assert "boom" in result.detail

    def test_timeout_outcome_fails(self) -> None:
        from core.doctor import interpret_preflight_reachability

        result = interpret_preflight_reachability(
            _outcome(timed_out=True), "sandbox"
        )
        assert result.status == "fail"
        assert "sudoers" in (result.remediation or "").lower() or "timed out" in result.detail.lower()


class TestInterpretComposeCollisionSegment:
    def test_ok_segment_delegates(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import interpret_compose_collision_segment

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True)
        (state / "instances.json").write_text(json.dumps({"foo": {"instance_dir": "/x"}}))
        result = interpret_compose_collision_segment(_outcome(ok=True, stdout="[]"))
        assert result.status == "pass"

    def test_none_segment_skips_gracefully(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import interpret_compose_collision_segment

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True)
        (state / "instances.json").write_text(json.dumps({"foo": {"instance_dir": "/x"}}))
        result = interpret_compose_collision_segment(None)
        assert result.status == "skip"


class TestEvaluatePreflightGate:
    """F3: the once-parsed reachability gate shared by ``start`` and ``init``."""

    def test_reachable_bundle_parses_once_and_passes_gate(self) -> None:
        from core.doctor import evaluate_preflight_gate

        gate = evaluate_preflight_gate(_bundle_outcome(_ALL_OK_SEGMENTS))
        assert gate.reachable is True
        # ``per_op`` carries the parsed segments for ``interpret_preflight_bundle``.
        assert gate.per_op["auth-probe"].ok is True
        assert set(gate.per_op) >= {
            "auth-probe",
            "docker-version",
            "docker-info-security-options",
            "docker-info-runtimes",
            "compose-ls",
        }

    def test_timed_out_crossing_fails_gate_with_whole_outcome(self) -> None:
        from core.dispatch import ProbeOutcome
        from core.doctor import evaluate_preflight_gate

        timed_out = ProbeOutcome(ok=False, timed_out=True, stdout="", message="timed out")
        gate = evaluate_preflight_gate(timed_out)
        assert gate.reachable is False
        # The whole-crossing outcome (carries the timeout message) is the most
        # specific failing outcome.
        assert gate.reach_outcome is timed_out

    def test_failed_crossing_fails_gate_with_whole_outcome(self) -> None:
        from core.dispatch import ProbeOutcome
        from core.doctor import evaluate_preflight_gate

        failed = ProbeOutcome(ok=False, timed_out=False, stdout="", message="op exit 1")
        gate = evaluate_preflight_gate(failed)
        assert gate.reachable is False
        assert gate.reach_outcome is failed

    def test_ok_crossing_but_auth_probe_segment_not_ok_fails_gate(self) -> None:
        from core.doctor import evaluate_preflight_gate

        segments = dict(_ALL_OK_SEGMENTS)
        segments["auth-probe"] = ("shell probe failed", 1)
        gate = evaluate_preflight_gate(_bundle_outcome(segments))
        assert gate.reachable is False
        # The most specific failing outcome is the parsed (not-ok) auth-probe
        # segment, not the whole (ok) crossing.
        assert gate.reach_outcome.ok is False
        assert gate.reach_outcome is gate.per_op["auth-probe"]

    def test_ok_crossing_garbled_bundle_uses_not_ok_auth_probe_segment(self) -> None:
        from core.dispatch import ProbeOutcome
        from core.doctor import evaluate_preflight_gate

        # An ok crossing whose bundle is garbled (no markers) → ``parse`` returns
        # a not-ok ``auth-probe`` segment (still present in the map). The gate
        # fails and feeds that segment as the most-specific failing outcome.
        outcome = ProbeOutcome(ok=True, timed_out=False, stdout="garbled", message="", preflight_nonce="deadbeef")
        gate = evaluate_preflight_gate(outcome)
        assert gate.reachable is False
        assert gate.reach_outcome.ok is False
        assert gate.reach_outcome is gate.per_op["auth-probe"]


class TestInterpretPreflightBundle:
    def test_all_ok_produces_seven_verdicts_in_chain_order(self, isolated_sandbox_ai_home: Any) -> None:
        from core.dispatch import parse_preflight_outcome
        from core.doctor import interpret_preflight_bundle

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True)
        (state / "instances.json").write_text("{}")
        per_op = parse_preflight_outcome(_bundle_outcome(_ALL_OK_SEGMENTS))
        results = interpret_preflight_bundle(per_op, "sandbox")
        names = [r.name for r in results]
        assert names == [
            "boundary reachable",
            "Docker available",
            "Docker rootless",
            "gVisor runsc",
            "runsc runtimeArgs",
            "--host-uds=none",
            "compose project name collision",
        ]
        assert all(r.status in ("pass", "warn") for r in results)

    def test_runtimes_segment_feeds_three_checks(self, isolated_sandbox_ai_home: Any) -> None:
        # A runtimes segment WITHOUT the reserved key fails runsc-registered and
        # warns the two derived checks — proving all three derive from the single
        # deduped segment.
        from core.dispatch import parse_preflight_outcome
        from core.doctor import interpret_preflight_bundle

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True)
        (state / "instances.json").write_text("{}")
        segments = dict(_ALL_OK_SEGMENTS)
        segments["docker-info-runtimes"] = (json.dumps({"runc": {}}), 0)
        per_op = parse_preflight_outcome(_bundle_outcome(segments))
        results = interpret_preflight_bundle(per_op, "sandbox")
        by_name = {r.name: r for r in results}
        assert by_name["gVisor runsc"].status == "fail"
        # runtimeArgs + host-uds derive from the same segment (no reserved key →
        # empty args): host-uds passes (no --host-uds=all), runtimeArgs warns.
        assert by_name["runsc runtimeArgs"].status == "warn"
        assert by_name["--host-uds=none"].status == "pass"

    def test_single_failing_segment_surfaces_its_own_verdict(self, isolated_sandbox_ai_home: Any) -> None:
        # The docker-version query fails (rc!=0) → "Docker available" fails with
        # ITS specific diagnostic, not a generic bundle failure.
        from core.dispatch import parse_preflight_outcome
        from core.doctor import interpret_preflight_bundle

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True)
        (state / "instances.json").write_text("{}")
        segments = dict(_ALL_OK_SEGMENTS)
        segments["docker-version"] = ("Cannot connect to the Docker daemon", 1)
        per_op = parse_preflight_outcome(_bundle_outcome(segments))
        results = interpret_preflight_bundle(per_op, "sandbox")
        by_name = {r.name: r for r in results}
        assert by_name["Docker available"].status == "fail"
        assert "Docker not reachable" in by_name["Docker available"].detail
        # the reachability segment was fine, so machinectl-reachable still passes
        assert by_name["boundary reachable"].status == "pass"
