# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Sudoer daemon-owner doctor check (spec "Sudoer Daemon-Owner Warning").

Operator-rootless only (registry ``applies_in={OPERATOR_ROOTLESS}``). In
operator-rootless mode the rootless Docker daemon is owned by the **invoking
operator** — not a dedicated dead-end account — so if that operator can ``sudo``
to root, a (rare, gVisor-fronted) container/runtime escape reaching the daemon
owner could escalate. This is an *informed-tradeoff* signal, not a
misconfiguration: WARN, never FAIL (design D4).

The owner is resolved via ``core.host_config.resolve_daemon_owner`` — in
operator-rootless that is the invoking operator (``getpass.getuser``), NEVER the
stale ``docker_unprivileged_user`` default (the D7 owner-read convention guard).
Admin-group membership is resolved through L2's single-source
``user_admin_groups`` so this check and the separate-user no-sudo daemon-user
invariant cannot disagree on what counts as an admin group.
"""

from __future__ import annotations

from core.doctor.types import CheckResult
from core.host_config import (
    DEFAULT_PROVISIONING_MODE,
    DockerExecutionMode,
    MachinectlAuth,
    minimal_host_config,
    resolve_daemon_owner,
)


def check_daemon_owner_sudo(
    user: str,
    distro: str | None,
    mode: DockerExecutionMode = DEFAULT_PROVISIONING_MODE,
) -> CheckResult:
    """WARN when the operator account owning rootless Docker is a sudoer.

    PASS when the owner is in no ``sudo``/``wheel``/``admin`` group; WARN (never
    FAIL) naming the escalation trade-off and BOTH remedies when it is. Gated to
    operator-rootless by the registry, so the resolved owner is always the
    invoking operator.

    The ``core.setup.l2_host_prereqs`` import is LAZY (function-local), mirroring
    ``setup_invariants``: ``core.doctor.registry`` imports this check module, and
    a top-level ``from core.setup …`` closes the
    registry → check → l0_identity → core.doctor import-time cycle.
    """
    from core.setup import l2_host_prereqs as l2

    del distro
    host_config = minimal_host_config(user, MachinectlAuth.SUDO, mode)
    owner = resolve_daemon_owner(host_config)
    admin_groups = l2.user_admin_groups(owner)
    # The owner IS the invoking operator (the current process user) in
    # operator-rootless, so query their OWN sudo privileges — self_query=True
    # uses ``sudo -n -l`` (no root, no ``-U``). This catches the policy-grant
    # path (``/etc/sudoers.d/`` drop-ins + ``NOPASSWD``) that group membership
    # alone misses (the cloud-VM / dev-box false-PASS that motivated C-005).
    policy = l2.user_sudoers_grant(owner, self_query=True)

    if not admin_groups and not policy.granted:
        return CheckResult(
            status="pass",
            name="daemon owner sudo",
            detail=(
                f"operator-rootless daemon owner {owner!r} is in no "
                f"sudo/wheel/admin group and the sudoers policy grants no sudo; "
                f"an escape reaching the daemon owner cannot escalate to root"
            ),
        )

    # Name BOTH escalation paths the owner actually has, so the operator knows
    # which to close. The stable substring "is a sudoer" is preserved.
    paths: list[str] = []
    if admin_groups:
        paths.append(f"member of sudo/wheel/admin: {', '.join(admin_groups)}")
    if policy.granted:
        if policy.nopasswd:
            paths.append(
                "passwordless sudo via the sudoers policy "
                "(e.g. a /etc/sudoers.d/ drop-in)"
            )
        else:
            paths.append("sudo via the sudoers policy")
    via = "; ".join(paths)
    remedy_groups = ", ".join(admin_groups) if admin_groups else "sudo/wheel/admin"
    return CheckResult(
        status="warn",
        name="daemon owner sudo",
        detail=(
            f"operator-rootless daemon owner {owner!r} is a sudoer ({via}); "
            f"because the daemon owner can sudo, a (rare, gVisor-fronted) escape "
            f"reaching the daemon owner could escalate to root — re-enlarging the "
            f"blast radius the separate-user dead-end account shrinks"
        ),
        remediation=(
            "this is an informed-tradeoff signal (WARN, never FAIL). To shrink the "
            "blast radius, either (a) run sandboxes as a dedicated non-sudo operator "
            f"account (one in no {remedy_groups} group and with no sudoers-policy "
            "grant), or (b) set "
            "docker_execution_mode = separate-user to run the daemon as a dedicated "
            "dead-end user behind the machinectl crossing"
        ),
    )


__all__ = ["check_daemon_owner_sudo"]
