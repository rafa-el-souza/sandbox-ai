import os
import re
import secrets
import subprocess
from typing import Any

from core.exceptions import SandboxExecutionError

# Sentinel pattern: __SANDBOX_EXIT_{hex_token}_{exit_code}
_SENTINEL_RE = re.compile(r"^__SANDBOX_EXIT_([0-9a-f]+)_(\d+)\s*$", re.MULTILINE)

# ANSI escape sequence pattern (CSI sequences)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


class Executor:
    """
    Deterministically handles POSIX subprocess executions.
    Strips environments and correctly channels I/O.
    """

    @staticmethod
    def _sanitize_pty_output(raw: str) -> str:
        """Strip PTY artifacts from captured non-interactive output.

        Layer 2 defense-in-depth (D2):
        - Strip ANSI escape sequences
        - Remove carriage returns from PTY ONLCR line discipline
        - Collapse runs of 3+ consecutive newlines to exactly 2
        """
        clean = _ANSI_RE.sub("", raw)
        clean = clean.replace("\r", "")
        clean = re.sub(r"\n{3,}", "\n\n", clean)
        return clean

    def run(
        self,
        cmd: list[str],
        interactive: bool = False,
        env: dict[str, str] | None = None,
        *,
        sentinel: bool = False,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """
        Executes a command synchronously in a sterile POSIX environment,
        stripping host variables and bifurcating stream captures.

        Args:
            cmd: Command and arguments to execute.
            interactive: If True, inherits stdio for PTY handover.
            env: Additional environment variables to merge into the sterile matrix.
            sentinel: If True, inject an exit code sentinel into the bash payload
                      to recover the inner command's exit code from machinectl shell.
                      Only effective when interactive=False.
        """
        # Sterile Matrix: strictly permit only PATH, append overrides
        sterile_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")}
        if env:
            sterile_env.update(env)

        kwargs: dict[str, Any] = {"check": True, "env": sterile_env}
        if timeout is not None:
            kwargs["timeout"] = timeout

        if not interactive:
            kwargs["capture_output"] = True
            kwargs["text"] = True

        # Sentinel injection: wrap inner command for non-interactive calls
        token: str | None = None
        if sentinel and not interactive:
            token = secrets.token_hex(8)
            sentinel_echo = f"__SANDBOX_EXIT_{token}_$?"
            # The last argument is the bash -c payload; wrap it
            if len(cmd) >= 3 and cmd[-2] == "-c":
                inner_cmd = cmd[-1]
                wrapped = f"{{ {inner_cmd}; }}; echo {sentinel_echo}"
                cmd = [*cmd[:-1], wrapped]
                # Disable check=True — we parse exit code from sentinel
                kwargs["check"] = False
            # else: token remains None — fail-closed handled below

        try:
            result = subprocess.run(cmd, **kwargs)
        except subprocess.CalledProcessError as e:
            # Mask host topologies via opaque domain error
            error_msg = (
                f"[FATAL] Sandbox Execution Fault: Command '{' '.join(cmd)}' failed with exit status {e.returncode}."
            )
            if not interactive and e.stderr:
                error_msg += f"\nError Trace:\n{e.stderr}"
            raise SandboxExecutionError(error_msg) from e
        except subprocess.TimeoutExpired as e:
            raise SandboxExecutionError(
                f"[FATAL] Sandbox Execution Fault: Command '{' '.join(cmd)}' timed out after {e.timeout}s."
            ) from e
        except OSError as e:
            # Handle cases where binary doesn't exist
            raise SandboxExecutionError(
                f"[FATAL] Sandbox Execution Fault: OS Error during execution of '{' '.join(cmd)}': {e}"
            ) from e

        # Sentinel parsing: extract inner exit code from output
        if token is not None and not interactive:
            stdout = result.stdout or ""

            # Find all matches — use last-match semantics
            matches = list(_SENTINEL_RE.finditer(stdout))
            if not matches:
                # Fail-closed: sentinel not found
                sanitized = self._sanitize_pty_output(stdout)
                raise SandboxExecutionError(
                    f"[FATAL] Exit sentinel not found in command output. "
                    f"The command may have crashed or produced corrupted output.\n"
                    f"Output:\n{sanitized}"
                )

            last_match = matches[-1]
            exit_code = int(last_match.group(2))

            # Strip the sentinel line from output
            stdout_clean = stdout[: last_match.start()] + stdout[last_match.end() :]

            # Apply PTY sanitization (Layer 2)
            stdout_clean = self._sanitize_pty_output(stdout_clean)
            result = subprocess.CompletedProcess(
                args=result.args,
                returncode=exit_code,
                stdout=stdout_clean,
                stderr=result.stderr,
            )

            if exit_code != 0:
                error_msg = f"[FATAL] Sandbox Execution Fault: Inner command failed with exit status {exit_code}."
                if result.stderr:
                    error_msg += f"\nError Trace:\n{result.stderr}"
                if result.stdout:
                    error_msg += f"\nOutput:\n{result.stdout}"
                raise SandboxExecutionError(error_msg)

        return result
