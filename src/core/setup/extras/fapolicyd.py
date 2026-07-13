# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""Optional fapolicyd integration phase (sticky opt-in, design D11).

fapolicyd is a userspace allow-listing daemon: a binary not in its trust DB
cannot exec. sandbox-ai's two managed binaries live in the reserved namespace
(``/usr/local/libexec/sandbox-ai/dispatch`` + ``/usr/local/libexec/sandbox-ai/runsc``);
under an enforcing fapolicyd they must be explicitly trusted or the orchestrator
cannot launch them. This phase writes a fapolicyd ``trust.d`` drop-in recording
each managed binary's ``<path> <size> <sha256>`` and reloads the trust DB.

Phase shape (per the spec's "Optional Fapolicyd Integration Phase"
requirement):

- **Probe (content-aware, design D10)** — refuse if ``fapolicyd`` is not on
  PATH or ``/etc/fapolicyd/trust.d/`` is absent (older fapolicyd without
  drop-in support); otherwise compute the expected trust content from the
  *current* on-disk managed binaries (size + sha256 are recomputed every probe,
  so a dispatcher/runsc rebuild is detected as ``DRIFT`` — that is the
  content-aware signal). Byte-compare against the existing drop-in: match →
  ``ALREADY_CORRECT``; absent → ``MISSING``; differs → ``DRIFT``.
- **Act** — render + write the drop-in mode 0644 root:root with the managed
  header, then ``fapolicyd-cli --update``. If fapolicyd is installed but not
  running, warn (do not fail).
- **Reverify** — ``fapolicyd-cli --check-trust file=<dispatch>`` reports
  ``trusted: yes``.

Identity ``ROOT``: every operation here is a root-local filesystem read/write
or a root-local ``fapolicyd-cli`` invocation — nothing crosses a privilege
boundary, so :func:`core.setup.phase_runner.route` yields an empty prefix and
this module never imports ``machinectl_cmd``.

The phase runs *after* the entire base ceremony (``depends_on=("l8",)``): the
managed binaries only exist once L6a (runsc) and L6.5 (dispatcher) have
installed them. A failure here does NOT roll back L0..L8 (the phase carries no
``rollback`` callable, and the runner only rolls back phases that do); the
operator re-runs once the underlying issue is fixed.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from typing import TYPE_CHECKING

from core.exceptions import SandboxExecutionError
from core.executor import Executor
from core.setup.phase_runner import Identity, Phase, PhaseResult

if TYPE_CHECKING:
    from core.setup.phase_runner import SetupContext

# Reserved-namespace managed binaries (spec "Reserved Namespace File
# Ownership"). Order is load-bearing: the rendered trust file lists them in
# this fixed order so the byte-compare is deterministic.
_DISPATCH_PATH = "/usr/local/libexec/sandbox-ai/dispatch"
_RUNSC_PATH = "/usr/local/libexec/sandbox-ai/runsc"
_MANAGED_BINARIES: tuple[str, ...] = (_DISPATCH_PATH, _RUNSC_PATH)

_TRUST_DROPIN_DIR = "/etc/fapolicyd/trust.d"
_TRUST_DROPIN_PATH = "/etc/fapolicyd/trust.d/sandbox-ai.trust"

_MANAGED_HEADER = "# sandbox-ai managed — do not edit; rerun 'sudo sandbox setup'"

_FAPOLICYD_ABSENT_REFUSAL = (
    "fapolicyd not installed. Run: sudo apt install fapolicyd (or sudo dnf "
    "install fapolicyd; Arch: paru -S fapolicyd), then re-run."
)
_TRUST_D_ABSENT_REFUSAL = (
    "fapolicyd installed but trust.d directory missing; check fapolicyd "
    "installation"
)
_FAPOLICYD_INACTIVE_WARNING = (
    "fapolicyd installed but not running. Run: sudo systemctl enable --now "
    "fapolicyd to start enforcement."
)


