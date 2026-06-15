# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Execution-mode marker reader/writer (design D6).

Setup persists the provisioned execution mode in a root-owned host-plane marker
``/usr/local/libexec/sandbox-ai/setup-state.json`` (root:root ``0644``,
world-readable; same trust tier as the dispatcher manifest, NOT under
``sandbox_ai_home()``). The marker is keyed per operator, and each entry carries
the setup-determined host facts (mode-conditional per D-A)::

    {"operators": {"<name>": {
        "mode": "<mode>",
        "workspace_bridge_group": "<group>",
        "workspace_bridge_gid": <int>,
        "docker_unprivileged_user": "<user>"  # separate-user entries only
    }}}

because operator-rootless runs a per-operator daemon while separate-user shares
one ``sandbox`` daemon — a host can legitimately carry ``alice=operator-rootless``
+ ``bob=separate-user``.

This module is the **single source** of the marker path and the only place that
parses/serializes the marker file. It is deliberately dependency-light: it imports
``RESERVED_DIR`` from :mod:`core.binary_install` (to single-source the directory)
and :class:`DockerExecutionMode` from :mod:`core.host_config` (a one-way
dependency — ``core.host_config`` may read this module in a later milestone
without an import cycle, which is why the marker reader lives at
``core.setup_state`` rather than under ``core.setup`` — ``core.setup.*`` imports
``core.host_config``).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from core.binary_install import RESERVED_DIR
from core.exceptions import SandboxExecutionError
from core.host_config import DockerExecutionMode

if TYPE_CHECKING:
    from core.json_types import JsonValue

# Single-source the marker path off the reserved binary directory (D6).
MARKER_PATH = RESERVED_DIR / "setup-state.json"


def read_mode(operator: str) -> DockerExecutionMode | None:
    """Return the recorded execution mode for ``operator``, or ``None``.

    Parses the marker JSON ``{"operators": {"<name>": {"mode": "<mode>"}}}`` and
    returns the operator's mode as a :class:`DockerExecutionMode`. Returns
    ``None`` when the marker file is absent OR has no entry for ``operator``.

    A missing file is tolerated (the unprovisioned case) and yields ``None``.
    """
    try:
        raw = MARKER_PATH.read_text()
    except FileNotFoundError:
        return None
    data = json.loads(raw)
    operators = data.get("operators", {})
    entry = operators.get(operator)
    if entry is None:
        return None
    return DockerExecutionMode(entry["mode"])


@dataclass(frozen=True)
class MarkerEntry:
    """The full per-operator marker record (design D-A).

    Carries the setup-determined host facts: the provisioned execution mode, the
    workspace bridge group name + its gid (mode-scoped per D-F), and — for
    separate-user entries only — the unprivileged daemon user
    (``docker_unprivileged_user is None`` for operator-rootless, whose daemon
    owner is intrinsic via ``getpass.getuser()``).
    """

    mode: DockerExecutionMode
    workspace_bridge_group: str
    workspace_bridge_gid: int
    docker_unprivileged_user: str | None


def read_entry(operator: str) -> MarkerEntry | None:
    """Return the full :class:`MarkerEntry` for ``operator``, or ``None``.

    Returns ``None`` when the marker file is absent, has no entry for
    ``operator``, OR the entry is a **legacy mode-only** record (a C-004-era
    marker missing ``workspace_bridge_group`` / ``workspace_bridge_gid``) — such
    a host is treated as not-yet-provisioned so setup rewrites the full entry.
    """
    try:
        raw = MARKER_PATH.read_text()
    except FileNotFoundError:
        return None
    data = json.loads(raw)
    operators = data.get("operators", {})
    entry = operators.get(operator)
    if entry is None:
        return None
    if "workspace_bridge_group" not in entry or "workspace_bridge_gid" not in entry:
        return None
    return MarkerEntry(
        mode=DockerExecutionMode(entry["mode"]),
        workspace_bridge_group=entry["workspace_bridge_group"],
        workspace_bridge_gid=entry["workspace_bridge_gid"],
        docker_unprivileged_user=entry.get("docker_unprivileged_user"),
    )


