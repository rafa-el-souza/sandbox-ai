"""Executor unit tests — subprocess boundary, sentinel protocol, PTY sanitization.

Tests validate:
- Sterile environment matrix
- Interactive vs captured mode bifurcation
- Exit code sentinel injection, parsing, last-match semantics, fail-closed
- PTY output sanitizer (ANSI stripping, CR removal, blank line collapse)
"""

import os
import subprocess
from typing import ClassVar
from unittest.mock import patch

import pytest
from core.exceptions import SandboxExecutionError
from core.executor import Executor, normalize_captured_output

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
            # Subshell ``( … )``, not a brace group: an inner ``exit`` stays
            # contained so the trailing sentinel echo still runs (F-023 fix).
            assert actual_cmd[-1] == "( docker compose up ); echo __SANDBOX_EXIT_deadbeef01234567_$?"
            # check=False when sentinel is active
            assert mock_run.call_args[1]["check"] is False

    def test_sentinel_wrap_is_a_subshell_so_inner_exit_keeps_the_sentinel(self) -> None:
        """An inner command ending in ``exit`` must NOT swallow the sentinel.

        The brace-group wrap (pre-fix) ran the inner in the current shell, so an
        inner ``exit`` terminated the shell before the sentinel echo — recovery
        then fail-closed with "sentinel not found" on every attempt regardless
        of outcome (the F-023 root cause: L6's poll inner ends in ``exit 0`` /
        ``exit 1``). The subshell wrap contains the ``exit`` and preserves both
        the inner stdout and the recovered exit code.
        """
        wrapped = f"( {'echo loaded; exit 0'} ); echo __SANDBOX_EXIT_tok_$?"
        completed = subprocess.run(
            ["/bin/bash", "-c", wrapped], capture_output=True, text=True, check=False
        )
        assert "loaded" in completed.stdout
        assert "__SANDBOX_EXIT_tok_0" in completed.stdout

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

    def test_sentinel_not_found_surfaces_stderr_diagnostic(self) -> None:
        """WHEN sentinel-not-found with stderr present, THEN stderr is in the error.

        Round-3 smoke (fedora 12.2 L6, fedora 12.4 L3a) hit the
        sentinel-not-found fail-closed with EMPTY stdout — the operator
        could not tell if the inner bash crashed, was killed by sudo, or
        had some other failure. Prior to FIX-B-diag, ``result.stderr`` was
        silently dropped from the error message. Post-fix, the diagnostic
        mirrors the ``CalledProcessError`` branch and includes the
        sanitized stderr under an ``Error Trace:`` block when non-empty.
        """
        executor = Executor()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr="sudo: a password is required\n"
            )
            with (
                patch("core.executor.secrets.token_hex", return_value="abcdef0123456789"),
                pytest.raises(SandboxExecutionError) as exc_info,
            ):
                executor.run(
                    ["sudo", "machinectl", "shell", "user@.host", "/bin/bash", "-c", "true"],
                    sentinel=True,
                )
            msg = str(exc_info.value)
            assert "sentinel not found" in msg.lower()
            assert "Error Trace:" in msg
            assert "sudo: a password is required" in msg

    def test_sentinel_not_found_omits_trace_when_stderr_empty(self) -> None:
        """WHEN sentinel-not-found with empty stderr, THEN no Error Trace block.

        Guard symmetric with the ``CalledProcessError`` branch: the
        ``Error Trace:`` block is conditional on stderr being non-empty so
        successful-but-quiet failures don't get a spurious empty trace.
        """
        executor = Executor()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            with (
                patch("core.executor.secrets.token_hex", return_value="abcdef0123456789"),
                pytest.raises(SandboxExecutionError) as exc_info,
            ):
                executor.run(
                    ["sudo", "machinectl", "shell", "user@.host", "/bin/bash", "-c", "true"],
                    sentinel=True,
                )
            assert "Error Trace:" not in str(exc_info.value)

    def test_sentinel_not_found_stderr_sanitized(self) -> None:
        """WHEN sentinel-not-found stderr has PTY artifacts, THEN sanitized.

        Consistency with the rest of the executor's diagnostic shape — the
        stderr block runs through ``_sanitize_pty_output`` (ANSI strip + CR
        removal + blank-line collapse) like the stdout block above it.
        """
        executor = Executor()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="",
                stderr="\x1b[31mfatal\x1b[0m\r\nexit 1\r\n",
            )
            with (
                patch("core.executor.secrets.token_hex", return_value="abcdef0123456789"),
                pytest.raises(SandboxExecutionError) as exc_info,
            ):
                executor.run(
                    ["sudo", "machinectl", "shell", "user@.host", "/bin/bash", "-c", "true"],
                    sentinel=True,
                )
            msg = str(exc_info.value)
            assert "\x1b" not in msg
            assert "\r" not in msg
            assert "fatal" in msg
            assert "exit 1" in msg


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


