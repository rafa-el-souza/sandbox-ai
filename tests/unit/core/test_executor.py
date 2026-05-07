"""Executor unit tests — subprocess boundary, sentinel protocol, PTY sanitization.

Tests validate:
- Sterile environment matrix
- Interactive vs captured mode bifurcation
- Exit code sentinel injection, parsing, last-match semantics, fail-closed
- PTY output sanitizer (ANSI stripping, CR removal, blank line collapse)
"""

import os
import subprocess
from unittest.mock import patch

import pytest
from core.exceptions import SandboxExecutionError
from core.executor import Executor

# ── Existing tests (backward compatibility) ──────────────────────────────────


def test_executor_runs_synchronously_with_default_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = Executor()
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=["ls"], returncode=0, stdout="output", stderr="")

        result = executor.run(["ls"])

        mock_run.assert_called_once_with(
            ["ls"], capture_output=True, check=True, env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}, text=True
        )
        assert result.stdout == "output"


def test_executor_interactive_mode_forgoes_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = Executor()
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=["bash"], returncode=0)

        executor.run(["bash"], interactive=True)

        mock_run.assert_called_once_with(["bash"], check=True, env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})


def test_executor_wraps_errors_in_sandbox_error() -> None:
    executor = Executor()

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=1, cmd=["failing"], output=b"some error", stderr=b"fatal error"
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
            text=True,
        )


def test_executor_oserror() -> None:
    executor = Executor()
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = OSError("No such file or directory")
        with pytest.raises(SandboxExecutionError) as exc_info:
            executor.run(["/no-such-binary"])
        assert "OS Error during execution" in str(exc_info.value)


# ── Sentinel injection tests (Task 1.7) ──────────────────────────────────────


class TestSentinelInjection:
    """Sentinel protocol: injection wraps inner bash -c command."""

    def test_sentinel_wraps_bash_c_payload(self) -> None:
        """WHEN sentinel=True and cmd ends with ['-c', '<payload>'], THEN payload is wrapped."""
        executor = Executor()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="output\n__SANDBOX_EXIT_deadbeef01234567_0\n", stderr=""
            )
            with patch("core.executor.secrets.token_hex", return_value="deadbeef01234567"):
                executor.run(
                    ["sudo", "machinectl", "shell", "user@.host", "/bin/bash", "-c", "docker compose up"],
                    sentinel=True,
                )

            actual_cmd = mock_run.call_args[0][0]
            assert actual_cmd[-1] == "{ docker compose up; }; echo __SANDBOX_EXIT_deadbeef01234567_$?"
            # check=False when sentinel is active
            assert mock_run.call_args[1]["check"] is False

    def test_sentinel_disabled_by_default(self) -> None:
        """WHEN sentinel not passed, THEN no wrapping occurs."""
        executor = Executor()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=["ls"], returncode=0, stdout="output", stderr="")
            executor.run(["sudo", "machinectl", "shell", "user@.host", "/bin/bash", "-c", "echo hi"])

            actual_cmd = mock_run.call_args[0][0]
            assert actual_cmd[-1] == "echo hi"
            assert mock_run.call_args[1]["check"] is True

    def test_sentinel_bypassed_for_interactive(self) -> None:
        """WHEN sentinel=True and interactive=True, THEN no sentinel injection."""
        executor = Executor()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            executor.run(
                ["sudo", "machinectl", "shell", "user@.host", "/bin/bash", "-c", "echo hi"],
                interactive=True,
                sentinel=True,
            )

            actual_cmd = mock_run.call_args[0][0]
            # Payload should be unchanged
            assert actual_cmd[-1] == "echo hi"
            assert mock_run.call_args[1]["check"] is True


class TestSentinelParsing:
    """Sentinel protocol: exit code parsing from captured output."""

    def test_zero_exit_code_returns_success(self) -> None:
        """WHEN sentinel line contains _0, THEN result.returncode == 0."""
        executor = Executor()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="line1\nline2\n__SANDBOX_EXIT_abcdef0123456789_0\n", stderr=""
            )
            with patch("core.executor.secrets.token_hex", return_value="abcdef0123456789"):
                result = executor.run(
                    ["sudo", "machinectl", "shell", "user@.host", "/bin/bash", "-c", "echo ok"],
                    sentinel=True,
                )
            assert result.returncode == 0
            assert "line1" in result.stdout
            assert "__SANDBOX_EXIT_" not in result.stdout

    def test_nonzero_exit_code_raises_error(self) -> None:
        """WHEN sentinel line contains non-zero exit code, THEN SandboxExecutionError raised."""
        executor = Executor()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="error output\n__SANDBOX_EXIT_abcdef0123456789_1\n", stderr="some stderr"
            )
            with (
                patch("core.executor.secrets.token_hex", return_value="abcdef0123456789"),
                pytest.raises(SandboxExecutionError) as exc_info,
            ):
                executor.run(
                    ["sudo", "machinectl", "shell", "user@.host", "/bin/bash", "-c", "false"],
                    sentinel=True,
                )
            assert "exit status 1" in str(exc_info.value)

    def test_sentinel_line_stripped_from_output(self) -> None:
        """WHEN sentinel parsed, THEN sentinel line is removed from stdout."""
        executor = Executor()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="clean output\n__SANDBOX_EXIT_abcdef0123456789_0\n", stderr=""
            )
            with patch("core.executor.secrets.token_hex", return_value="abcdef0123456789"):
                result = executor.run(
                    ["sudo", "machinectl", "shell", "user@.host", "/bin/bash", "-c", "echo hi"],
                    sentinel=True,
                )
            assert "__SANDBOX_EXIT_" not in result.stdout
            assert "clean output" in result.stdout

    def test_last_match_semantics(self) -> None:
        """WHEN multiple sentinel-like lines exist, THEN last match is authoritative."""
        executor = Executor()

        # Inner command echos something that looks like a sentinel, but the real one is last
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=("__SANDBOX_EXIT_abcdef0123456789_42\nreal output\n__SANDBOX_EXIT_abcdef0123456789_0\n"),
                stderr="",
            )
            with patch("core.executor.secrets.token_hex", return_value="abcdef0123456789"):
                result = executor.run(
                    ["sudo", "machinectl", "shell", "user@.host", "/bin/bash", "-c", "echo fake"],
                    sentinel=True,
                )
            # Should use last match (exit code 0), not first (42)
            assert result.returncode == 0


