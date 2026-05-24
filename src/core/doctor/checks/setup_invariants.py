"""Setup-invariants doctor check (spec "Setup Invariants Check").

Read-only steady-state audit of setup's owned-namespace artifacts. This is the
post-ceremony counterpart of the setup phases' setup-time probes: it re-derives
the *expected* state from the same single-source logic the phases use and WARNs
(never FAILs — drift may be operator-intentional and is re-runnable) on any
deviation.

Single-source reuse (orchestrator decision 2 / F-011 — these are NOT
re-implemented here; a second copy would diverge and produce spurious WARNs):

- ``core.setup.l0_identity.resolve_machinectl_path`` — machinectl-path
  re-enumeration on the sudoers ``secure_path`` basis (B-3, F-005), identical
  to L0 task 5.1;
- ``core.setup.l0_identity.parse_sudo_version`` / ``_SUDO_FLOOR`` — the
  V9c-validated sudo floor;
- ``core.setup.l0_identity.resolve_operator`` — operator resolution precedence;
- ``core.setup.l3_sudoers_polkit.render_sudoers_rule`` / ``_cmnd_specs`` /
  ``_OP_NAME_RE`` / ``_sudoers_path`` — the canonical rule renderer (the
  rule-body content audit re-renders via this exact code path);
- ``core.setup.l2_host_prereqs`` subuid/subgid + group-membership helpers.

The rule-body content audit asserts: (a) the installed drop-in's enumerated op
set exactly equals ``core.dispatch.Op``; (b) ZERO ``"`` chars in any
``Cmnd_Spec`` body (F-004 silent-footgun shape); (c) every op-name segment
matches ``[a-z0-9-]+`` (re-rendering via the L3 renderer raises
``RuleRenderError`` on a non-conforming op — caught and surfaced as WARN).
"""

from __future__ import annotations

import grp
import os
import pwd
import re
import socket
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from core.dispatch import _DISPATCH_BINARY, Op
from core.doctor.types import CheckResult
from core.host_config import (
    MachinectlAuth,
    minimal_host_config,
    parse_subgid_for_user,
)

# NOTE — the ``core.setup.*`` modules are imported LAZILY inside the functions
# that use them, NOT at module top-level. ``core.doctor.registry`` imports this
# check module; ``core.setup.l0_identity`` imports ``core.doctor``
# (``detect_distro`` / ``get_install_cmd``, per setup task 5.1). A top-level
# ``from core.setup.l0_identity import …`` here closes a
# registry → check → l0_identity → core.doctor import-time cycle. Deferring the
# import to call time (when every module is fully initialized) breaks the cycle
# WITHOUT relaxing the single-source pin: the SAME ``resolve_machinectl_path`` /
# ``render_sudoers_rule`` / L2 helpers are reused, just imported when called.

if TYPE_CHECKING:
    from core.host_config import HostConfig

# Owned root:root drop-ins always present after a base ceremony, with their
# canonical octal modes (spec "Reserved Namespace File Ownership"). The
# auth-mode-specific privilege-boundary rule is audited separately (its path +
# mode depend on SUDO vs POLKIT).
_RESERVED_DIR = Path("/usr/local/libexec/sandbox-ai")

# Extract each op-name segment from a rendered sudoers rule body. The L3
# renderer emits ``… /bin/bash -c <dispatch>\ <op>[\ *]`` per Cmnd_Spec (F-004:
# backslash-escaped whitespace, never shell-quoting). The dispatch path is
# single-sourced from ``core.dispatch._DISPATCH_BINARY`` so a rename there is a
# single-point change. The op segment is ``[a-z0-9-]+`` per the L3 op-name
# shape gate.
_CMND_SPEC_OP_RE = re.compile(re.escape(_DISPATCH_BINARY) + r"\\ ([a-z0-9-]+)")


def _audit_reserved_dir(violations: list[str]) -> None:
    """``/usr/local/libexec/sandbox-ai/`` present, mode 0755, root:root."""
    if not _RESERVED_DIR.is_dir():
        violations.append(f"{_RESERVED_DIR} directory missing")
        return
    st = _RESERVED_DIR.stat()
    if stat.S_IMODE(st.st_mode) != 0o755:
        violations.append(
            f"{_RESERVED_DIR} mode {oct(stat.S_IMODE(st.st_mode))} != 0o755"
        )
    if st.st_uid != 0 or st.st_gid != 0:
        violations.append(
            f"{_RESERVED_DIR} owned {st.st_uid}:{st.st_gid} != 0:0 (root:root)"
        )


