"""L6 — daemon.json reserved-key merge + restart cliff + reverify.

Setup owns exactly one key in the sandbox user's rootless docker
``daemon.json``: ``runtimes["sandbox-ai-runsc"]``. Every other key (the
operator's own ``runtimes[...]`` entries, log opts, registry mirrors, …) is
left untouched (design — "Reserved Namespace File Ownership").

The file is read by **root** (the ``sudo sandbox setup`` process itself —
identity ``ROOT``); the conditional ``systemctl --user restart docker`` and the
``docker info`` readiness poll cross into the sandbox user via
``machinectl_cmd``. The file write is inode-stable (``cat > file`` semantics —
truncate-in-place, design D9) so a live dockerd watching the inode is not
surprised by a rename.

Content-aware probe (design D10): a deep-equal comparison of the *observed*
``runtimes["sandbox-ai-runsc"]`` value against the *expected* one
(``{"path": "/usr/local/libexec/sandbox-ai/runsc", "runtimeArgs":
["--oci-seccomp"]}``). Present + deep-equal → ``ALREADY_CORRECT``; present +
differing → ``DRIFT``; key (or file) absent → ``MISSING``. A naive
file-exists probe would miss a wheel upgrade that changed the expected runtime
args.
"""

from __future__ import annotations

import json
import os
import pwd
from pathlib import Path
from typing import TYPE_CHECKING

from core.exceptions import SandboxExecutionError
from core.executor import Executor
from core.host_config import machinectl_cmd
from core.setup.phase_runner import Identity, Phase, PhaseResult

if TYPE_CHECKING:
    from core.host_config import HostConfig
    from core.setup.phase_runner import SetupContext

# The single reserved key + its expected value (the content-aware target).
_RESERVED_RUNTIME_KEY = "sandbox-ai-runsc"
_EXPECTED_RUNTIME: dict[str, object] = {
    "path": "/usr/local/libexec/sandbox-ai/runsc",
    "runtimeArgs": ["--oci-seccomp"],
}


def _sandbox_user(host_config: HostConfig) -> str:
    """The unprivileged docker user whose rootless daemon.json is managed."""
    return host_config.host.docker_unprivileged_user


def _daemon_json_path(host_config: HostConfig) -> Path:
    """Resolve ``~<sandbox-user>/.config/docker/daemon.json`` via passwd."""
    home = pwd.getpwnam(_sandbox_user(host_config)).pw_dir
    return Path(home) / ".config" / "docker" / "daemon.json"


def _read_doc(path: Path) -> dict[str, object] | None:
    """Read + parse the daemon.json document; ``None`` if the file is absent.

    An empty file parses as a fresh empty document (dockerd treats it the same
    way); a present-but-corrupt JSON file raises (a malformed operator file is
    not something setup silently overwrites — surfaced as an act failure).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    if not text.strip():
        return {}
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise SandboxExecutionError(
            f"[FATAL] Sandbox Execution Fault: {path} is not a JSON object; "
            f"refusing to merge the reserved runtime key."
        )
    return parsed


def _observed_runtime(doc: dict[str, object]) -> object | None:
    """The observed ``runtimes['sandbox-ai-runsc']`` value (``None`` if absent)."""
    runtimes = doc.get("runtimes")
    if isinstance(runtimes, dict):
        return runtimes.get(_RESERVED_RUNTIME_KEY)
    return None


def _probe(ctx: SetupContext) -> tuple[PhaseResult, str]:
    """Content-aware deep-equal probe over the reserved runtime key."""
    path = _daemon_json_path(ctx.host_config)
    doc = _read_doc(path)
    if doc is None:
        return PhaseResult.MISSING, f"{path} absent; will create with reserved key"
    observed = _observed_runtime(doc)
    if observed is None:
        return (
            PhaseResult.MISSING,
            f"{path} present but reserved runtime key absent; will merge",
        )
    if observed == _EXPECTED_RUNTIME:
        return PhaseResult.ALREADY_CORRECT, "reserved runtime key matches expected"
    return (
        PhaseResult.DRIFT,
        f"{path} reserved runtime key differs from expected; will converge",
    )


def _write_inode_stable(path: Path, text: str) -> None:
    """Truncate-in-place write (``cat > file`` semantics, design D9).

    Opens the existing inode with ``O_TRUNC`` rather than ``rename``-ing a new
    file in, so a live dockerd holding the inode sees the updated content
    without an inode swap. Creates parent dirs / the file if absent.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


def _restart_and_poll(host_config: HostConfig) -> None:
    """Restart rootless docker (crossed) and poll ``docker info`` readiness."""
    user = _sandbox_user(host_config)
    prefix = machinectl_cmd(user, host_config.host.machinectl_authentication)
    Executor().run(
        [
            *prefix,
            "/bin/bash",
            "-c",
            "systemctl --user restart docker",
        ],
        sentinel=True,
    )
    # Readiness poll: a short bounded shell retry loop on ``docker info``;
    # the inner exit is recovered via the sentinel so a never-ready daemon
    # surfaces as a failure rather than a masked success.
    Executor().run(
        [
            *prefix,
            "/bin/bash",
            "-c",
            "for i in $(seq 1 30); do docker info >/dev/null 2>&1 && exit 0; "
            "sleep 1; done; exit 1",
        ],
        sentinel=True,
    )


def _act(ctx: SetupContext) -> str:
    """Merge the reserved runtime key (preserving other keys), restart if dirty.

    Never called on a ``CONFLICT`` (there is none for this phase) — only on
    ``MISSING`` / ``DRIFT``. A merge that is byte-identical to the existing
    file does not restart docker; any real change triggers the restart cliff.
    """
    host_config = ctx.host_config
    path = _daemon_json_path(host_config)
    doc = _read_doc(path) or {}
    runtimes = doc.get("runtimes")
    if not isinstance(runtimes, dict):
        runtimes = {}
        doc["runtimes"] = runtimes
    runtimes[_RESERVED_RUNTIME_KEY] = _EXPECTED_RUNTIME

    new_text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    try:
        old_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        old_text = ""
    if new_text == old_text:
        return "reserved runtime key already byte-identical; no restart"

    _write_inode_stable(path, new_text)
    _restart_and_poll(host_config)
    return "reserved runtime key merged; rootless docker restarted"


def _reverify(ctx: SetupContext) -> bool:
    """Confirm the reserved runtime key is present and deep-equal to expected."""
    path = _daemon_json_path(ctx.host_config)
    doc = _read_doc(path)
    if doc is None:
        return False
    return _observed_runtime(doc) == _EXPECTED_RUNTIME


PHASE = Phase(
    id="l6",
    name="daemon.json reserved runtime key",
    identity=Identity.ROOT,
    probe=_probe,
    act=_act,
    reverify=_reverify,
    depends_on=("l5",),
)
