# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Operator-rootless host-root batch model (design D5a / D2 / D6).

Operator-rootless ``sandbox setup`` runs as the *operator*, unprivileged. The
only genuinely-privileged prerequisites are an enumerated, dependency-ordered
set of host-root mutations that setup escalates **once** (via ``sudo sandbox
_bootstrap-host`` — §8-B, not this module) when, and only when, unsatisfied.

This module is the model of that batch:

- :data:`HOST_ROOT_BATCH` — the canonical, load-bearing apply order. The tuple
  index IS the apply order; ``MARKER`` is last so a mid-batch failure can never
  reach the root-owned mode-marker write (crash-safety is structural, not a
  ``try``/``except``).
- :func:`apply_host_root_batch` — the root-side applier loop (run by §8-B under
  the single escalation). It walks :data:`HOST_ROOT_BATCH`, applies only the
  requested items, and any applier raising aborts the loop — later items (incl.
  ``MARKER``) never run.
- :func:`classify_host_root_batch` — the unprivileged, content-aware planner.
  It inspects the host (no mutation, no sudo) and returns only the *unsatisfied*
  items plus the computed :class:`BatchParams`.
- :func:`build_bootstrap_argv` / :func:`render_remediation_block` — the two
  operator-facing renderings of the batch (the escalation argv consumed by
  §8-B, and the copy-pasteable ``sudo`` fallback when the operator has no
  escalation path at all).