def _audit_subid_and_group(
    user: str, operator: str, bridge_group: str, violations: list[str]
) -> None:
    """subuid/subgid adequate; sb-ws gid in subgid range; operator in sb-ws.

    Reuses L2's single-source subuid/group helpers (lazy import — see the
    module-top NOTE on the import-time cycle).
    """
    from core.setup import l2_host_prereqs as l2

    status, detail = l2._subid_status(user)
    if status != "adequate":
        violations.append(f"/etc/subuid|subgid for {user!r}: {detail}")

    if not l2._group_exists(bridge_group):
        violations.append(f"bridge group {bridge_group!r} absent per /etc/group")
        return

    bridge_gid = grp.getgrnam(bridge_group).gr_gid
    if not l2._gid_in_subgid_range(bridge_gid, parse_subgid_for_user(user)):
        violations.append(
            f"bridge group {bridge_group!r} gid {bridge_gid} outside "
            f"{user!r}'s /etc/subgid range"
        )
    if not l2._operator_in_group(operator, bridge_group):
        violations.append(
            f"operator {operator!r} not in {bridge_group!r} group per "
            f"/etc/group. Run 'sudo sandbox setup' to restore (and log "
            f"out/in to refresh group set)."
        )


def _audit_machinectl_stability(
    host_config: HostConfig, drop_in_text: str | None, violations: list[str]
) -> None:
    """Re-resolved machinectl path == the path pinned in the drop-in Cmnd_Spec.

    Version-accurate detail per V9e-2: on sudo ≥1.9.15 a second machinectl
    breaks the grant (availability); on 1.9.5p2 the pinned binary still runs
    (hygiene only). WARN either way per this check's policy.

    Reuses L0's single-source ``resolve_machinectl_path`` (lazy import — see
    the module-top NOTE on the import-time cycle).
    """
    from core.setup import l0_identity as l0

    try:
        resolved = l0.resolve_machinectl_path(host_config)
    except l0.MachinectlResolutionError as exc:
        violations.append(f"machinectl-path-stability: {exc}")
        return
    if drop_in_text is None:
        return
    if resolved not in drop_in_text:
        violations.append(
            f"machinectl-path-stability: resolved secure_path machinectl "
            f"{resolved!r} is not the path pinned in the installed sudoers "
            f"drop-in. On sudo ≥1.9.15 this breaks the orchestrator's "
            f"'sudo machinectl …' grant (availability); on 1.9.5p2 the pinned "
            f"binary still runs (hygiene). Remove the unexpected copy or run "
            f"'sudo sandbox setup' to re-evaluate."
        )


def _audit_rule_body(
    host_config: HostConfig, operator: str, drop_in_text: str, violations: list[str]
) -> None:
    """Re-render the expected operator rule body and audit the installed drop-in.

    (a) op set == ``core.dispatch.Op``; (b) zero ``"`` in any Cmnd_Spec body;
    (c) op-name segment shape (the L3 renderer raises ``RuleRenderError`` on a
    non-conforming op — surfaced as a WARN, not an exception).

    Reuses L0's ``resolve_machinectl_path`` + L3's ``render_sudoers_rule``
    (lazy import — see the module-top NOTE on the import-time cycle).
    """
    from core.setup import l0_identity as l0
    from core.setup import l3_sudoers_polkit as l3

    sandbox_user = host_config.host.docker_unprivileged_user
    try:
        machinectl_path = l0.resolve_machinectl_path(host_config)
    except l0.MachinectlResolutionError:
        # Stability audit already recorded the resolution failure; without a
        # resolvable path we cannot re-render, so skip the body comparison.
        return

    try:
        expected = l3.render_sudoers_rule(
            machinectl_path, operator, socket.gethostname(), sandbox_user
        )
    except l3.RuleRenderError as exc:
        violations.append(
            f"sudoers drop-in content drifted from canonical (F-004 / "
            f"op-enum drift): {exc}. Run 'sudo sandbox setup' to regenerate."
        )
        return

    if '"' in drop_in_text:
        violations.append(
            "sudoers drop-in content drifted from canonical (F-004 "
            "silent-footgun shape: double-quote in a Cmnd_Spec — the rule "
            "passes 'visudo -cf' but matches nothing at runtime). Run "
            "'sudo sandbox setup' to regenerate."
        )

    installed_ops = _extract_ops(drop_in_text)
    expected_ops = {op.value for op in Op}
    if installed_ops != expected_ops:
        missing = sorted(expected_ops - installed_ops)
        extra = sorted(installed_ops - expected_ops)
        violations.append(
            f"sudoers drop-in content drifted from canonical (F-004 / "
            f"op-enum drift): installed op set != core.dispatch.Op "
            f"(missing={missing}, extra={extra}). Run 'sudo sandbox setup' "
            f"to regenerate the rule for the current dispatcher op set."
        )
    elif drop_in_text != expected:
        violations.append(
            "sudoers drop-in content drifted from canonical (F-004 / "
            "op-enum drift): installed body differs from the re-rendered "
            "canonical body. Run 'sudo sandbox setup' to regenerate."
        )


def _extract_ops(drop_in_text: str) -> set[str]:
    """Extract the op-name segments from a rendered sudoers rule body.

    Each Cmnd_Spec contains ``<dispatch>\\ <op>[\\ *]``; the op name is the
    backslash-escaped segment immediately after the dispatch path.
    """
    return {m.group(1) for m in _CMND_SPEC_OP_RE.finditer(drop_in_text)}


