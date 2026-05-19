"""L1 — kernel sysctl drop-in + verify-only checks (user-independent).

Second phase of ``sandbox setup`` (spec "Phase Execution Order" step 2). L1
owns one on-disk file and additionally verifies (but does not fix) two host
properties. It is deliberately **user-independent**: L1 runs before L2 creates
the sandbox user, so nothing in this phase may resolve an OS user (the
uid-scoped systemd ``Delegate=yes`` drop-in moved out to its own phase
``l2a_delegate``, which depends on L2 — see that module).

- ``/etc/sysctl.d/49-sandbox-ai.conf`` (mode 0644, root:root) —
  ``user.max_user_namespaces=15000`` always; ``kernel.unprivileged_userns_clone
  =1`` *only on Debian-family* (the knob exists only on Debian-family kernels —
  design "kernel.unprivileged_userns_clone … setup's L1 phase must branch on
  /etc/os-release ID family"). Applied immediately via ``sysctl -w``.
- verify-only: ACL FS support and the cgroup-v2 unified hierarchy. These are
  not fixable by setup; an unmet one surfaces as a ``CONFLICT`` refusal (the
  spec's ``✗ verify-only failure`` marker).

Content-aware probe (design D10): the expected file *body* is rendered from
the current source (the distro family decides whether the
``unprivileged_userns_clone`` line is present). The probe byte-compares
rendered-expected against the on-disk file: absent → ``MISSING``;
present-but-different → ``DRIFT``; matching → ``ALREADY_CORRECT``; an unmet
verify-only property → ``CONFLICT``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from core.doctor import detect_distro
from core.setup.phase_runner import Identity, Phase, PhaseResult

if TYPE_CHECKING:
    from core.setup.phase_runner import SetupContext

_MANAGED_HEADER = "# sandbox-ai managed — do not edit; rerun 'sudo sandbox setup'"

_SYSCTL_DROPIN = Path("/etc/sysctl.d/49-sandbox-ai.conf")

_MAX_USER_NS = 15000


def _is_debian_family() -> bool:
    """``True`` iff the host is Debian-family (``kernel.unprivileged_userns_clone``).

    The knob exists only on Debian-family kernels (design / spec); on
    Fedora/RHEL/Arch it must be omitted entirely (writing an absent knob via
    ``sysctl -w`` errors).
    """
    return detect_distro() == "debian"


def render_sysctl_dropin() -> str:
    """Render the expected ``/etc/sysctl.d/49-sandbox-ai.conf`` body."""
    lines = [_MANAGED_HEADER, f"user.max_user_namespaces={_MAX_USER_NS}"]
    if _is_debian_family():
        lines.append("kernel.unprivileged_userns_clone=1")
    return "\n".join(lines) + "\n"


def _acl_fs_supported() -> bool:
    """Verify-only: the filesystem under ``/`` supports POSIX ACLs.

    Probes via ``getfacl`` on ``/`` — a kernel/mount without ACL support
    makes ``getfacl`` fail. Pure read; no mutation.
    """
    try:
        proc = subprocess.run(
            ["getfacl", "-p", "/"], capture_output=True, text=True, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _cgroup_v2_active() -> bool:
    """Verify-only: the unified cgroup-v2 hierarchy is mounted.

    The unified hierarchy mounts ``cgroup2`` at ``/sys/fs/cgroup`` and exposes
    ``/sys/fs/cgroup/cgroup.controllers``. Pure read; no mutation.
    """
    return Path("/sys/fs/cgroup/cgroup.controllers").exists()


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


def _probe(_ctx: SetupContext) -> tuple[PhaseResult, str]:
    """Content-aware L1 probe (see module docstring). No OS-user resolution."""
    if not _acl_fs_supported():
        return (
            PhaseResult.CONFLICT,
            "verify-only failure: the root filesystem does not support POSIX "
            "ACLs (setfacl/getfacl). setup cannot fix this.",
        )
    if not _cgroup_v2_active():
        return (
            PhaseResult.CONFLICT,
            "verify-only failure: the cgroup v2 unified hierarchy is not "
            "active (/sys/fs/cgroup/cgroup.controllers absent). setup cannot "
            "fix this.",
        )

    expected_sysctl = render_sysctl_dropin()
    observed_sysctl = _read(_SYSCTL_DROPIN)

    if observed_sysctl is None:
        return PhaseResult.MISSING, f"{_SYSCTL_DROPIN} absent"
    if observed_sysctl != expected_sysctl:
        return PhaseResult.DRIFT, f"{_SYSCTL_DROPIN} content differs from source"
    return PhaseResult.ALREADY_CORRECT, "sysctl drop-in matches source"


def _write_root_file(path: Path, body: str, mode: int) -> None:
    """Write ``body`` to ``path`` root:root at ``mode`` (parent dirs 0755)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    os.chmod(path, mode)
    os.chown(path, 0, 0)


def _act(_ctx: SetupContext) -> str:
    """Write the sysctl drop-in and apply it immediately."""
    _write_root_file(_SYSCTL_DROPIN, render_sysctl_dropin(), 0o644)

    applied = [f"user.max_user_namespaces={_MAX_USER_NS}"]
    subprocess.run(
        ["sysctl", "-w", f"user.max_user_namespaces={_MAX_USER_NS}"],
        capture_output=True,
        text=True,
        check=True,
    )
    if _is_debian_family():
        subprocess.run(
            ["sysctl", "-w", "kernel.unprivileged_userns_clone=1"],
            capture_output=True,
            text=True,
            check=True,
        )
        applied.append("kernel.unprivileged_userns_clone=1")

    return f"wrote {_SYSCTL_DROPIN} ({', '.join(applied)})"


def _reverify(_ctx: SetupContext) -> bool:
    """L1 converged iff the sysctl drop-in now byte-matches rendered source."""
    return _read(_SYSCTL_DROPIN) == render_sysctl_dropin()


PHASE = Phase(
    id="l1",
    name="kernel sysctl drop-in",
    identity=Identity.ROOT,
    probe=_probe,
    act=_act,
    reverify=_reverify,
    depends_on=("l0",),
)

__all__ = [
    "PHASE",
    "render_sysctl_dropin",
]
