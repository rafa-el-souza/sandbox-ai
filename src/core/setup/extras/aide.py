"""Optional AIDE integration phase (sticky opt-in, design D11).

AIDE (Advanced Intrusion Detection Environment) is a file-integrity monitor: it
snapshots a baseline DB and reports drift on later ``aide --check`` runs. This
phase writes an AIDE ``aide.conf.d`` drop-in that registers sandbox-ai's two
reserved-namespace managed binaries
(``/usr/local/libexec/sandbox-ai/dispatch`` + ``/usr/local/libexec/sandbox-ai/runsc``)
for ``NORMAL`` monitoring so a tampered dispatcher/runsc is caught.

Phase shape (per the spec's "Optional AIDE Integration Phase" requirement):

- **Probe (content-aware, design D10)** — refuse if ``aide`` is not on PATH or
  ``/etc/aide/aide.conf.d/`` is absent (older AIDE without drop-in support);
  otherwise compute the canonical two-managed-binary snippet and byte-compare
  against the existing drop-in. Match → ``ALREADY_CORRECT``; absent →
  ``MISSING``; differs → ``DRIFT``.
- **Act** — write the drop-in mode 0644 root:root with the managed header,
  optionally validate via ``aide --config-check`` when that flag is supported.
- **Reverify** — the drop-in is present with the expected content. **DB
  init**: ``aide --init`` walks the whole filesystem (10+ minutes typical);
  setup MUST NOT auto-run it. When ``/var/lib/aide/aide.db`` is absent on a
  first install, the phase surfaces the ``aide --init`` operator prompt
  *through the apply outcome's detail text* — :func:`core.setup.cli_flow.
  summarize_apply` renders each phase's detail into the apply-pass
  finalization block, so a detail line IS the finalization-summary channel the
  spec requires (no new channel is invented; see the orchestrator brief).

Identity ``ROOT``: all operations are root-local filesystem reads/writes or a
root-local ``aide`` invocation — nothing crosses a privilege boundary.

Runs after the entire base ceremony (``depends_on=("l8",)``) — the managed
binaries only exist post-L6a/L6.5. A failure here does NOT roll back L0..L8.
"""

from __future__ import annotations

import os
import shutil
from typing import TYPE_CHECKING

from core.exceptions import SandboxExecutionError
from core.executor import Executor
from core.setup.phase_runner import Identity, Phase, PhaseResult

if TYPE_CHECKING:
    from core.setup.phase_runner import SetupContext

_DISPATCH_PATH = "/usr/local/libexec/sandbox-ai/dispatch"
_RUNSC_PATH = "/usr/local/libexec/sandbox-ai/runsc"

_CONF_D_DIR = "/etc/aide/aide.conf.d"
_CONF_DROPIN_PATH = "/etc/aide/aide.conf.d/sandbox-ai.conf"
_AIDE_DB_PATH = "/var/lib/aide/aide.db"

_MANAGED_HEADER = "# sandbox-ai managed — do not edit; rerun 'sudo sandbox setup'"

# The canonical drop-in body (spec "Optional AIDE Integration Phase" → Act).
_CONF_CONTENT = (
    f"{_MANAGED_HEADER}\n"
    f"{_DISPATCH_PATH} NORMAL\n"
    f"{_RUNSC_PATH} NORMAL\n"
)

_AIDE_ABSENT_REFUSAL = (
    "aide not installed. Run: sudo apt install aide (or sudo dnf install "
    "aide; Arch: sudo pacman -S aide), then re-run."
)
_CONF_D_ABSENT_REFUSAL = (
    "AIDE on this host does not support /etc/aide/aide.conf.d/ drop-ins; "
    "manually integrate per docs/setup-guide.md"
)
_DB_INIT_PROMPT = (
    "AIDE conf.d snippet installed. To begin monitoring, run: sudo aide "
    "--init (warning: walks the entire filesystem; can take 10+ minutes), "
    "then schedule periodic checks via cron (e.g., daily aide --check at "
    "off-peak hours)."
)


