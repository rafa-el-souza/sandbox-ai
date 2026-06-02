"""Execution-mode marker reader/writer (design D6).

Setup persists the provisioned execution mode in a root-owned host-plane marker
``/usr/local/libexec/sandbox-ai/setup-state.json`` (root:root ``0644``,
world-readable; same trust tier as the dispatcher manifest, NOT under
``sandbox_ai_home()``). The marker is keyed per operator::

    {"operators": {"<name>": {"mode": "<mode>"}}}

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
from pathlib import Path

from core.binary_install import RESERVED_DIR
from core.host_config import DockerExecutionMode

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


def write_mode(operator: str, mode: DockerExecutionMode) -> None:
    """Record ``mode`` for ``operator`` in the marker, preserving other entries.

    Read-merge-write: existing entries for *other* operators are preserved; the
    entry for ``operator`` is set (or overwritten) to ``mode``. The write is
    atomic (temp file in the marker's directory + ``os.replace``) and the file
    lands mode ``0o644``. Ownership is NOT set here (root ownership is the host
    batch's concern — this writer is called under whatever identity the caller
    holds).
    """
    try:
        raw = MARKER_PATH.read_text()
        data = json.loads(raw)
    except FileNotFoundError:
        data = {}
    operators = dict(data.get("operators", {}))
    operators = {**operators, operator: {"mode": mode.value}}
    merged = {**data, "operators": operators}

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
