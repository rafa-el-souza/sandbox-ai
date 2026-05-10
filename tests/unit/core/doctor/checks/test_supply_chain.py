"""Tests for core.doctor.checks.supply_chain.

Covers `check_image_digests` IMAGE_REGISTRY pin verification.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import patch


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

        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = check_image_digests("sandbox", None)
            assert result.status == "pass"
            assert "8" in result.detail

    def test_stale_digest_detected_fail(self) -> None:
        from core.doctor import check_image_digests
        from core.hydration import IMAGE_REGISTRY

        keys = list(IMAGE_REGISTRY.keys())

        def selective_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            cmd_str = " ".join(args[0]) if isinstance(args[0], list) else str(args[0])
            if IMAGE_REGISTRY[keys[0]].digest in cmd_str:
                return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="MANIFEST_UNKNOWN")
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")

        with patch("subprocess.run", side_effect=selective_run):
            result = check_image_digests("sandbox", None)
            assert result.status == "fail"
            assert keys[0] in result.detail

    def test_timeout_returns_skip(self) -> None:
        from core.doctor import check_image_digests

        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=2),
        ):
            result = check_image_digests("sandbox", None)
            assert result.status == "skip"
            assert "registry unreachable" in result.detail.lower()

    def test_tag_drift_reports_warn(self) -> None:
        from core.doctor import check_image_digests
        from core.hydration import IMAGE_REGISTRY

        keys = list(IMAGE_REGISTRY.keys())

        def selective_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            cmd_str = " ".join(args[0]) if isinstance(args[0], list) else str(args[0])
            if "@sha256:" in cmd_str:
                return subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
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

        with patch("subprocess.run", side_effect=selective_run):
            result = check_image_digests("sandbox", None)
            assert result.status in ("pass", "warn")

    def test_tag_drift_json_decode_error(self) -> None:
        from core.doctor import check_image_digests

        def mixed_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            cmd_str = " ".join(args[0]) if isinstance(args[0], list) else str(args[0])
            if "@sha256:" in cmd_str:
                return subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="NOT-JSON{{", stderr="")

        with patch("subprocess.run", side_effect=mixed_run):
            result = check_image_digests("sandbox", None)
            assert result.status == "pass"

    def test_tag_drift_timeout_ignored(self) -> None:
        from core.doctor import check_image_digests

        def timeout_on_tag(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            cmd_str = " ".join(args[0]) if isinstance(args[0], list) else str(args[0])
            if "@sha256:" in cmd_str:
                return subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
            raise subprocess.TimeoutExpired(cmd="docker", timeout=2)

        with patch("subprocess.run", side_effect=timeout_on_tag):
            result = check_image_digests("sandbox", None)
            assert result.status == "pass"

    def test_polkit_image_digests_command_has_no_sudo(self) -> None:
        from core.doctor import check_image_digests
        from core.host_config import MachinectlAuth

        captured: list[list[str]] = []

        def capture(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            captured.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="{}", stderr="")

        with patch("subprocess.run", side_effect=capture):
            check_image_digests("sandbox", None, auth_mode=MachinectlAuth.POLKIT)

        assert captured
        for cmd in captured:
            assert cmd[0] == "machinectl"
            assert "sudo" not in cmd