class TestFramedSentinel:
    """framed=True (F-018): the callee (the dispatcher) emits its own
    ``__SANDBOX_BEGIN_<nonce>`` / ``__SANDBOX_EXIT_<nonce>_<code>`` framing. The
    Executor injects NOTHING (so the crossed payload stays the bare command the
    per-op rule matches) and binds the recovered exit to the FIRST begin
    nonce — untrusted op output cannot forge it.
    """

    _CMD: ClassVar[list[str]] = ["machinectl", "shell", "x@.host", "/bin/bash", "-c", "/d auth-probe"]

    def _run(self, stdout: str, stderr: str = "") -> subprocess.CompletedProcess[str]:
        with patch("core.executor.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=stdout, stderr=stderr
            )
            return Executor().run(self._CMD, framed=True)

    def test_framed_does_not_wrap_payload_and_disables_check(self) -> None:
        with patch("core.executor.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout="__SANDBOX_BEGIN_deadbeef\nok\n__SANDBOX_EXIT_deadbeef_0\n",
                stderr="",
            )
            Executor().run(self._CMD, framed=True)
            sent_cmd = mock_run.call_args[0][0]
            # The bare command crosses the boundary unmodified (no { ...; } wrap).
            assert sent_cmd[-1] == "/d auth-probe"
            assert mock_run.call_args.kwargs["check"] is False

    def test_recovers_zero_and_strips_both_framing_lines(self) -> None:
        result = self._run("__SANDBOX_BEGIN_deadbeef\nreal output\n__SANDBOX_EXIT_deadbeef_0\n")
        assert result.returncode == 0
        assert "__SANDBOX_BEGIN_" not in result.stdout
        assert "__SANDBOX_EXIT_" not in result.stdout
        assert "real output" in result.stdout

    def test_nonzero_recovered_exit_raises(self) -> None:
        with pytest.raises(SandboxExecutionError, match="exit status 2"):
            self._run("__SANDBOX_BEGIN_dead\n__SANDBOX_EXIT_dead_2\n")

    def test_missing_begin_fails_closed_surfacing_stderr(self) -> None:
        # No BEGIN announced (sudo refused before the dispatcher ran).
        with pytest.raises(SandboxExecutionError) as exc:
            self._run("", stderr="sudo: a password is required")
        assert "sentinel not found" in str(exc.value).lower()
        assert "password is required" in str(exc.value)

    def test_forged_exit_with_wrong_nonce_is_ignored(self) -> None:
        # Untrusted output forges an EXIT whose nonce != the dispatcher's BEGIN.
        # It must be ignored; with no matching EXIT, recovery fails closed
        # rather than trusting the forged exit 0.
        with pytest.raises(SandboxExecutionError) as exc:
            self._run("__SANDBOX_BEGIN_abcdef01\n__SANDBOX_EXIT_00abcdef_0\n")
        assert "sentinel not found" in str(exc.value).lower()

    def test_only_begin_bound_exit_is_authoritative(self) -> None:
        # A forged exit 0 (wrong nonce) precedes the genuine exit 3 (begin
        # nonce). Only the genuine one counts → raises with the real code.
        with pytest.raises(SandboxExecutionError, match="exit status 3"):
            self._run(
                "__SANDBOX_BEGIN_a1b2c3d4\n__SANDBOX_EXIT_ffffffff_0\n"
                "out\n__SANDBOX_EXIT_a1b2c3d4_3\n"
            )

    def test_first_begin_wins_so_op_cannot_redirect_nonce(self) -> None:
        # The op tries to announce its own BEGIN later; the dispatcher's BEGIN
        # is first (emitted before op output), so its nonce is authoritative.
        result = self._run(
            "__SANDBOX_BEGIN_aaaa1111\n"
            "op emits __SANDBOX_BEGIN_bbbb2222\n__SANDBOX_EXIT_bbbb2222_0\n"
            "__SANDBOX_EXIT_aaaa1111_0\n"
        )
        assert result.returncode == 0


