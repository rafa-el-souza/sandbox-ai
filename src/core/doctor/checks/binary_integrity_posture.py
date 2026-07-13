# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""Binary-integrity posture doctor check (spec "Binary Integrity Posture Check").

Probes the host for the presence and enforcement state of four
binary-integrity mechanisms and reports a *structured*, *informational*
posture. This check ALWAYS reports PASS — it exists to surface posture, not to
gate (sandbox-ai's manifest detects accidental drift but does not provide
attack-resistant integrity; that is the host's responsibility).

Probed mechanisms (each probe degrades gracefully to NOT-INSTALLED if its
binary/file is absent — never raises):

- **dm-verity**: ``/proc/cmdline`` ``dm-verity`` / ``verity`` markers AND an
  active ``verity`` target in ``dmsetup status`` → ACTIVE / INACTIVE.
- **IMA-appraise**: ``appraise`` directives in
  ``/sys/kernel/security/ima/policy`` → APPRAISING / NOT-APPRAISING.
- **fapolicyd**: ``systemctl is-active fapolicyd`` AND
  ``fapolicyd-cli --check-status`` enforcing → ENFORCING / PERMISSIVE /
  NOT-RUNNING.
- **AIDE**: ``which aide`` AND ``/var/lib/aide/aide.db`` →
  INSTALLED-DB-PRESENT / INSTALLED-DB-MISSING / NOT-INSTALLED.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from core.doctor.types import CheckResult

_CMDLINE = Path("/proc/cmdline")
_IMA_POLICY = Path("/sys/kernel/security/ima/policy")
_AIDE_DB = Path("/var/lib/aide/aide.db")

_FULLY_HARDENED = (
    "dm-verity=ACTIVE; IMA=APPRAISING; fapolicyd=ENFORCING; AIDE=INSTALLED-DB-PRESENT"
)
_PRODUCTION_HINT = (
    "for production hosts, consider configuring dm-verity, IMA-appraise, "
    "fapolicyd, or AIDE; sandbox-ai's manifest detects accidental drift but "
    "does not provide attack-resistant integrity"
)


def _run(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
    """Run ``argv`` capturing text; ``None`` if the binary is absent/unrunnable."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _read_text(path: Path) -> str | None:
    """Read ``path`` as text; ``None`` if absent/unreadable."""
    try:
        return path.read_text()
    except OSError:
        return None


def _probe_dm_verity() -> str:
    """ACTIVE iff a kernel verity marker AND an active dmsetup verity target."""
    cmdline = _read_text(_CMDLINE) or ""
    has_marker = "dm-verity" in cmdline or "verity" in cmdline
    if not has_marker or shutil.which("dmsetup") is None:
        return "INACTIVE"
    proc = _run(["dmsetup", "status"])
    if proc is None or proc.returncode != 0:
        return "INACTIVE"
    if "verity" in proc.stdout:
        return "ACTIVE"
    return "INACTIVE"


def _probe_ima() -> str:
    """APPRAISING iff the IMA policy contains an ``appraise`` directive."""
    policy = _read_text(_IMA_POLICY)
    if policy is None:
        return "NOT-APPRAISING"
    if "appraise" in policy:
        return "APPRAISING"
    return "NOT-APPRAISING"


def _probe_fapolicyd() -> str:
    """ENFORCING / PERMISSIVE / NOT-RUNNING per systemctl + fapolicyd-cli."""
    active = _run(["systemctl", "is-active", "fapolicyd"])
    if active is None or active.stdout.strip() != "active":
        return "NOT-RUNNING"
    if shutil.which("fapolicyd-cli") is None:
        return "NOT-RUNNING"
    status = _run(["fapolicyd-cli", "--check-status"])
    if status is None:
        return "NOT-RUNNING"
    blob = (status.stdout + status.stderr).lower()
    if "enforc" in blob:
        return "ENFORCING"
    if "permissive" in blob:
        return "PERMISSIVE"
    return "ENFORCING"


def _probe_aide() -> str:
    """INSTALLED-DB-PRESENT / INSTALLED-DB-MISSING / NOT-INSTALLED."""
    if shutil.which("aide") is None:
        return "NOT-INSTALLED"
    if _AIDE_DB.exists():
        return "INSTALLED-DB-PRESENT"
    return "INSTALLED-DB-MISSING"


def check_binary_integrity_posture(user: str, distro: str | None) -> CheckResult:
    """Report the host's binary-integrity posture (always PASS, informational)."""
    del user, distro

    dm_verity = _probe_dm_verity()
    ima = _probe_ima()
    fapolicyd = _probe_fapolicyd()
    aide = _probe_aide()

    detail = (
        f"dm-verity={dm_verity}; IMA={ima}; "
        f"fapolicyd={fapolicyd}; AIDE={aide}"
    )

    fully_hardened = detail == _FULLY_HARDENED
    remediation = None if fully_hardened else _PRODUCTION_HINT

    return CheckResult(
        status="pass",
        name="binary integrity posture",
        detail=(
            detail + " (posture is fully hardened)"
            if fully_hardened
            else detail
        ),
        remediation=remediation,
    )


__all__ = ["check_binary_integrity_posture"]