def _sha256_hex(path: str) -> str:
    """Return the lowercase hex sha256 of ``path``'s full content."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _render_trust_content() -> str:
    """Render the canonical trust-file body from the *current* binaries.

    One ``<absolute-path> <size-bytes> <sha256-hex>`` line per managed binary
    in the fixed :data:`_MANAGED_BINARIES` order, under the managed header. The
    size + sha256 are read live from disk so a rebuilt dispatcher/runsc yields
    different content — this is what makes the probe content-aware (design
    D10): a wheel upgrade or ``--update-runsc`` changes the bytes here and the
    probe reports ``DRIFT`` rather than silently skipping.
    """
    lines = [_MANAGED_HEADER]
    for path in _MANAGED_BINARIES:
        size = os.path.getsize(path)
        sha = _sha256_hex(path)
        lines.append(f"{path} {size} {sha}")
    return "\n".join(lines) + "\n"


def _read_existing() -> str | None:
    """Return the existing drop-in's content, or ``None`` if absent."""
    try:
        with open(_TRUST_DROPIN_PATH, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def _fapolicyd_installed() -> bool:
    """``True`` iff ``fapolicyd`` resolves on PATH (``which fapolicyd``)."""
    return shutil.which("fapolicyd") is not None


def _fapolicyd_active() -> bool:
    """``True`` iff ``systemctl is-active fapolicyd`` reports active.

    A non-zero exit (``inactive`` / ``failed`` / no systemd) is treated as
    not-active; the act warns but does not fail in that case (spec).
    """
    try:
        Executor().run(["systemctl", "is-active", "fapolicyd"])
    except SandboxExecutionError:
        return False
    return True


def _probe(ctx: SetupContext) -> tuple[PhaseResult, str]:
    """Content-aware probe over the expected vs. observed trust drop-in.

    Refusals (``CONFLICT`` — the runner never overwrites on a refusal):
    fapolicyd not installed, or its ``trust.d`` directory is missing (older
    fapolicyd without drop-in support). Otherwise compute the expected content
    from the live managed binaries and byte-compare against the drop-in.
    """
    if not _fapolicyd_installed():
        return PhaseResult.CONFLICT, _FAPOLICYD_ABSENT_REFUSAL
    if not os.path.isdir(_TRUST_DROPIN_DIR):
        return PhaseResult.CONFLICT, _TRUST_D_ABSENT_REFUSAL

    expected = _render_trust_content()
    existing = _read_existing()
    if existing is None:
        return (
            PhaseResult.MISSING,
            f"fapolicyd trust drop-in absent at {_TRUST_DROPIN_PATH}; "
            "will write it and reload the trust DB",
        )
    if existing == expected:
        return (
            PhaseResult.ALREADY_CORRECT,
            f"fapolicyd trust drop-in at {_TRUST_DROPIN_PATH} matches the "
            "current managed-binary shas",
        )
    return (
        PhaseResult.DRIFT,
        f"fapolicyd trust drop-in at {_TRUST_DROPIN_PATH} records stale "
        "size/sha for a managed binary; will re-render and reload",
    )


def _act(ctx: SetupContext) -> str:
    """Render + write the trust drop-in (0644 root:root); reload the trust DB.

    Setup runs as root, so ``open(...).write`` lands a root-owned file;
    ``os.chmod`` pins mode 0644 (``os.chown`` to ``0:0`` is a no-op identity
    write under root but kept explicit for the spec's "0644 root:root"
    contract). ``fapolicyd-cli --update`` reloads the in-kernel trust DB. A
    not-running fapolicyd is surfaced as a warning in the returned detail, not
    a failure (spec).
    """
    content = _render_trust_content()
    with open(_TRUST_DROPIN_PATH, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.chmod(_TRUST_DROPIN_PATH, 0o644)
    os.chown(_TRUST_DROPIN_PATH, 0, 0)
    Executor().run(["fapolicyd-cli", "--update"])
    detail = (
        f"wrote {_TRUST_DROPIN_PATH} (0644 root:root) and reloaded the "
        "fapolicyd trust DB"
    )
    if not _fapolicyd_active():
        detail = f"{detail}; WARNING: {_FAPOLICYD_INACTIVE_WARNING}"
    return detail


def _reverify(ctx: SetupContext) -> bool:
    """``True`` iff ``fapolicyd-cli --check-trust`` reports the dispatcher trusted.

    ``fapolicyd-cli --check-trust file=<dispatch>`` prints a line containing
    ``trusted: yes`` for a trusted binary; a non-zero exit or any other text
    means not-yet-trusted (the trust DB did not pick up the drop-in).
    """
    try:
        result = Executor().run(
            ["fapolicyd-cli", "--check-trust", f"file={_DISPATCH_PATH}"]
        )
    except SandboxExecutionError:
        return False
    return "trusted: yes" in (result.stdout or "")


PHASE = Phase(
    id="fapolicyd",
    name="fapolicyd trust drop-in (optional integration)",
    identity=Identity.ROOT,
    probe=_probe,
    act=_act,
    reverify=_reverify,
    depends_on=("l8",),
)