def _audit_sudo_floor(violations: list[str]) -> None:
    """``sudo --version`` >= the V9c-validated floor 1.9.5p2.

    Reuses L0's ``parse_sudo_version`` + ``_SUDO_FLOOR`` (lazy import — see the
    module-top NOTE on the import-time cycle).
    """
    from core.setup import l0_identity as l0

    ver = l0.parse_sudo_version()
    if ver is None or ver >= l0._SUDO_FLOOR:
        return
    rendered = f"{ver[0]}.{ver[1]}.{ver[2]}" + (f"p{ver[3]}" if ver[3] else "")
    violations.append(
        f"sudo {rendered} predates the validated floor 1.9.5p2; the V9 "
        f"sudoers rule shape is unverified on this version (supported "
        f"enterprise distros ship ≥1.9.5p2; only EOL distros are below)."
    )


def _audit_rule_shape_agreement(is_sudo: bool, operator: str, violations: list[str]) -> None:
    """WARN when the drop-in for the OTHER auth mode is also present (F-022).

    The operator toml's ``machinectl_authentication`` determines which
    privilege-boundary rule SHOULD be installed. If the opposite-mode rule is
    *also* on disk, the toml and the installed rule shape disagree (Defect B):
    the operator likely flipped ``machinectl_authentication`` after a setup run,
    or ran setup under one mode while configuring the toml for the other — so
    the operator's runtime commands cross the boundary one way while the rule
    grants the other. WARN (never FAIL — it is operator-resolvable).

    Reuses L3's path helpers (lazy import — see the module-top NOTE on the
    import-time cycle).
    """
    from core.setup import l3_sudoers_polkit as l3

    if is_sudo:
        other = l3._POLKIT_RULE_PATH
        if other.exists():
            violations.append(
                f"toml selects SUDO auth but a POLKIT rule is also installed at "
                f"{other}; the installed rule shape disagrees with "
                f"machinectl_authentication. Remove the stale rule, or reconcile "
                f"the toml's auth mode with the rule you intend."
            )
    else:
        other = l3._sudoers_path(operator)
        if other.exists():
            violations.append(
                f"toml selects POLKIT auth but a SUDO sudoers drop-in is also "
                f"installed at {other}; the installed rule shape disagrees with "
                f"machinectl_authentication. Remove the stale drop-in, or "
                f"reconcile the toml's auth mode with the rule you intend."
            )


def check_setup_invariants(
    user: str, distro: str | None, auth_mode: MachinectlAuth = MachinectlAuth.SUDO
) -> CheckResult:
    """Read-only steady-state audit of setup's owned-namespace artifacts.

    PASS iff every enumerated invariant holds; WARN (never FAIL — drift may be
    operator-intentional / re-runnable) naming each violated invariant.
    """
    from core.setup import l0_identity as l0
    from core.setup import l3_sudoers_polkit as l3

    del distro
    host_config = minimal_host_config(user, auth_mode)
    bridge_group = host_config.host.workspace_bridge_group
    violations: list[str] = []

    try:
        operator = l0.resolve_operator()
    except l0.OperatorResolutionError:
        # Under a plain `sandbox doctor` (run by the operator AS THEMSELVES, not
        # via sudo), resolve_operator()'s setup precedence ($SUDO_USER →
        # $PKEXEC_UID → --operator → refuse) has no context and raises — which
        # used to short-circuit the whole audit, so it never ran in doctor's
        # normal invocation. The current real user IS the operator here, so fall
        # back to it. resolve_operator() itself stays STRICT for setup (which
        # MUST refuse without explicit context — no heuristics; load-bearing).
        operator = pwd.getpwuid(os.getuid()).pw_name

    _audit_reserved_dir(violations)
    _audit_subid_and_group(user, operator, bridge_group, violations)

    drop_in_path = l3._drop_in_path(host_config, operator)
    is_sudo = auth_mode == MachinectlAuth.SUDO
    drop_in_text: str | None
    try:
        drop_in_text = drop_in_path.read_text()
    except FileNotFoundError:
        drop_in_text = None
        kind = "sudoers" if is_sudo else "polkit"
        violations.append(
            f"{kind} drop-in {drop_in_path} missing. Run 'sudo sandbox "
            f"setup' to restore."
        )

    _audit_rule_shape_agreement(is_sudo, operator, violations)

    if is_sudo:
        _audit_machinectl_stability(host_config, drop_in_text, violations)
        if drop_in_text is not None:
            _audit_rule_body(host_config, operator, drop_in_text, violations)
        _audit_sudo_floor(violations)

    if not violations:
        return CheckResult(
            status="pass",
            name="setup invariants",
            detail=(
                f"all setup invariants hold (operator={operator}, "
                f"drop-in={drop_in_path.name})"
            ),
        )

    return CheckResult(
        status="warn",
        name="setup invariants",
        detail="; ".join(violations),
        remediation="run 'sudo sandbox setup' to restore canonical setup state",
    )


__all__ = ["check_setup_invariants"]
