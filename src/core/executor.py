# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
import os
import re
import secrets
import subprocess
from typing import NoReturn

from core.exceptions import SandboxExecutionError

__all__ = [
    "Executor",
    "normalize_captured_output",
]

# Sentinel pattern: __SANDBOX_EXIT_{hex_token}_{exit_code}
_SENTINEL_RE = re.compile(r"^__SANDBOX_EXIT_([0-9a-f]+)_(\d+)\s*$", re.MULTILINE)

# Dispatcher-framed-sentinel begin pattern: __SANDBOX_BEGIN_{hex_nonce}. The
# dispatcher (F-018) announces this nonce on stdout BEFORE running the op and
# echoes __SANDBOX_EXIT_{same_nonce}_{code} after. Untrusted op output cannot
# forge the trailer because it cannot read the dispatcher's prior stdout to
# learn the nonce, and the parser binds the trailer to the FIRST begin line's
# nonce (the dispatcher's — emitted before any op output).
_BEGIN_RE = re.compile(r"^__SANDBOX_BEGIN_([0-9a-f]+)\s*$", re.MULTILINE)

# ANSI escape sequence pattern (CSI sequences)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def normalize_captured_output(raw: str) -> str:
    """Normalize captured op output (single source of truth).

    Three-step normalization shared by the ``separate-user`` framing-recovery
    path (via :meth:`Executor._sanitize_pty_output`) and the
    ``operator-rootless`` ``core.dispatch`` path:

    - Strip ANSI escape sequences
    - Remove carriage returns (PTY ONLCR line discipline artifact)
    - Collapse runs of 3+ consecutive newlines to exactly 2

    Idempotent: applying it to already-normalized output is a no-op.
    """
    clean = _ANSI_RE.sub("", raw)
    clean = clean.replace("\r", "")
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean


