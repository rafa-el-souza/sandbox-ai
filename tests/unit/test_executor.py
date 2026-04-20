import os
import subprocess
from unittest.mock import patch

import pytest
from core.exceptions import SandboxExecutionError
from core.executor import Executor


def test_executor_runs_synchronously_with_default_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = Executor()
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=["ls"], returncode=0, stdout="output", stderr="")

        result = executor.run(["ls"])

        mock_run.assert_called_once_with(
            ["ls"],
            capture_output=True,
            check=True,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            text=True
        )
        assert result.stdout == "output"

def test_executor_interactive_mode_forgoes_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = Executor()
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=["bash"], returncode=0)

        executor.run(["bash"], interactive=True)

        mock_run.assert_called_once_with(
            ["bash"],
            check=True,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
        )

def test_executor_wraps_errors_in_sandbox_error() -> None:
    executor = Executor()

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=1,
            cmd=["failing"],
            output=b"some error",
            stderr=b"fatal error"
        )

        with pytest.raises(SandboxExecutionError) as exc_info:
            executor.run(["failing"])

        error_text = str(exc_info.value)
        assert "fatal error" in error_text or "some error" in error_text or "failing" in error_text

def test_executor_merges_custom_env(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = Executor()
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=["ls"], returncode=0)

        executor.run(["ls"], env={"CUSTOM_VAR": "custom_value"})

        mock_run.assert_called_once_with(
            ["ls"],
            capture_output=True,
            check=True,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "CUSTOM_VAR": "custom_value"},
            text=True
        )

def test_executor_oserror() -> None:
    executor = Executor()
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = OSError("No such file or directory")
        with pytest.raises(SandboxExecutionError) as exc_info:
            executor.run(["/no-such-binary"])
        assert "OS Error during execution" in str(exc_info.value)
