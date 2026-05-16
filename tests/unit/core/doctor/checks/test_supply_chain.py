"""Tests for core.doctor.checks.supply_chain.

Covers `check_image_digests` IMAGE_REGISTRY pin verification.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import patch

from core.exceptions import SandboxExecutionError


def _ok(stdout: str = "{}") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _exec_error(*, timeout: bool = False) -> SandboxExecutionError:
    """The ``SandboxExecutionError`` ``core.dispatch.invoke`` raises (the sterile
    Executor chains the originating cause via ``raise ... from e``; the check
    discriminates timeout-vs-stale on ``exc.__cause__``)."""
    err = SandboxExecutionError("[FATAL] Sandbox Execution Fault")
    if timeout:
        err.__cause__ = subprocess.TimeoutExpired(cmd="dispatch", timeout=2)
    else:
        err.__cause__ = subprocess.CalledProcessError(returncode=1, cmd="dispatch")
    return err


def test_module_exposes_image_digests_check() -> None:
    from core.doctor.checks import supply_chain

    assert set(supply_chain.__all__) == {"check_image_digests"}


def test_public_re_export_resolves_to_topic_module() -> None:
    import core.doctor as doctor_pkg
    from core.doctor.checks import supply_chain

    assert doctor_pkg.check_image_digests is supply_chain.check_image_digests


class TestCheckImageDigests:
    def test_all_digests_resolvable_pass(self) -> None:
        from core.doctor import check_image_digests

        with (
            patch("core.dispatch.invoke", return_value=_ok()) as inv,
            patch("subprocess.run", return_value=_ok()),
        ):
            result = check_image_digests("sandbox", None)
            assert result.status == "pass"
            assert "8" in result.detail
            # One docker-manifest-inspect op per IMAGE_REGISTRY pin, arg = pin.pinned.
            (op, args, _hc), kw = inv.call_args
            assert op == "docker-manifest-inspect"
            assert "@sha256:" in args[0]
            assert kw["timeout"] == 2

    def test_stale_digest_detected_fail(self) -> None:
        from core.doctor import check_image_digests
        from core.hydration import IMAGE_REGISTRY

        keys = list(IMAGE_REGISTRY.keys())

        def selective_invoke(op: str, args: Any, host_config: Any, **kwargs: Any) -> Any:
            if IMAGE_REGISTRY[keys[0]].digest in args[0]:
                raise _exec_error()
            return _ok()

        with (
            patch("core.dispatch.invoke", side_effect=selective_invoke),
            patch("subprocess.run", return_value=_ok()),
        ):
            result = check_image_digests("sandbox", None)
            assert result.status == "fail"
            assert keys[0] in result.detail

    def test_timeout_returns_skip(self) -> None:
        from core.doctor import check_image_digests

        with patch("core.dispatch.invoke", side_effect=_exec_error(timeout=True)):
            result = check_image_digests("sandbox", None)
            assert result.status == "skip"
            assert "registry unreachable" in result.detail.lower()

    def test_tag_drift_reports_warn(self) -> None:
        from core.doctor import check_image_digests
        from core.hydration import IMAGE_REGISTRY

        keys = list(IMAGE_REGISTRY.keys())

        def selective_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            cmd_str = " ".join(args[0]) if isinstance(args[0], list) else str(args[0])
            if f":{IMAGE_REGISTRY[keys[0]].tag}" in cmd_str:
                return subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout='{"digest": "sha256:0000000000000000000000000000000000000000000000000000000000000000"}',
                    stderr="",
                )
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

        with (
            patch("core.dispatch.invoke", return_value=_ok()),
            patch("subprocess.run", side_effect=selective_run),
        ):
            result = check_image_digests("sandbox", None)
            assert result.status in ("pass", "warn")

    def test_tag_drift_json_decode_error(self) -> None:
        from core.doctor import check_image_digests

        with (
            patch("core.dispatch.invoke", return_value=_ok()),
            patch("subprocess.run", return_value=_ok("NOT-JSON{{")),
        ):
            result = check_image_digests("sandbox", None)
            assert result.status == "pass"

    def test_tag_drift_timeout_ignored(self) -> None:
        from core.doctor import check_image_digests

        with (
            patch("core.dispatch.invoke", return_value=_ok()),
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=2)),
        ):
            result = check_image_digests("sandbox", None)
            assert result.status == "pass"

    def test_auth_mode_threaded_into_host_config(self) -> None:
        from core.doctor import check_image_digests
        from core.host_config import MachinectlAuth

        captured: dict[str, Any] = {}

        def capture(op: str, args: Any, host_config: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            captured["host_config"] = host_config
            return _ok()

        with (
            patch("core.dispatch.invoke", side_effect=capture),
            patch("subprocess.run", return_value=_ok()),
        ):
            check_image_digests("sandbox", None, auth_mode=MachinectlAuth.POLKIT)

        assert captured["host_config"].host.docker_unprivileged_user == "sandbox"
        assert captured["host_config"].host.machinectl_authentication == MachinectlAuth.POLKIT

    def test_tag_drift_call_still_uses_machinectl_prefix(self) -> None:
        """The best-effort tag-drift probe uses ``pin.tagged`` (a ``ref:tag``,
        not a digest ref) which the ``docker-manifest-inspect`` op validator
        rejects — it deliberately stays on the ``machinectl_cmd`` path (task
        6.2 scopes only the ``pin.pinned`` callsite to the op)."""
        from core.doctor import check_image_digests
        from core.host_config import MachinectlAuth

        captured: list[list[str]] = []

        def capture(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            captured.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="{}", stderr="")

        with (
            patch("core.dispatch.invoke", return_value=_ok()),
            patch("subprocess.run", side_effect=capture),
        ):
            check_image_digests("sandbox", None, auth_mode=MachinectlAuth.POLKIT)

        assert captured
        for cmd in captured:
            assert cmd[0] == "machinectl"
            assert "sudo" not in cmd
            assert "manifest inspect" in cmd[-1]
            assert "@sha256:" not in cmd[-1]