Each applier is a **thin wrapper** over the existing setup helpers (l1's sysctl
drop-in, l2's subuid/groupadd logic, l2a's Delegate drop-in, l6a/binary_install's
runsc install, setup_state's marker write); the batch reproduces the proven
op-rootless recipe via those helpers rather than re-deriving any flag list. The
one exception is ``LINGER`` (``loginctl enable-linger <operator>``): linger
self-service (the operator enabling their own linger) is polkit-gated and works
only on some distros (validated: Ubuntu yes; Debian/Fedora/Arch need root), so it
is a host-root batch item rather than operator-space work in L5 — L5's
operator-rootless path no longer touches linger.
"""

from __future__ import annotations

import grp
import pwd
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from core.binary_install import detect_drift, install_pinned
from core.doctor import detect_distro
from core.host_config import (
    DockerExecutionMode,
    HostConfig,
    HostSettings,
    autodetect_workspace_bridge_gid_recommendation,
    parse_subgid_for_user,
    parse_subuid_for_user,
)
from core.setup import l1_kernel, l2a_delegate
from core.setup.subid import pick_free_subid_block
from core.setup_state import read_mode, write_mode_root_owned

if TYPE_CHECKING:
    from collections.abc import Callable

    from core.setup.phase_runner import SetupContext

# ---------------------------------------------------------------------------
# Batch enumeration and canonical order
# ---------------------------------------------------------------------------


class BatchItem(StrEnum):
    """The enumerated host-root prerequisites (design D5a)."""

    SUBID = "subid"
    GROUPADD = "groupadd"
    SYSCTL = "sysctl"
    NFTABLES = "nftables"
    DELEGATE = "delegate"
    LINGER = "linger"
    RUNSC = "runsc"
    MARKER = "marker"


# THE load-bearing order. Index in this tuple IS the apply order. MARKER LAST so
# a mid-batch failure can never reach the root-owned mode-marker write. LINGER
# sits among the pre-dockerd prerequisites (after DELEGATE) — the operator's
# user manager must persist before L5 installs rootless dockerd into it.
HOST_ROOT_BATCH: tuple[BatchItem, ...] = (
    BatchItem.SUBID,
    BatchItem.GROUPADD,
    BatchItem.SYSCTL,
    BatchItem.NFTABLES,
    BatchItem.DELEGATE,
    BatchItem.LINGER,
    BatchItem.RUNSC,
    BatchItem.MARKER,
)


@dataclass(frozen=True)
class BatchParams:
    """The typed inputs every applier reads (no host re-derivation root-side).

    The §8-B root sub-step receives these as wire flags and reconstructs this
    object; the appliers run root-side and trust it (the dispatcher trusts
    upstream validation — the same model the batch uses here).
    """

    operator: str
    operator_uid: int
    bridge_group: str
    bridge_gid: int  # autodetected inside the operator's subgid range (unprivileged)
    distro_family: str  # for the SYSCTL branch + NFTABLES module set
    mode: DockerExecutionMode  # for the marker write (always OPERATOR_ROOTLESS here)


# ---------------------------------------------------------------------------
# nftables persist artifact (the one NEW owned namespace in this module)
# ---------------------------------------------------------------------------

_MANAGED_HEADER = "# sandbox-ai managed — do not edit; rerun 'sudo sandbox setup'"

_MODULES_LOAD_DROPIN = Path("/etc/modules-load.d/sandbox-ai.conf")


def _subid_range_arg() -> str:
    """Render the ``<start>-<end>`` usermod range for the next free block.

    Derived from :func:`core.setup.subid.pick_free_subid_block` — the SINGLE
    seam shared by the real append and the remediation preview (no second
    hand-typed literal).
    """
    start, size = pick_free_subid_block()
    return f"{start}-{start + size - 1}"


def _nftables_modules(distro_family: str) -> tuple[str, ...]:
    """The kernel module set to load+persist for ``distro_family``.

    From the validated op-rootless recipe: fedora needs ``ip_tables``; arch
    needs both ``nf_tables`` and ``ip_tables``; every other family (ubuntu /
    debian) needs only ``nf_tables``.
    """
    if distro_family == "fedora":
        return ("ip_tables",)
    if distro_family == "arch":
        return ("nf_tables", "ip_tables")
    return ("nf_tables",)


def _render_modules_load_dropin(distro_family: str) -> str:
    """Render the expected ``/etc/modules-load.d/sandbox-ai.conf`` body."""
    lines = [_MANAGED_HEADER, *_nftables_modules(distro_family)]
    return "\n".join(lines) + "\n"


def _module_loaded(module: str) -> bool:
    """``True`` iff ``module`` is currently present in ``lsmod`` output."""
    try:
        proc = subprocess.run(["lsmod"], capture_output=True, text=True, check=True)
    except (OSError, subprocess.SubprocessError):
        return False
    for line in proc.stdout.splitlines()[1:]:
        name = line.split(maxsplit=1)[0] if line.split() else ""
        if name == module:
            return True
    return False


# ---------------------------------------------------------------------------
# Delegation detection (content-aware, unprivileged)
# ---------------------------------------------------------------------------


def _user_manager_cgroup_controllers(uid: int) -> set[str]:
    """The cgroup controllers delegated to the operator's user manager.

    Reads ``cgroup.controllers`` of the operator's ``user@<uid>.service`` cgroup
    — the set of controllers systemd delegated to the per-user manager. A pure
    read; missing file (no live session) yields an empty set.
    """
    path = Path(
        f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/cgroup.controllers"
    )
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return set()
    return set(raw.split())


_REQUIRED_DELEGATED_CONTROLLERS = frozenset({"cpu", "memory", "pids"})


def _delegation_present(uid: int) -> bool:
    """``True`` iff the operator's user manager already has cpu/memory/pids."""
    return _REQUIRED_DELEGATED_CONTROLLERS.issubset(_user_manager_cgroup_controllers(uid))


# ---------------------------------------------------------------------------
# Appliers — each a thin wrapper over an existing helper
# ---------------------------------------------------------------------------


def _run(argv: list[str]) -> None:
    """Run ``argv`` to completion, raising on non-zero exit."""
    subprocess.run(argv, capture_output=True, text=True, check=True)


def _apply_subid(params: BatchParams) -> None:
    """Append the OPERATOR's /etc/subuid + /etc/subgid ranges (append-only-safe).

    Reuses l2's ``usermod --add-subuids`` / ``--add-subgids`` append mechanism
    (the host-prereq contract): an absent entry is appended, an existing entry
    is left untouched (idempotent skip).
    """
    operator = params.operator
    range_arg = _subid_range_arg()
    if not parse_subuid_for_user(operator):
        _run(["usermod", "--add-subuids", range_arg, operator])
    if not parse_subgid_for_user(operator):
        _run(["usermod", "--add-subgids", range_arg, operator])


def _apply_groupadd(params: BatchParams) -> None:
    """``groupadd -g <bridge_gid> <bridge_group>`` + ``usermod -aG`` (l2's logic).

    Idempotent: the group is created only when absent; the operator is added to
    it only when not already a member.
    """
    try:
        grp.getgrnam(params.bridge_group)
    except KeyError:
        _run(["groupadd", "-g", str(params.bridge_gid), params.bridge_group])
    if not _operator_in_group(params.operator, params.bridge_group):
        _run(["usermod", "-aG", params.bridge_group, params.operator])


def _operator_in_group(operator: str, group: str) -> bool:
    """``True`` iff ``operator`` is a member of ``group`` (supplementary or primary)."""
    try:
        entry = grp.getgrnam(group)
    except KeyError:
        return False
    if operator in set(entry.gr_mem):
        return True
    try:
        return entry.gr_gid == pwd.getpwnam(operator).pw_gid
    except KeyError:
        return False


def _apply_sysctl(params: BatchParams) -> None:
    """Write the sysctl drop-in + apply via ``sysctl -w`` (l1's logic, reused).

    Reuses l1's :func:`~core.setup.l1_kernel.render_sysctl_dropin` (which
    already self-branches on the debian-family for ``unprivileged_userns_clone``,
    confirmed correct by validation) and ``write_root_file`` + the ``sysctl -w``
    calls.
    """
    del params  # l1's renderer self-branches via detect_distro(); no param needed
    l1_kernel.write_root_file(l1_kernel.SYSCTL_DROPIN, l1_kernel.render_sysctl_dropin(), 0o644)
    _run(["sysctl", "-w", f"user.max_user_namespaces={l1_kernel.MAX_USER_NS}"])
    if l1_kernel.is_debian_family():
        _run(["sysctl", "-w", "kernel.unprivileged_userns_clone=1"])


def _apply_nftables(params: BatchParams) -> None:
    """``modprobe`` the per-distro module set + persist (content-aware).

    Loads each module not already present and writes the persist drop-in only
    when its content differs from the rendered expectation (idempotent).
    """
    modules = _nftables_modules(params.distro_family)
    for module in modules:
        if not _module_loaded(module):
            _run(["modprobe", module])
    expected = _render_modules_load_dropin(params.distro_family)
    if _read(_MODULES_LOAD_DROPIN) != expected:
        l1_kernel.write_root_file(_MODULES_LOAD_DROPIN, expected, 0o644)


def _apply_delegate(params: BatchParams) -> None:
    """Write the ``Delegate=yes`` drop-in for the operator's manager (l2a's logic).

    Reuses l2a's :func:`~core.setup.l2a_delegate.render_delegate_dropin` and its
    write logic, targeting ``user-<operator_uid>.service.d/`` + a
    ``systemctl daemon-reload``.
    """
    dropin = l2a_delegate.delegate_dropin_path_for_uid(params.operator_uid)
    l2a_delegate.write_root_file(dropin, l2a_delegate.render_delegate_dropin(), 0o644)
    _run(["systemctl", "daemon-reload"])


def _apply_linger(params: BatchParams) -> None:
    """``loginctl enable-linger <operator>`` (root-side).

    Linger persists the operator's per-user systemd manager so the rootless
    dockerd L5 installs survives logout. Self-service (the operator enabling
    their own linger) is polkit-gated and fails on most distros (validated:
    Ubuntu allows it; Debian/Fedora/Arch need root), so it is applied here as
    root. Idempotent: ``enable-linger`` on an already-lingering user is a no-op.
    """
    _run(["loginctl", "enable-linger", params.operator])


def _apply_runsc(params: BatchParams) -> None:
    """Install / update the pinned runsc root-owned in the reserved dir.

    The host-root batch IS the operator-rootless runsc lifecycle: the classifier
    selects ``RUNSC`` whenever the on-disk binary is absent OR drifts from the
    pinned sha (``_runsc_satisfied`` requires an exact match), so this applier must
    handle BOTH install and update. It therefore uses ``force=True``:

    - **fresh install** (target absent) — ``force`` is inert (``install_pinned``
      only ``chattr -i``s an *existing* target), so this is identical to the
      validated fresh-host path;
    - **drift update** (target present + ``chattr +i`` sealed) — ``force`` unseals
      the immutable target before the atomic replace. With ``force=False`` the
      ``os.rename`` over the sealed binary would hard-fail (EPERM), aborting the
      batch — so a wheel upgrade that bumped the runsc pin would break op-rootless
      setup. ``force=True`` lets a normal ``sandbox setup`` re-run converge runsc
      to the new pin (surfaced in the plan + the one ``sudo`` escalation; not
      silent). This is the op-rootless analogue of separate-user ``--update-runsc``
      — convergence is the batch's model, so no separate force flag is needed.

    ``host_config`` is accepted by ``install_pinned`` only for API uniformity
    (the reserved path is host-independent).
    """
    del params  # the reserved runsc path is root-owned and host-independent
    install_pinned("runsc", _RESERVED_HOST_CONFIG, force=True)


def _apply_marker(params: BatchParams) -> None:
    """Write the root-owned mode marker (LAST in the batch — crash-safe).

    Delegates to ``core.setup_state.write_mode_root_owned`` (the single source for
    the root-owned marker write shared with separate-user setup): atomic content
    write + mode ``0644`` + ``chown root:root``.
    """
    write_mode_root_owned(params.operator, params.mode)


# ``install_pinned`` reads no host-specific field (the reserved path is
# root-owned + host-independent), but its signature requires a HostConfig; build
# a minimal one once at module load.
_RESERVED_HOST_CONFIG = HostConfig(
    host=HostSettings(
        docker_unprivileged_user="root",
        docker_execution_mode=DockerExecutionMode.OPERATOR_ROOTLESS,
    )
)


_APPLIERS: dict[BatchItem, Callable[[BatchParams], None]] = {
    BatchItem.SUBID: _apply_subid,
    BatchItem.GROUPADD: _apply_groupadd,
    BatchItem.SYSCTL: _apply_sysctl,
    BatchItem.NFTABLES: _apply_nftables,
    BatchItem.DELEGATE: _apply_delegate,
    BatchItem.LINGER: _apply_linger,
    BatchItem.RUNSC: _apply_runsc,
    BatchItem.MARKER: _apply_marker,
}


def apply_host_root_batch(items: frozenset[BatchItem], params: BatchParams) -> None:
    """Apply the requested batch items in canonical order (root-side, §8-B).

    Walks :data:`HOST_ROOT_BATCH` (NOT argv order) and applies only the items in
    ``items``. Any applier raising aborts the loop, so a mid-batch failure can
    never reach a later item — in particular never the ``MARKER`` write, which is
    last. This marker-last crash-safety is structural; do NOT wrap the appliers
    in a ``try``/``except`` that would defeat it.
    """
    for item in HOST_ROOT_BATCH:
        if item not in items:
            continue
        _APPLIERS[item](params)


# ---------------------------------------------------------------------------
# Classifier — unprivileged, content-aware
# ---------------------------------------------------------------------------


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


def _subid_satisfied(operator: str) -> bool:
    """``True`` iff the operator already has both subuid + subgid entries."""
    return bool(parse_subuid_for_user(operator)) and bool(parse_subgid_for_user(operator))


def _sysctl_satisfied() -> bool:
    """``True`` iff the sysctl drop-in already matches l1's rendered source."""
    return _read(l1_kernel.SYSCTL_DROPIN) == l1_kernel.render_sysctl_dropin()


def _nftables_satisfied(distro_family: str) -> bool:
    """``True`` iff every required module is loaded AND the persist file matches."""
    modules = _nftables_modules(distro_family)
    if any(not _module_loaded(module) for module in modules):
        return False
    return _read(_MODULES_LOAD_DROPIN) == _render_modules_load_dropin(distro_family)


def _runsc_satisfied() -> bool:
    """``True`` iff the on-disk runsc matches the pinned sha512."""
    return detect_drift("runsc", _RESERVED_HOST_CONFIG).status == "match"


def _linger_satisfied(operator: str) -> bool:
    """``True`` iff ``loginctl`` reports ``Linger=yes`` for ``operator``.

    Tolerates any ``loginctl show-user`` failure as linger-absent (a
    never-lingering user is reported "User ID N is not logged in or lingering",
    exit 1) — mirrors l5's ``_linger_enabled`` fail-safe. Pure read; no mutation.
    """
    try:
        proc = subprocess.run(
            ["loginctl", "show-user", operator, "--property=Linger"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "Linger=yes" in proc.stdout


def classify_host_root_batch(ctx: SetupContext) -> tuple[frozenset[BatchItem], BatchParams]:
    """Content-aware, unprivileged classifier (no mutation, no sudo).

    Inspects the host and returns the **unsatisfied** batch items plus the
    computed :class:`BatchParams`. Reuses each phase's read-only probe logic
    (l1's sysctl compare, l2's subuid/group checks, l6a's runsc sha check,
    ``setup_state.read_mode`` for the marker, the cgroup delegation read).
    """
    operator = ctx.operator
    operator_uid = pwd.getpwnam(operator).pw_uid
    bridge_group = ctx.host_config.host.workspace_bridge_group
    bridge_gid = autodetect_workspace_bridge_gid_recommendation(operator)
    family = detect_distro() or ""
    mode = ctx.host_config.host.docker_execution_mode

    params = BatchParams(
        operator=operator,
        operator_uid=operator_uid,
        bridge_group=bridge_group,
        bridge_gid=bridge_gid,
        distro_family=family,
        mode=mode,
    )

    unsatisfied: set[BatchItem] = set()
    if not _subid_satisfied(operator):
        unsatisfied.add(BatchItem.SUBID)
    if not _group_satisfied(operator, bridge_group):
        unsatisfied.add(BatchItem.GROUPADD)
    if not _sysctl_satisfied():
        unsatisfied.add(BatchItem.SYSCTL)
    if not _nftables_satisfied(family):
        unsatisfied.add(BatchItem.NFTABLES)
    if not _delegation_present(operator_uid):
        unsatisfied.add(BatchItem.DELEGATE)
    if not _linger_satisfied(operator):
        unsatisfied.add(BatchItem.LINGER)
    if not _runsc_satisfied():
        unsatisfied.add(BatchItem.RUNSC)
    if read_mode(operator) != mode:
        unsatisfied.add(BatchItem.MARKER)

    return frozenset(unsatisfied), params


def _group_satisfied(operator: str, bridge_group: str) -> bool:
    """``True`` iff the bridge group exists AND the operator is a member."""
    try:
        grp.getgrnam(bridge_group)
    except KeyError:
        return False
    return _operator_in_group(operator, bridge_group)


# ---------------------------------------------------------------------------
# Operator-facing renderings (escalation argv + sudo fallback)
# ---------------------------------------------------------------------------


def build_bootstrap_argv(items: frozenset[BatchItem], params: BatchParams) -> list[str]:
    """Build the ``sandbox _bootstrap-host`` escalation argv (§8-B consumes it).

    The ``--item`` flags are emitted in canonical :data:`HOST_ROOT_BATCH` order
    (NOT set-iteration order) so the wire is deterministic; the trailing params
    pin the flag names §8-B parses.
    """
    argv = ["sandbox", "_bootstrap-host"]
    for item in HOST_ROOT_BATCH:
        if item in items:
            argv.extend(["--item", item.value])
    argv.extend(
        [
            "--operator",
            params.operator,
            "--operator-uid",
            str(params.operator_uid),
            "--bridge-group",
            params.bridge_group,
            "--bridge-gid",
            str(params.bridge_gid),
            "--distro-family",
            params.distro_family,
            "--docker-execution-mode",
            DockerExecutionMode.OPERATOR_ROOTLESS.value,
        ]
    )
    return argv


def render_remediation_block(items: frozenset[BatchItem], params: BatchParams) -> str:
    """Render a copy-pasteable, ordered ``sudo`` block (the no-escalation fallback).

    One ``sudo`` line per requested item, in canonical :data:`HOST_ROOT_BATCH`
    order — the fail-closed remediation surfaced when the operator has no
    escalation path at all (not a sudoer, no polkit). Mirrors the pattern
    ``doctor`` already uses for the ``sb-ws`` group.
    """
    lines = ["# sandbox-ai operator-rootless host-root prerequisites (run in order):"]
    for item in HOST_ROOT_BATCH:
        if item in items:
            lines.append(_REMEDIATION_LINES[item](params))
    return "\n".join(lines) + "\n"


def _remediation_subid(params: BatchParams) -> str:
    range_arg = _subid_range_arg()
    return (
        f"sudo usermod --add-subuids {range_arg} "
        f"--add-subgids {range_arg} {params.operator}"
    )


def _remediation_groupadd(params: BatchParams) -> str:
    return (
        f"sudo groupadd -g {params.bridge_gid} {params.bridge_group} && "
        f"sudo usermod -aG {params.bridge_group} {params.operator}"
    )


def _remediation_sysctl(params: BatchParams) -> str:
    del params
    return f"sudo tee {l1_kernel.SYSCTL_DROPIN} <<'EOF' && sudo sysctl --system"


def _remediation_nftables(params: BatchParams) -> str:
    modules = " ".join(_nftables_modules(params.distro_family))
    return f"sudo modprobe {modules} && echo '{modules}' | sudo tee {_MODULES_LOAD_DROPIN}"


def _remediation_delegate(params: BatchParams) -> str:
    dropin = l2a_delegate.delegate_dropin_path_for_uid(params.operator_uid)
    return f"sudo install -Dm0644 /dev/stdin {dropin} && sudo systemctl daemon-reload"


def _remediation_linger(params: BatchParams) -> str:
    return f"sudo loginctl enable-linger {params.operator}"


def _remediation_runsc(params: BatchParams) -> str:
    del params
    return "sudo sandbox setup --update-runsc  # installs the pinned runsc root-owned"


def _remediation_marker(params: BatchParams) -> str:
    return f"sudo sandbox setup --docker-execution-mode {params.mode.value}  # writes the root-owned mode marker"


_REMEDIATION_LINES: dict[BatchItem, Callable[[BatchParams], str]] = {
    BatchItem.SUBID: _remediation_subid,
    BatchItem.GROUPADD: _remediation_groupadd,
    BatchItem.SYSCTL: _remediation_sysctl,
    BatchItem.NFTABLES: _remediation_nftables,
    BatchItem.DELEGATE: _remediation_delegate,
    BatchItem.LINGER: _remediation_linger,
    BatchItem.RUNSC: _remediation_runsc,
    BatchItem.MARKER: _remediation_marker,
}


__all__ = [
    "HOST_ROOT_BATCH",
    "BatchItem",
    "BatchParams",
    "apply_host_root_batch",
    "build_bootstrap_argv",
    "classify_host_root_batch",
    "render_remediation_block",
]
