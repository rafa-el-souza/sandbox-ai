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
["--oci-seccomp"]}``). key (or file) absent → ``MISSING``; present + differing
→ ``DRIFT``; present + deep-equal **but the daemon has not loaded the runtime**
→ ``DRIFT`` (will restart); present + deep-equal **and loaded** →
``ALREADY_CORRECT``. The probe/reverify are **runtime-aware** (F-023): they
confirm docker's *loaded* runtimes via ``docker info``, not just the
``daemon.json`` file — a file-only check reported ``ALREADY_CORRECT`` over a
write-success/restart-fail end state where the runtime was never registered.
The restart itself is StartLimit-safe (``reset-failed`` first) and uses
``--no-block`` + a runtime-aware readiness poll — see ``_restart_and_poll``.
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
from core.setup.phase_runner import (
    Identity,
    Phase,
    PhaseResult,
    probe_sandbox_pw_or_missing,
    run_crossing_until_delivered,
    wait_user_manager_ready,
)

if TYPE_CHECKING:
    from core.host_config import HostConfig
    from core.setup.phase_runner import SetupContext

# The single reserved key + its expected value (the content-aware target).
_RESERVED_RUNTIME_KEY = "sandbox-ai-runsc"
# Definitive stdout markers the readiness-poll crossing emits (it always exits 0
# so a lost sentinel is unambiguously the first-session transient, never
# conflated with a non-zero inner exit — see ``_restart_and_poll``).
_RUNTIME_LOADED_MARKER = "__SBX_RUNTIME_LOADED__"
_RUNTIME_ABSENT_MARKER = "__SBX_RUNTIME_ABSENT__"
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
    """Content-aware deep-equal probe over the reserved runtime key.

    Uses the shared sandbox-user guard: although ``depends_on=("l5",)`` (after
    L2 created the user), the not-yet-created user is the ``MISSING`` signal
    rather than a crash escaping the plan/apply passes (content-aware-probe
    contract / B1 class).
    """
    pw = probe_sandbox_pw_or_missing(ctx.host_config)
    if not isinstance(pw, pwd.struct_passwd):
        return pw
    path = Path(pw.pw_dir) / ".config" / "docker" / "daemon.json"
    doc = _read_doc(path)
    if doc is None:
        return PhaseResult.MISSING, f"{path} absent; will create with reserved key"
    observed = _observed_runtime(doc)
    if observed is None:
        return (
            PhaseResult.MISSING,
            f"{path} present but reserved runtime key absent; will merge",
        )
    if observed != _EXPECTED_RUNTIME:
        return (
            PhaseResult.DRIFT,
            f"{path} reserved runtime key differs from expected; will converge",
        )
    # File carries the correct key — but has docker actually LOADED it? A
    # write-success/restart-fail leaves the file correct and the runtime
    # unregistered (F-023); a file-only probe would report ALREADY_CORRECT over
    # a broken end state. Confirm the loaded runtime before skipping.
    if not _runtime_registered(ctx.host_config):
        return (
            PhaseResult.DRIFT,
            f"{path} has the reserved runtime key but docker has not loaded it; "
            f"will restart + verify registration",
        )
    return (
        PhaseResult.ALREADY_CORRECT,
        "reserved runtime key present and loaded by docker",
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


def _runtime_registered(host_config: HostConfig) -> bool:
    """``True`` iff docker's *loaded* runtimes include the reserved key (crossed).

    Queries the sandbox user's rootless daemon via ``docker info`` — the runtime
    docker has actually **loaded**, NOT the ``daemon.json`` file. This is the
    distinction F-023 turned on: a write-success/restart-fail leaves the file
    correct while the daemon never reloaded, so a file-only probe reports
    ``ALREADY_CORRECT`` over an unregistered runtime. A failed crossing (docker
    down, sentinel absent) is treated as not-registered, so the probe/reverify
    fail toward DRIFT and ``act`` re-restarts (idempotent, fail-closed).
    """
    prefix = machinectl_cmd(
        _sandbox_user(host_config), host_config.host.machinectl_authentication
    )
    try:
        result = Executor().run(
            [*prefix, "/bin/bash", "-c", "docker info --format '{{json .Runtimes}}'"],
            sentinel=True,
        )
    except SandboxExecutionError:
        return False
    return _RESERVED_RUNTIME_KEY in (result.stdout or "")


def _restart_and_poll(host_config: HostConfig) -> None:
    """Restart rootless docker StartLimit-safely; poll until the runtime loads.

    F-023-driven properties (fresh-VM-capture-confirmed):

    - ``reset-failed`` before the restart clears any prior failed state AND the
      systemd ``StartLimit`` counter, so the restart cannot trip "start of the
      service was attempted too often" and leave docker down (battery B5/B6) —
      and, crucially, makes the restart **safe to re-issue** when a fresh
      session drops its sentinel (below).
    - ``restart --no-block`` returns without synchronously waiting on the job;
      the **poll**, not the restart crossing, observes success.
    - the readiness poll waits until docker is up **and the reserved runtime is
      loaded** (``docker info`` runtimes contains the key), not merely until the
      daemon answers (battery B4a needed a settle). The poll's loaded-runtime
      marker is the authoritative success signal (F-023: confirm the daemon's
      LOADED runtime, never just the ``daemon.json`` file).

    The first-session empty-sentinel transient is **per-session** (each
    ``machinectl shell`` is a fresh PTY): a delivered crossing does not prove
    the next delivers, so the round-9 ``wait_user_crossing_ready`` pre-gate —
    which proved a throwaway echo session delivered, then restarted through a
    *separate* session that independently dropped its sentinel — could not fix
    it (F-023 first-apply, fresh-VM re-smoke). Instead the restart and the poll
    each run via :func:`run_crossing_until_delivered`, which retries the
    crossing itself on a lost sentinel: re-issuing the ``reset-failed`` +
    ``restart`` is StartLimit-safe and the poll is a pure read, so both are safe
    to repeat. ``wait_user_manager_ready`` (F-014, root-side ``is-active``)
    remains the cheap necessary precondition.
    """
    user = _sandbox_user(host_config)
    auth = host_config.host.machinectl_authentication
    wait_user_manager_ready(user)
    run_crossing_until_delivered(
        user,
        auth,
        "systemctl --user reset-failed docker.service; "
        "systemctl --user restart --no-block docker",
        what="docker restart",
    )
    # The crossing always exits 0 and emits a definitive marker, so a lost
    # sentinel (empty stdout) is retried as the first-session transient while a
    # delivered "runtime absent" is a genuine failure raised on below.
    poll = (
        "for i in $(seq 1 30); do "
        "docker info --format '{{json .Runtimes}}' 2>/dev/null "
        "| grep -qF " + _RESERVED_RUNTIME_KEY + " "
        "&& { echo " + _RUNTIME_LOADED_MARKER + "; exit 0; }; "
        "sleep 1; done; echo " + _RUNTIME_ABSENT_MARKER + "; exit 0"
    )
    result = run_crossing_until_delivered(
        user, auth, poll, what="docker runtime readiness poll"
    )
    if _RUNTIME_LOADED_MARKER not in (result.stdout or ""):
        raise SandboxExecutionError(
            f"[FATAL] Sandbox Execution Fault: rootless docker restarted but the "
            f"reserved runtime {_RESERVED_RUNTIME_KEY!r} was not loaded within "
            f"30s (docker info never listed it). Inspect the sandbox user's "
            f"docker journal: journalctl --user -u docker."
        )


def _act(ctx: SetupContext) -> str:
    """Ensure the reserved runtime key (preserving other keys), then restart.

    Never called on a ``CONFLICT`` (there is none for this phase) — only on
    ``MISSING`` / ``DRIFT``. The act ALWAYS restarts (F-023): with a
    runtime-aware probe, ``DRIFT`` can mean "the file is correct but docker has
    not loaded the runtime", so the old byte-identical-file short-circuit would
    wrongly skip the restart that registers it. The restart is StartLimit-safe
    and idempotent, and the readiness poll fails closed if the runtime never
    loads — so a no-op-merge with an already-loaded runtime is screened out at
    *probe* time (ALREADY_CORRECT → act not called), not here.
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
    if new_text != old_text:
        _write_inode_stable(path, new_text)
    _restart_and_poll(host_config)
    return "reserved runtime key ensured; rootless docker restarted + runtime loaded"


def _reverify(ctx: SetupContext) -> bool:
    """Confirm the key is in the file AND docker has loaded the runtime (F-023).

    File-deep-equal alone is insufficient — that was the masking gap. Reverify
    the *end state* the phase exists to produce: the runtime is registered in
    the running daemon.
    """
    path = _daemon_json_path(ctx.host_config)
    doc = _read_doc(path)
    if doc is None:
        return False
    if _observed_runtime(doc) != _EXPECTED_RUNTIME:
        return False
    return _runtime_registered(ctx.host_config)


PHASE = Phase(
    id="l6",
    name="daemon.json reserved runtime key",
    identity=Identity.ROOT,
    probe=_probe,
    act=_act,
    reverify=_reverify,
    depends_on=("l5",),
)