def write_mode(
    operator: str,
    mode: DockerExecutionMode,
    *,
    workspace_bridge_group: str,
    workspace_bridge_gid: int,
    docker_unprivileged_user: str | None = None,
) -> None:
    """Record the full marker entry for ``operator``, preserving other entries.

    Read-merge-write: existing entries for *other* operators are preserved; the
    entry for ``operator`` is set (or overwritten) to the mode + the
    setup-determined host facts. ``docker_unprivileged_user`` is included in the
    entry **iff** it is not ``None`` (separate-user entries carry it; op-rootless
    omits it). The write is atomic (temp file in the marker's directory +
    ``os.replace``) and the file lands mode ``0o644``. Ownership is NOT set here
    (root ownership is the host batch's concern — this writer is called under
    whatever identity the caller holds).
    """
    entry: dict[str, JsonValue] = {
        "mode": mode.value,
        "workspace_bridge_group": workspace_bridge_group,
        "workspace_bridge_gid": workspace_bridge_gid,
    }
    if docker_unprivileged_user is not None:
        entry["docker_unprivileged_user"] = docker_unprivileged_user

    data: dict[str, JsonValue]
    try:
        raw = MARKER_PATH.read_text()
    except FileNotFoundError:
        data = {}
    else:
        parsed: JsonValue = json.loads(raw)
        if not isinstance(parsed, dict):
            raise SandboxExecutionError(
                f"[FATAL] Sandbox Execution Fault: {MARKER_PATH} is not a JSON "
                f"object; refusing to overwrite a malformed mode marker."
            )
        data = parsed
    raw_operators: JsonValue = data.get("operators", {})
    if not isinstance(raw_operators, dict):
        raise SandboxExecutionError(
            f"[FATAL] Sandbox Execution Fault: {MARKER_PATH} has a non-object "
            f"'operators' field; refusing to overwrite a malformed mode marker."
        )
    operators: dict[str, JsonValue] = {**raw_operators, operator: entry}
    merged: dict[str, JsonValue] = {**data, "operators": operators}

    MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(MARKER_PATH.parent), prefix=".setup-state-", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(merged, f, indent=2, sort_keys=True)
            f.write("\n")
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, MARKER_PATH)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def write_mode_root_owned(
    operator: str,
    mode: DockerExecutionMode,
    *,
    workspace_bridge_group: str,
    workspace_bridge_gid: int,
    docker_unprivileged_user: str | None = None,
) -> None:
    """Write the marker for ``operator`` and force root:root ``0644`` ownership.

    The single source for the *root-owned* marker write both setup paths need:
    the operator-rootless host-root batch (``host_batch._apply_marker``, run under
    the one ``sudo`` escalation) and separate-user setup (run as root). ``write_mode``
    lands the content + mode ``0644`` but not ownership; this wrapper additionally
    ``chown``s the marker to root (the marker is a root-owned reserved-namespace
    artifact, D6). The caller MUST hold root.
    """
    write_mode(
        operator,
        mode,
        workspace_bridge_group=workspace_bridge_group,
        workspace_bridge_gid=workspace_bridge_gid,
        docker_unprivileged_user=docker_unprivileged_user,
    )
    os.chmod(MARKER_PATH, 0o644)
    os.chown(MARKER_PATH, 0, 0)


class ModeMarkerMissing(LookupError):
    """The setup-state marker records no execution mode for the operator.

    Raised by :func:`resolve_execution_mode` (the runtime mode-resolution path)
    when the marker is absent or has no entry for the current operator. The
    runtime fails closed on this — a provisioned host always carries a marker
    entry (setup writes it in both modes), so its absence means "not provisioned".
    """


def resolve_execution_mode(operator: str) -> DockerExecutionMode:
    """Resolve ``operator``'s execution mode from the marker (runtime authority, D11).

    The runtime parallel of setup's mode resolution: the marker is the **single
    authority** for the execution mode (it is no longer a user-editable toml
    field). Returns the recorded :class:`DockerExecutionMode`; raises
    :class:`ModeMarkerMissing` (fail-closed) when no entry exists for ``operator``
    — the caller surfaces "run `sudo sandbox setup` first".
    """
    mode = read_mode(operator)
    if mode is None:
        raise ModeMarkerMissing(
            f"no execution mode recorded for operator {operator!r}. "
            "Run `sudo sandbox setup` first."
        )
    return mode