class TestSentinelFailClosed:
    """Sentinel protocol: fail-closed when sentinel not found."""

    def test_missing_sentinel_raises_error(self) -> None:
        """WHEN output has no sentinel line, THEN SandboxExecutionError raised."""
        executor = Executor()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="output without sentinel\n", stderr=""
            )
            with (
                patch("core.executor.secrets.token_hex", return_value="abcdef0123456789"),
                pytest.raises(SandboxExecutionError) as exc_info,
            ):
                executor.run(
                    ["sudo", "machinectl", "shell", "user@.host", "/bin/bash", "-c", "echo crash"],
                    sentinel=True,
                )
            assert "sentinel not found" in str(exc_info.value).lower()

    def test_empty_output_fails_closed(self) -> None:
        """WHEN output is empty, THEN SandboxExecutionError raised."""
        executor = Executor()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            with (
                patch("core.executor.secrets.token_hex", return_value="abcdef0123456789"),
                pytest.raises(SandboxExecutionError),
            ):
                executor.run(
                    ["sudo", "machinectl", "shell", "user@.host", "/bin/bash", "-c", "true"],
                    sentinel=True,
                )


# ── PTY Sanitizer tests (Task 1.8) ──────────────────────────────────────────


class TestPTYSanitizer:
    """PTY output sanitization: ANSI stripping, CR removal, blank collapse."""

    def test_ansi_escape_sequences_stripped(self) -> None:
        """WHEN output contains ANSI escapes, THEN they are removed."""
        raw = "\x1b[37mhello\x1b[0m \x1b[31mERROR\x1b[0m"
        result = Executor._sanitize_pty_output(raw)
        assert result == "hello ERROR"
        assert "\x1b" not in result

    def test_carriage_returns_removed(self) -> None:
        """WHEN output contains \\r from PTY ONLCR, THEN they are removed."""
        raw = "line1\r\nline2\r\n"
        result = Executor._sanitize_pty_output(raw)
        assert "\r" not in result
        assert result == "line1\nline2\n"

    def test_excessive_blank_lines_collapsed(self) -> None:
        """WHEN output has 3+ consecutive newlines, THEN collapsed to 2."""
        raw = "start\n\n\n\n\nend"
        result = Executor._sanitize_pty_output(raw)
        assert result == "start\n\nend"

    def test_two_newlines_preserved(self) -> None:
        """WHEN output has exactly 2 consecutive newlines, THEN preserved."""
        raw = "start\n\nend"
        result = Executor._sanitize_pty_output(raw)
        assert result == "start\n\nend"

    def test_combined_artifacts(self) -> None:
        """WHEN output has mixed PTY artifacts, THEN all are cleaned."""
        raw = "\x1b[1mBold\x1b[0m\r\n\r\n\r\n\r\nEnd\r\n"
        result = Executor._sanitize_pty_output(raw)
        assert "\x1b" not in result
        assert "\r" not in result
        # 4 newlines → 2
        assert result == "Bold\n\nEnd\n"

    def test_interactive_output_not_sanitized(self) -> None:
        """WHEN interactive=True, THEN sanitizer is never called on output."""
        executor = Executor()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            # sentinel=True + interactive=True → no sanitization
            result = executor.run(
                ["sudo", "machinectl", "shell", "user@.host", "/bin/bash", "-c", "echo hi"],
                interactive=True,
                sentinel=True,
            )
            # For interactive calls, stdout is None (no capture)
            assert result.stdout is None

    def test_sanitizer_applied_after_sentinel_extraction(self) -> None:
        """WHEN sentinel parsed from non-interactive output, THEN sanitizer runs on remaining output."""
        executor = Executor()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="\x1b[37mclean\x1b[0m\r\noutput\r\n\r\n\r\n\r\n__SANDBOX_EXIT_abcdef0123456789_0\n",
                stderr="",
            )
            with patch("core.executor.secrets.token_hex", return_value="abcdef0123456789"):
                result = executor.run(
                    ["sudo", "machinectl", "shell", "user@.host", "/bin/bash", "-c", "echo test"],
                    sentinel=True,
                )
            # ANSI stripped, CRs removed, blank lines collapsed, sentinel removed
            assert "\x1b" not in result.stdout
            assert "\r" not in result.stdout
            assert "__SANDBOX_EXIT_" not in result.stdout
            assert "clean" in result.stdout
            assert "output" in result.stdout
