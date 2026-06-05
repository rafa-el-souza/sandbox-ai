"""L6 — daemon.json reserved-key merge + restart cliff + reverify.

Setup owns exactly one key in the sandbox user's rootless docker
``daemon.json``: ``runtimes["sandbox-ai-runsc"]``. Every other key (the
operator's own ``runtimes[...]`` entries, log opts, registry mirrors, …) is
left untouched (design — "Reserved Namespace File Ownership").

The daemon owner is :func:`daemon_owner_user` (the dedicated ``sandbox`` user in
separate-user; the invoking operator in operator-rootless), and the
``daemon.json`` lives under that owner's home. The file is read/written by the
``sandbox setup`` process itself (identity ``ROOT`` in separate-user — root
writes the sandbox user's file; the operator writes their own file in
operator-rootless). The conditional ``systemctl --user restart docker`` and the
``docker info`` readiness poll cross to the owner via
:func:`daemon_owner_crossing` — ``machinectl_cmd`` in separate-user (sentinel on,
since ``machinectl shell`` masks the inner exit), an empty LOCAL prefix in
operator-rootless (sentinel off; setup already runs as the operator, so it is a
plain local subprocess in the operator's live session — no user-manager
readiness gate needed). The file write is inode-stable (``cat > file`` semantics —
truncate-in-place, design D9) so a live dockerd watching the inode is not
surprised by a rename.

Content-aware probe (design D10): a deep-equal comparison of the *observed*
``runtimes["sandbox-ai-runsc"]`` value against the *expected* one
(``{"path": "/usr/local/libexec/sandbox-ai/runsc", "runtimeArgs":
["--oci-seccomp", "--ignore-cgroups"]}`` — see ``_EXPECTED_RUNTIME`` for why
``--ignore-cgroups`` is required under rootless). key (or file) absent →
``MISSING``; present + differing
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
from core.host_config import is_operator_rootless
from core.hydration import RESERVED_RUNTIME_KEY
from core.setup.phase_runner import (
    Identity,
    Phase,
    PhaseResult,
    daemon_owner_crossing,
    daemon_owner_user,
    probe_sandbox_pw_or_missing,
    wait_user_manager_ready,
)

if TYPE_CHECKING:
    from core.setup.phase_runner import SetupContext

# The single reserved key + its expected value (the content-aware target). The
# key is single-sourced from `core.hydration` (the compose `runtime` value must
# equal what we register here) and re-exported under the private name so the
# `cli-doctor` checks keep importing it from this module unchanged.
_RESERVED_RUNTIME_KEY = RESERVED_RUNTIME_KEY
# `--ignore-cgroups` is load-bearing under operator-rootless (F-057): rootless
# docker runs the `systemd` cgroup driver, so it passes `--systemd-cgroup` to the
# runtime. runsc's systemd-cgroup manager is NOT rootless-aware — it asks the
# *system* systemd (over the system D-Bus) to create the container's transient
# `docker-<id>.scope` under `user.slice`, which an unprivileged user may not do
# (→ "systemd error: Interactive authentication required" / "Permission denied" at
# OCI task-create). Plain runc sidesteps this because it routes to the user manager
# when rootless; runsc has no such path, so we tell it to skip systemd cgroup setup
# entirely. Cost: runsc no longer enforces the OCI cgroup CPU/memory limits (the
# C-008 clamp becomes render-time-only for gVisor containers) — restoring runtime
# enforcement via the rootless `cgroupfs` driver is a tracked follow-up (F-057).
_EXPECTED_RUNTIME: dict[str, object] = {
    "path": "/usr/local/libexec/sandbox-ai/runsc",
    "runtimeArgs": ["--oci-seccomp", "--ignore-cgroups"],
}


def _daemon_json_path(ctx: SetupContext) -> Path:
    """Resolve ``~<daemon-owner>/.config/docker/daemon.json`` via passwd.

    The owner is :func:`daemon_owner_user` (sandbox user in separate-user; the
    invoking operator in operator-rootless), so the path lands under the right
    home in both modes.
    """
    home = pwd.getpwnam(daemon_owner_user(ctx)).pw_dir
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
    if not is_operator_rootless(ctx.host_config):
        # separate-user: the sandbox user is created by L2, so a fresh-host plan
        # pass sees it absent (the MISSING signal). operator-rootless's owner is
        # the invoking operator (always present), so this guard does not apply.
        pw = probe_sandbox_pw_or_missing(ctx.host_config)
        if not isinstance(pw, pwd.struct_passwd):
            return pw
    path = _daemon_json_path(ctx)
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
    if not _runtime_registered(ctx):
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


def _runtime_registered(ctx: SetupContext) -> bool:
    """``True`` iff docker's *loaded* runtimes include the reserved key (crossed).

    Queries the daemon owner's rootless daemon via ``docker info`` — the runtime
    docker has actually **loaded**, NOT the ``daemon.json`` file. This is the
    distinction F-023 turned on: a write-success/restart-fail leaves the file
    correct while the daemon never reloaded, so a file-only probe reports
    ``ALREADY_CORRECT`` over an unregistered runtime. A failed crossing (docker
    down, sentinel absent) is treated as not-registered, so the probe/reverify
    fail toward DRIFT and ``act`` re-restarts (idempotent, fail-closed). The
    crossing is :func:`daemon_owner_crossing` (``machinectl`` in separate-user,
    sentinel on; LOCAL in operator-rootless, sentinel off).
    """
    prefix = daemon_owner_crossing(ctx)
    try:
        result = Executor().run(
            [*prefix, "/bin/bash", "-c", "docker info --format '{{json .Runtimes}}'"],
            sentinel=not is_operator_rootless(ctx.host_config),
        )
    except SandboxExecutionError:
        return False
    return _RESERVED_RUNTIME_KEY in (result.stdout or "")


def _restart_and_poll(ctx: SetupContext) -> None:
    """Restart rootless docker StartLimit-safely; poll until the runtime loads.

    Two F-023-driven properties (the parts of the F-023 work that were real, as
    opposed to the misdiagnosed "session blackout" — see ``_runtime_registered``
    and the F-023 finding):

    - ``reset-failed`` before the restart clears any prior failed state AND the
      systemd ``StartLimit`` counter, so the restart cannot trip "start of the
      service was attempted too often" and leave docker down; ``restart
      --no-block`` returns without synchronously waiting on the job — the poll,
      not the restart crossing, observes success.
    - the readiness poll waits until docker is up **and the reserved runtime is
      loaded** (``docker info`` runtimes contains the key), not merely until the
      daemon answers; ``exit 0`` ends the poll as soon as the key appears,
      ``exit 1`` after the bounded loop signals "never loaded". The sentinel
      crossing recovers that inner exit (the executor wraps the inner in a
      subshell, so the ``exit`` no longer swallows the sentinel — the actual
      F-023 root cause), so ``exit 1`` surfaces as a raised
      :class:`~core.exceptions.SandboxExecutionError` → phase FAIL.

    ``wait_user_manager_ready`` (F-014, a root-side ``is-active`` query) remains
    the cheap necessary precondition before crossing into the sandbox user in
    separate-user. operator-rootless runs locally in the operator's already-live
    session, so the gate is unnecessary and the crossing is an empty LOCAL prefix
    (sentinel off — a local command's inner exit is not masked).
    """
    op_rootless = is_operator_rootless(ctx.host_config)
    if not op_rootless:
        wait_user_manager_ready(daemon_owner_user(ctx))
    prefix = daemon_owner_crossing(ctx)
    sentinel = not op_rootless
    Executor().run(
        [
            *prefix,
            "/bin/bash",
            "-c",
            "systemctl --user reset-failed docker.service; "
            "systemctl --user restart --no-block docker",
        ],
        sentinel=sentinel,
    )
    poll = (
        "for i in $(seq 1 30); do "
        "docker info --format '{{json .Runtimes}}' 2>/dev/null "
        "| grep -qF " + _RESERVED_RUNTIME_KEY + " && exit 0; sleep 1; done; exit 1"
    )
    Executor().run([*prefix, "/bin/bash", "-c", poll], sentinel=sentinel)


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
    path = _daemon_json_path(ctx)
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
    _restart_and_poll(ctx)
    return "reserved runtime key ensured; rootless docker restarted + runtime loaded"


def _reverify(ctx: SetupContext) -> bool:
    """Confirm the key is in the file AND docker has loaded the runtime (F-023).

    File-deep-equal alone is insufficient — that was the masking gap. Reverify
    the *end state* the phase exists to produce: the runtime is registered in
    the running daemon.
    """
    path = _daemon_json_path(ctx)
    doc = _read_doc(path)
    if doc is None:
        return False
    if _observed_runtime(doc) != _EXPECTED_RUNTIME:
        return False
    return _runtime_registered(ctx)


PHASE = Phase(
    id="l6",
    name="daemon.json reserved runtime key",
    identity=Identity.ROOT,
    probe=_probe,
    act=_act,
    reverify=_reverify,
    depends_on=("l5",),
)