def _aide_installed() -> bool:
    """``True`` iff ``aide`` resolves on PATH (``which aide``)."""
    return shutil.which("aide") is not None


def _read_existing() -> str | None:
    """Return the existing drop-in's content, or ``None`` if absent."""
    try:
        with open(_CONF_DROPIN_PATH, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def _aide_db_present() -> bool:
    """``True`` iff the AIDE baseline DB exists at ``/var/lib/aide/aide.db``."""
    return os.path.exists(_AIDE_DB_PATH)


def _config_check_supported() -> bool:
    """``True`` iff this ``aide`` build accepts ``--config-check``.

    Probed via ``aide --help``: older AIDE builds lack the flag, so a literal
    substring check on the help text decides whether the act validates the
    drop-in. Any failure to introspect (no help, non-zero exit) is treated as
    unsupported — validation is best-effort per the spec ("if AIDE's version
    supports the flag; otherwise skip validation").
    """
    try:
        result = Executor().run(["aide", "--help"])
    except SandboxExecutionError:
        return False
    return "--config-check" in (result.stdout or "")


def _probe(ctx: SetupContext) -> tuple[PhaseResult, str]:
    """Content-aware probe over the expected vs. observed conf.d drop-in.

    Refusals (``CONFLICT`` — the runner never overwrites on a refusal): aide
    not installed, or ``/etc/aide/aide.conf.d/`` is missing (older AIDE without
    drop-in support). Otherwise byte-compare the canonical snippet against the
    existing drop-in.
    """
    if not _aide_installed():
        return PhaseResult.CONFLICT, _AIDE_ABSENT_REFUSAL
    if not os.path.isdir(_CONF_D_DIR):
        return PhaseResult.CONFLICT, _CONF_D_ABSENT_REFUSAL

    existing = _read_existing()
    if existing is None:
        return (
            PhaseResult.MISSING,
            f"AIDE conf.d drop-in absent at {_CONF_DROPIN_PATH}; will write it",
        )
    if existing == _CONF_CONTENT:
        return (
            PhaseResult.ALREADY_CORRECT,
            f"AIDE conf.d drop-in at {_CONF_DROPIN_PATH} matches the canonical "
            "managed-binary snippet",
        )
    return (
        PhaseResult.DRIFT,
        f"AIDE conf.d drop-in at {_CONF_DROPIN_PATH} differs from the "
        "canonical managed-binary snippet; will re-render",
    )


def _act(ctx: SetupContext) -> str:
    """Write the conf.d drop-in (0644 root:root); optionally config-check it.

    The ``aide --init`` operator prompt is appended to the returned detail
    when ``/var/lib/aide/aide.db`` is absent (first install): the runner stores
    this detail on the apply outcome and ``cli_flow.summarize_apply`` renders
    it into the finalization block — that detail line IS the finalization
    summary the spec's "DB initialization" clause requires. Setup never runs
    ``aide --init`` itself (10+ minute filesystem walk).
    """
    with open(_CONF_DROPIN_PATH, "w", encoding="utf-8") as fh:
        fh.write(_CONF_CONTENT)
    os.chmod(_CONF_DROPIN_PATH, 0o644)
    os.chown(_CONF_DROPIN_PATH, 0, 0)

    detail = f"wrote {_CONF_DROPIN_PATH} (0644 root:root)"
    if _config_check_supported():
        Executor().run(["aide", "--config-check"])
        detail = f"{detail}; validated via aide --config-check"

    if not _aide_db_present():
        detail = f"{detail}. {_DB_INIT_PROMPT}"
    return detail


def _reverify(ctx: SetupContext) -> bool:
    """``True`` iff the drop-in is present with exactly the canonical content."""
    return _read_existing() == _CONF_CONTENT


PHASE = Phase(
    id="aide",
    name="AIDE conf.d drop-in (optional integration)",
    identity=Identity.ROOT,
    probe=_probe,
    act=_act,
    reverify=_reverify,
    depends_on=("l8",),
)