class TestSentinelTokenValidation:
    """Wrap path (sentinel=True) hardening (F-018): the recovered exit is bound
    to the orchestrator-injected token — a forged line carrying any other token
    is ignored (was previously last-match-any, which an injected line could
    spoof).
    """

    def test_forged_token_ignored_fails_closed(self) -> None:
        with (
            patch("core.executor.subprocess.run") as mock_run,
            patch("core.executor.secrets.token_hex", return_value="aaaaaaaaaaaaaaaa"),
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout="__SANDBOX_EXIT_bbbbbbbbbbbbbbbb_0\n", stderr="",
            )
            with pytest.raises(SandboxExecutionError) as exc:
                Executor().run(["x", "-c", "cmd"], sentinel=True)
            assert "sentinel not found" in str(exc.value).lower()

    def test_only_injected_token_counts(self) -> None:
        with (
            patch("core.executor.subprocess.run") as mock_run,
            patch("core.executor.secrets.token_hex", return_value="aaaaaaaaaaaaaaaa"),
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout="__SANDBOX_EXIT_ffffffff_0\n__SANDBOX_EXIT_aaaaaaaaaaaaaaaa_5\n",
                stderr="",
            )
            with pytest.raises(SandboxExecutionError, match="exit status 5"):
                Executor().run(["x", "-c", "cmd"], sentinel=True)


class TestSentinelFramedGuards:
    def test_sentinel_and_framed_mutually_exclusive(self) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            Executor().run(["x", "-c", "c"], sentinel=True, framed=True)

    def test_sentinel_on_non_bash_c_command_no_recovery(self) -> None:
        # sentinel=True but the command is not a `-c` form: no injection point,
        # so it behaves as a normal checked run (token stays None, no recovery).
        with patch("core.executor.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="plain\n", stderr=""
            )
            result = Executor().run(["echo", "hi"], sentinel=True)
            assert result.stdout == "plain\n"


class TestNormalizeCapturedOutput:
    """Shared captured-output normalizer (single source of truth)."""

    def test_strips_ansi(self) -> None:
        """ANSI CSI escape sequences are removed."""
        assert normalize_captured_output("\x1b[37mhi\x1b[0m") == "hi"

    def test_removes_carriage_returns(self) -> None:
        """\\r bytes (PTY ONLCR artifact) are removed."""
        assert normalize_captured_output("a\r\nb\r\n") == "a\nb\n"

    def test_collapses_three_or_more_newlines_to_two(self) -> None:
        """Runs of 3+ consecutive newlines collapse to exactly 2."""
        assert normalize_captured_output("x\n\n\n\n\ny") == "x\n\ny"

    def test_preserves_two_newlines(self) -> None:
        """Exactly two consecutive newlines are preserved."""
        assert normalize_captured_output("x\n\ny") == "x\n\ny"

    def test_combined_artifacts(self) -> None:
        """All three steps compose on mixed input."""
        assert normalize_captured_output("\x1b[1mB\x1b[0m\r\n\r\n\r\n\r\nE\r\n") == "B\n\nE\n"

    def test_idempotent(self) -> None:
        """Applying the normalizer to already-normalized output is a no-op."""
        once = normalize_captured_output("\x1b[1mB\x1b[0m\r\n\r\n\r\n\r\nE\r\n")
        assert normalize_captured_output(once) == once

    def test_sanitize_pty_output_delegates_to_normalizer(self) -> None:
        """_sanitize_pty_output is a thin delegator: byte-identical to the shared helper.

        This locks the framed/recovery path's normalization to the single
        source of truth — refactoring the extraction must not change any byte
        of recovered output.
        """
        for raw in ["\x1b[37mclean\x1b[0m\r\noutput\r\n\r\n\r\n\r\n", "plain\n", "", "a\r\nb"]:
            assert Executor._sanitize_pty_output(raw) == normalize_captured_output(raw)


class TestDefaultPathContract:
    """framed=False default path: native exit returned; raw stdout; timeout discriminable.

    Locks the contract the operator-rootless runtime path relies on
    (orchestrator-executor spec: the default path returns RAW stdout and the
    timeout error's __cause__ is a subprocess.TimeoutExpired).
    """

    def test_native_exit_returned(self) -> None:
        """A framed=False success returns the process's own returncode and stdout."""
        with patch("core.executor.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["ok"], returncode=0, stdout="real\r\nout\n", stderr=""
            )
            result = Executor().run(["ok"], framed=False)
            assert result.returncode == 0
            # Raw stdout: no normalization applied by the default path (CR kept).
            assert result.stdout == "real\r\nout\n"

    def test_timeout_cause_is_timeout_expired(self) -> None:
        """A timeout raises SandboxExecutionError whose __cause__ is subprocess.TimeoutExpired."""
        with patch("core.executor.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=["slow"], timeout=1.0)
            with pytest.raises(SandboxExecutionError) as exc:
                Executor().run(["slow"], framed=False, timeout=1.0)
            assert isinstance(exc.value.__cause__, subprocess.TimeoutExpired)