class Executor:
    """
    Deterministically handles POSIX subprocess executions.
    Strips environments and correctly channels I/O.
    """

    # The begin nonce recovered from the most recent ``framed=True`` crossing
    # (the dispatcher's ``__SANDBOX_BEGIN_<nonce>`` line), or ``None`` if no
    # framed crossing has recovered one on this instance. ``core.dispatch``'s
    # preflight nonce-binding (H-1) reads this to bind the bundle markers to the
    # same nonce the frame used; ``run``'s public ``CompletedProcess`` contract
    # is unchanged, so every other caller is unaffected.
    last_frame_nonce: str | None = None

    @staticmethod
    def _sanitize_pty_output(raw: str) -> str:
        """Strip PTY artifacts from captured non-interactive output.

        Layer 2 defense-in-depth (D2). Thin delegator to the module-level
        :func:`normalize_captured_output` — the single source of truth for the
        three-step normalization (ANSI strip, CR removal, blank-line collapse).
        """
        return normalize_captured_output(raw)

    def run(
        self,
        cmd: list[str],
        interactive: bool = False,
        env: dict[str, str] | None = None,
        *,
        sentinel: bool = False,
        framed: bool = False,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """
        Executes a command synchronously in a sterile POSIX environment,
        stripping host variables and bifurcating stream captures.

        Args:
            cmd: Command and arguments to execute.
            interactive: If True, inherits stdio for PTY handover.
            env: Additional environment variables to merge into the sterile matrix.
            sentinel: If True, INJECT an exit-code sentinel into the bash payload
                      to recover the inner command's exit code from machinectl
                      shell. The injected token is orchestrator-generated and the
                      recovered exit is bound to it (an output line carrying a
                      different/forged token is ignored). Only effective when
                      interactive=False. Used by the root setup-phase crossings.
            framed: If True, do NOT inject — the callee (the dispatcher, F-018)
                    emits its own ``__SANDBOX_BEGIN_<nonce>`` line before the op
                    and ``__SANDBOX_EXIT_<nonce>_<code>`` after. The recovered exit
                    is bound to the FIRST begin line's nonce, so untrusted op
                    output cannot forge it. Mutually exclusive with ``sentinel``.
                    Used by the operator dispatch crossing (core.dispatch).
        """
        if sentinel and framed:
            raise ValueError("sentinel and framed are mutually exclusive")
        # Sterile Matrix: strictly permit only PATH, append overrides
        sterile_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")}
        if env:
            sterile_env.update(env)

        check = True

        # Sentinel injection (wrap path): wrap the inner command for
        # non-interactive calls. The token is orchestrator-generated, so the
        # recovered exit can be bound to it. Used by root setup-phase crossings.
        token: str | None = None
        if sentinel and not interactive:
            token = secrets.token_hex(8)
            sentinel_echo = f"__SANDBOX_EXIT_{token}_$?"
            # The last argument is the bash -c payload; wrap it. A SUBSHELL
            # ``( … )`` — NOT a brace group ``{ … }`` — so an ``exit`` inside the
            # inner command terminates only the subshell, and the trailing
            # sentinel echo still runs in the parent with the inner's exit in
            # ``$?``. A brace group runs in the current shell, so an inner
            # ``exit`` swallowed the sentinel entirely (recovery then fail-closed
            # with "sentinel not found" on every attempt regardless of outcome —
            # the F-023 root cause). The dispatcher's Go-side op framing
            # (``main.go`` ``wrapSentinel``) builds the same subshell wrap — kept
            # in parity so neither producer can regress to the brace-group bug.
            if len(cmd) >= 3 and cmd[-2] == "-c":
                inner_cmd = cmd[-1]
                wrapped = f"( {inner_cmd} ); echo {sentinel_echo}"
                cmd = [*cmd[:-1], wrapped]
                # Disable check=True — we parse exit code from sentinel
                check = False
            else:
                # sentinel requested on a non-`bash -c` command: no injection
                # point, so no recovery (behaves as a normal checked run).
                token = None

        # Framed path (F-018): the callee (the dispatcher) emits its own
        # begin/exit framing — we inject nothing and recover the exit from the
        # dispatcher's nonce-bound trailer, so the crossed (sudo-authorized)
        # payload stays the bare command the per-op rule matches.
        if framed and not interactive:
            check = False

        try:
            # Explicit ``subprocess.run`` calls in place of the former
            # ``kwargs: dict[str, Any]`` spread (which forced an opaque
            # ``CompletedProcess[Any]``). ``text=True`` selects the overload that
            # returns a concrete ``CompletedProcess[str]`` on both branches; the
            # non-interactive path additionally sets ``capture_output=True`` (the
            # recovery-eligible path that reads ``result.stdout`` as ``str``).
            # The interactive path inherits stdio for PTY handover and captures
            # nothing — with no pipe to decode, ``text`` is a runtime no-op
            # there (stdout/stderr stay ``None``), so the only observable change
            # vs. the prior dict is the kwarg's presence, not behaviour.
            # ``timeout`` is forwarded unconditionally; ``timeout=None`` is the
            # parameter's own default, identical to omitting it.
            result: subprocess.CompletedProcess[str]
            if interactive:
                result = subprocess.run(cmd, check=check, env=sterile_env, timeout=timeout, text=True)
            else:
                result = subprocess.run(
                    cmd, check=check, env=sterile_env, timeout=timeout, capture_output=True, text=True
                )
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

        # Exit recovery: bind the recovered inner exit to a value untrusted
        # output cannot forge — the token we injected (wrap path) or the
        # dispatcher's begin/exit nonce (framed path).
        if not interactive and (token is not None or framed):
            return self._recover_inner_exit(result, framed=framed, injected_token=token)
        return result

    def _recover_inner_exit(
        self,
        result: subprocess.CompletedProcess[str],
        *,
        framed: bool,
        injected_token: str | None,
    ) -> subprocess.CompletedProcess[str]:
        """Recover and validate the inner exit from the captured output.

        The recovered exit is bound to an unforgeable token: on the framed path
        the nonce announced by the dispatcher's FIRST ``__SANDBOX_BEGIN_`` line
        (emitted before any op output, so untrusted output cannot precede or
        guess it); on the wrap path the token this Executor injected. Only
        ``__SANDBOX_EXIT_`` lines carrying that exact token are authoritative —
        a forged line with any other token is ignored. Both the begin and exit
        framing lines are stripped from the returned stdout. Fail-closed (raise
        :class:`SandboxExecutionError`) when the expected framing is absent.
        """
        stdout = result.stdout or ""
        spans: list[tuple[int, int]] = []
        if framed:
            begin = _BEGIN_RE.search(stdout)
            if begin is None:
                self._raise_sentinel_not_found(stdout, result.stderr)
            expected = begin.group(1)
            # Surface the frame's nonce so core.dispatch can bind the preflight
            # bundle markers to it (H-1). Stash before any further fail-closed
            # branch so it reflects the begin line we actually recovered.
            self.last_frame_nonce = expected
            spans.append((begin.start(), begin.end()))
        else:
            expected = injected_token

        matches = [m for m in _SENTINEL_RE.finditer(stdout) if m.group(1) == expected]
        if not matches:
            self._raise_sentinel_not_found(stdout, result.stderr)
        last_match = matches[-1]
        exit_code = int(last_match.group(2))
        spans.append((last_match.start(), last_match.end()))

        stdout_clean = stdout
        for start, end in sorted(spans, reverse=True):
            stdout_clean = stdout_clean[:start] + stdout_clean[end:]
        stdout_clean = self._sanitize_pty_output(stdout_clean)

        recovered = subprocess.CompletedProcess(
            args=result.args,
            returncode=exit_code,
            stdout=stdout_clean,
            stderr=result.stderr,
        )
        if exit_code != 0:
            error_msg = f"[FATAL] Sandbox Execution Fault: Inner command failed with exit status {exit_code}."
            if recovered.stderr:
                error_msg += f"\nError Trace:\n{recovered.stderr}"
            if recovered.stdout:
                error_msg += f"\nOutput:\n{recovered.stdout}"
            raise SandboxExecutionError(error_msg)
        return recovered

    def _raise_sentinel_not_found(self, stdout: str, stderr: str | None) -> NoReturn:
        """Fail closed when the expected exit framing is absent (NoReturn).

        Empty-output / framing-absent failures (machinectl shell against a
        not-yet-ready user manager, a sudo refusal that never ran the
        dispatcher, a forged trailer whose token did not match, etc.) carry the
        actual cause on stderr; dropping it silently masks the root cause.
        """
        sanitized = self._sanitize_pty_output(stdout)
        error_msg = (
            f"[FATAL] Exit sentinel not found in command output. "
            f"The command may have crashed or produced corrupted output.\n"
            f"Output:\n{sanitized}"
        )
        if stderr:
            error_msg += f"\nError Trace:\n{self._sanitize_pty_output(stderr)}"
        raise SandboxExecutionError(error_msg)
