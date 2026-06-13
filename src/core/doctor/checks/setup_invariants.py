# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
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
- ``core.setup.l0_identity.parse_sudo_version`` / ``SUDO_FLOOR`` — the
  V9c-validated sudo floor;
- ``core.setup.l0_identity.resolve_operator`` — operator resolution precedence;
- ``core.setup.l3_sudoers.render_sudoers_rule`` / ``_cmnd_specs`` /
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

from core.dispatch import DISPATCH_BINARY, Op
from core.doctor.types import CheckResult
from core.host_config import (
    DEFAULT_PROVISIONING_MODE,
    DockerExecutionMode,
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
# privilege-boundary sudoers drop-in is audited separately.
_RESERVED_DIR = Path("/usr/local/libexec/sandbox-ai")

# Extract each op-name segment from a rendered sudoers rule body. The L3
# renderer emits ``… /bin/bash -c <dispatch>\ <op>[\ *]`` per Cmnd_Spec (F-004:
# backslash-escaped whitespace, never shell-quoting). The dispatch path is
# single-sourced from ``core.dispatch.DISPATCH_BINARY`` so a rename there is a
# single-point change. The op segment is ``[a-z0-9-]+`` per the L3 op-name
# shape gate.
_CMND_SPEC_OP_RE = re.compile(re.escape(DISPATCH_BINARY) + r"\\ ([a-z0-9-]+)")


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

    status, detail = l2.subid_status(user)
    if status != "adequate":
        violations.append(f"/etc/subuid|subgid for {user!r}: {detail}")

    if not l2.group_exists(bridge_group):
        violations.append(f"bridge group {bridge_group!r} absent per /etc/group")
        return

    bridge_gid = grp.getgrnam(bridge_group).gr_gid
    if not l2.gid_in_subgid_range(bridge_gid, parse_subgid_for_user(user)):
        violations.append(
            f"bridge group {bridge_group!r} gid {bridge_gid} outside "
            f"{user!r}'s /etc/subgid range"
        )
    if not l2.operator_in_group(operator, bridge_group):
        violations.append(
            f"operator {operator!r} not in {bridge_group!r} group per "
            f"/etc/group. Run 'sudo sandbox setup' to restore (and log "
            f"out/in to refresh group set)."
        )


def _audit_daemon_user_no_admin(host_config: HostConfig, violations: list[str]) -> None:
    """Dedicated daemon user is a member of NO privilege-granting group (D3).

    Separate-user only (the caller runs this sub-audit only on the separate-user
    branch — the dedicated user exists only there). The no-privilege property is
    what makes the separate-user blast-radius reduction load-bearing: a
    container/runtime escape that reaches the daemon owner lands on a dead-end
    account only if that account cannot escalate. WARN (never FAIL) — an operator
    who deliberately privileged the daemon user should be told, not hard-blocked.

    Reuses L2's single-source admin-group resolver ``user_admin_groups`` (lazy
    import — see the module-top NOTE on the import-time cycle) so this audit and
    the operator-rootless sudoer-owner WARN cannot disagree on what counts as an
    admin group.
    """
    from core.setup import l2_host_prereqs as l2

    daemon_user = host_config.host.docker_unprivileged_user
    admin_groups = l2.user_admin_groups(daemon_user)
    # The owner here is a *different* user (the dedicated daemon account), so the
    # policy query is ``sudo -n -l -U <daemon_user>`` — which needs root.
    # setup_invariants may run as the operator (plain ``sandbox doctor``) or as
    # root (``sudo sandbox doctor``); when not root the ``-U`` query is
    # indeterminate. Best-effort: WARN on a determinable grant, otherwise fall
    # back to group-only and note the gap — NEVER false-WARN on indeterminate.
    policy = l2.user_sudoers_grant(daemon_user, self_query=False)

    if not admin_groups and not (policy.determinable and policy.granted):
        if not policy.determinable:
            violations.append(
                f"dedicated daemon user {daemon_user!r} is in no privilege-granting "
                f"group (sudoers-policy not checked — run 'sudo sandbox doctor' for "
                f"the full audit)"
            )
        return

    paths: list[str] = []
    if admin_groups:
        groups = ", ".join(admin_groups)
        paths.append(f"is a member of privilege-granting group(s) {groups}")
    if policy.determinable and policy.granted:
        if policy.nopasswd:
            paths.append(
                "is granted passwordless sudo by the sudoers policy "
                "(e.g. a /etc/sudoers.d/ drop-in)"
            )
        else:
            paths.append("is granted sudo by the sudoers policy")
    remedy_groups = ", ".join(admin_groups) if admin_groups else "<group>"
    violations.append(
        f"dedicated daemon user {daemon_user!r} {' and '.join(paths)}; a privileged "
        f"daemon user defeats the separate-user blast-radius reduction (a runtime "
        f"escape reaching the daemon owner could escalate). Remove the membership: "
        f"'sudo gpasswd -d {daemon_user} {remedy_groups}' (or 'sudo deluser "
        f"{daemon_user} <group>'), and revoke any /etc/sudoers.d/ grant for "
        f"{daemon_user!r}."
    )


def _audit_machinectl_stability(host_config: HostConfig, violations: list[str]) -> None:
    """machinectl path resolves uniquely on the sudoers ``secure_path`` basis.

    Post-C-009-D4 the operator SUDO drop-in is pipe-only — the per-op
    ``Cmnd_Spec`` pins the ``pipe_cmd`` byte-pipe launcher (``SYSTEMD_RUN_PATH``),
    NOT machinectl — so there is no machinectl path in the drop-in to match
    against (that match moved to :func:`_audit_systemd_run_stability`). machinectl
    is still load-bearing for the root L5/L6/L7 setup crossings, so a shadowed /
    absent / non-unique machinectl is still a real stability problem: the
    resolver raising IS the check. WARN per this check's policy.

    Reuses L0's single-source ``resolve_machinectl_path`` (lazy import — see
    the module-top NOTE on the import-time cycle).
    """
    from core.setup import l0_identity as l0

    try:
        l0.resolve_machinectl_path(host_config)
    except l0.MachinectlResolutionError as exc:
        violations.append(f"machinectl-path-stability: {exc}")


def _audit_systemd_run_stability(
    host_config: HostConfig, drop_in_text: str | None, violations: list[str]
) -> None:
    """Re-resolved byte-pipe launcher == the path pinned in the pipe Cmnd_Spec.

    Post-C-009-D4 the operator SUDO drop-in is the ``sudo_pipe_cmd`` crossing, so
    the per-op ``Cmnd_Spec`` pins the absolute ``SYSTEMD_RUN_PATH`` byte-pipe
    launcher sudo resolves on its ``secure_path``. If the re-resolved launcher is
    no longer the one pinned in the installed drop-in (a second copy, a shadow),
    the SUDO op grant breaks. WARN per this check's policy.

    Reuses L0's single-source ``resolve_systemd_run_path`` (lazy import — see
    the module-top NOTE on the import-time cycle).
    """
    from core.setup import l0_identity as l0

    try:
        resolved = l0.resolve_systemd_run_path(host_config)
    except l0.SystemdRunResolutionError as exc:
        violations.append(f"pipe-launcher-path-stability: {exc}")
        return
    if drop_in_text is None:
        return
    if resolved not in drop_in_text:
        violations.append(
            f"pipe-launcher-path-stability: the resolved secure_path byte-pipe "
            f"launcher {resolved!r} is not the launcher pinned in the installed "
            f"pipe Cmnd_Spec. This drift breaks the orchestrator's sudo_pipe_cmd "
            f"SUDO op grant. Remove the unexpected copy or run "
            f"'sudo sandbox setup' to re-evaluate."
        )


def _audit_rule_body(
    host_config: HostConfig, operator: str, drop_in_text: str, violations: list[str]
) -> None:
    """Re-render the expected operator rule body and audit the installed drop-in.

    (a) op set == ``core.dispatch.Op``; (b) zero ``"`` in any Cmnd_Spec body;
    (c) op-name segment shape (the L3 renderer raises ``RuleRenderError`` on a
    non-conforming op — surfaced as a WARN, not an exception).

    The L3 rule body now renders the per-op byte-pipe ``Cmnd_Spec`` keyed on the
    transient-unit launcher path (the ``sudo_pipe_cmd`` crossing — C-009 D4), so
    the expected body is rendered against L0's ``resolve_systemd_run_path``, NOT
    ``resolve_machinectl_path``. (The machinectl-path *stability* audit is a
    separate check — the root L5/L6/L7 crossings still use machinectl — and is
    unchanged.) Reuses L3's ``render_sudoers_rule`` (lazy import — see the
    module-top NOTE on the import-time cycle).
    """
    from core.setup import l0_identity as l0
    from core.setup import l3_sudoers as l3

    sandbox_user = host_config.host.docker_unprivileged_user
    try:
        systemd_run_path = l0.resolve_systemd_run_path(host_config)
    except l0.SystemdRunResolutionError:
        # Without a resolvable launcher path we cannot re-render the pipe rule
        # body, so skip the body comparison (same guard as the machinectl-path
        # resolution failure the stability audit records separately).
        return

    try:
        expected = l3.render_sudoers_rule(
            systemd_run_path, operator, socket.gethostname(), sandbox_user
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

    Reuses L0's ``parse_sudo_version`` + ``SUDO_FLOOR`` (lazy import — see the
    module-top NOTE on the import-time cycle).
    """
    from core.setup import l0_identity as l0

    ver = l0.parse_sudo_version()
    if ver is None or ver >= l0.SUDO_FLOOR:
        return
    rendered = f"{ver[0]}.{ver[1]}.{ver[2]}" + (f"p{ver[3]}" if ver[3] else "")
    violations.append(
        f"sudo {rendered} predates the validated floor 1.9.5p2; the V9 "
        f"sudoers rule shape is unverified on this version (supported "
        f"enterprise distros ship ≥1.9.5p2; only EOL distros are below)."
    )


def check_setup_invariants(
    user: str,
    distro: str | None,
    mode: DockerExecutionMode = DEFAULT_PROVISIONING_MODE,
) -> CheckResult:
    """Read-only steady-state audit of setup's owned-namespace artifacts.

    PASS iff every enumerated invariant holds; WARN (never FAIL — drift may be
    operator-intentional / re-runnable) naming each violated invariant.

    Mode-aware (design D2/D5): in ``operator-rootless`` there is no machinectl
    crossing and no sudoers privilege-boundary rule, so the drop-in read,
    the machinectl-stability audit, the
    rule-body audit, and the sudo-floor audit are ALL skipped — only the
    mode-applicable sub-audits (reserved dir + subid/subgid/bridge-group) run.
    The check itself is NOT mode-skipped (it stays both-mode); it branches
    internally so its applicable invariants still green in operator-rootless.
    """
    from core.setup import l0_identity as l0
    from core.setup import l3_sudoers as l3

    del distro
    host_config = minimal_host_config(user, MachinectlAuth.SUDO, mode)
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

    if mode is DockerExecutionMode.OPERATOR_ROOTLESS:
        # No machinectl crossing / sudoers rule exists in this mode:
        # the drop-in read + rule/stability/floor audits are not applicable.
        if not violations:
            return CheckResult(
                status="pass",
                name="setup invariants",
                detail=(
                    f"operator-rootless setup invariants hold (operator={operator}); "
                    f"machinectl-stability + sudoers-rule audits not applicable "
                    f"(no privilege-boundary crossing in operator-rootless)"
                ),
            )
        return CheckResult(
            status="warn",
            name="setup invariants",
            detail="; ".join(violations),
            remediation="run 'sudo sandbox setup' to restore canonical setup state",
        )

    drop_in_path = l3.drop_in_path(operator)
    drop_in_text: str | None
    # ``drop_in_readable`` is False when the drop-in EXISTS but the current
    # (non-root) process cannot read it — the normal case under a plain
    # ``sandbox doctor``, where the operator runs as themselves and the drop-in
    # is root-only (mode 0440 in a 0750 /etc/sudoers.d). That is NOT "missing"
    # (do not flag a violation) and MUST NOT crash: the rule-body +
    # machinectl-stability audits that need the file content are skipped (the
    # rule is validated at install time by L3a's per-op probe; ``sudo sandbox
    # doctor`` is NOT a fuller path — it resolves root's home, not the
    # operator's, per F-021).
    drop_in_readable = True
    try:
        drop_in_text = drop_in_path.read_text()
    except FileNotFoundError:
        drop_in_text = None
        violations.append(
            f"sudoers drop-in {drop_in_path} missing. Run 'sudo sandbox "
            f"setup' to restore."
        )
    except PermissionError:
        drop_in_text = None
        drop_in_readable = False

    _audit_daemon_user_no_admin(host_config, violations)

    _audit_machinectl_stability(host_config, violations)
    _audit_systemd_run_stability(host_config, drop_in_text, violations)
    if drop_in_text is not None:
        _audit_rule_body(host_config, operator, drop_in_text, violations)
    _audit_sudo_floor(violations)

    if not violations:
        if drop_in_readable:
            detail = (
                f"all setup invariants hold (operator={operator}, "
                f"drop-in={drop_in_path.name})"
            )
        else:
            detail = (
                f"operator-readable setup invariants hold (operator={operator}); "
                f"rule-body + machinectl-stability audits skipped — "
                f"{drop_in_path.name} is root-only (mode 0440), not readable by "
                f"the operator. The rule is validated at install time by setup's "
                f"L3a per-op probe."
            )
        return CheckResult(status="pass", name="setup invariants", detail=detail)

    return CheckResult(
        status="warn",
        name="setup invariants",
        detail="; ".join(violations),
        remediation="run 'sudo sandbox setup' to restore canonical setup state",
    )


__all__ = ["check_setup_invariants"]
