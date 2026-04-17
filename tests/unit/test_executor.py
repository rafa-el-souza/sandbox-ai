import pytest
import subprocess
import os
from unittest.mock import patch

from core.executor import Executor
from core.exceptions import SandboxExecutionError

def test_executor_runs_synchronously_with_default_capture(monkeypatch):
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

def test_executor_interactive_mode_forgoes_capture(monkeypatch):
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

def test_executor_wraps_errors_in_sandbox_error():
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
            
        assert "fatal error" in str(exc_info.value) or "some error" in str(exc_info.value) or "failing" in str(exc_info.value)

def test_executor_merges_custom_env(monkeypatch):
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
