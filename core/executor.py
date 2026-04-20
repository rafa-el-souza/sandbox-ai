import os
import subprocess
from typing import Any

from core.exceptions import SandboxExecutionError


class Executor:
    """
    Deterministically handles POSIX subprocess executions.
    Strips environments and correctly channels I/O.
    """

    def run(
        self, cmd: list[str], interactive: bool = False, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """
        Executes a command synchronously in a sterile POSIX environment,
        stripping host variables and bifurcating stream captures.
        """
        # Sterile Matrix: strictly permit only PATH, append overrides
        sterile_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        }
        if env:
            sterile_env.update(env)

        kwargs: dict[str, Any] = {
            "check": True,
            "env": sterile_env
        }

        if not interactive:
            kwargs["capture_output"] = True
            kwargs["text"] = True

        try:
            return subprocess.run(cmd, **kwargs)
        except subprocess.CalledProcessError as e:
            # Mask host topologies via opaque domain error
            error_msg = (
                f"[FATAL] Sandbox Execution Fault: Command '{' '.join(cmd)}'"
                f" failed with exit status {e.returncode}."
            )
            if not interactive and e.stderr:
                error_msg += f"\nError Trace:\n{e.stderr}"
            raise SandboxExecutionError(error_msg) from e
        except OSError as e:
            # Handle cases where binary doesn't exist
            raise SandboxExecutionError(
                f"[FATAL] Sandbox Execution Fault: OS Error during execution of '{' '.join(cmd)}': {e}"
            ) from e

