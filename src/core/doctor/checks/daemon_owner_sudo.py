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
``_user_admin_groups`` so this check and the separate-user no-sudo daemon-user
invariant cannot disagree on what counts as an admin group.
"""

from __future__ import annotations

from core.doctor.types import CheckResult
from core.host_config import (
    DockerExecutionMode,
    MachinectlAuth,
    minimal_host_config,
    resolve_daemon_owner,
)


def check_daemon_owner_sudo(
    user: str,
    distro: str | None,
    auth_mode: MachinectlAuth = MachinectlAuth.SUDO,
    mode: DockerExecutionMode = DockerExecutionMode.OPERATOR_ROOTLESS,
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
    host_config = minimal_host_config(user, auth_mode, mode)
    owner = resolve_daemon_owner(host_config)
    admin_groups = l2._user_admin_groups(owner)

    if not admin_groups:
        return CheckResult(
            status="pass",
            name="daemon owner sudo",
            detail=(
                f"operator-rootless daemon owner {owner!r} is in no "
                f"sudo/wheel/admin group; an escape reaching the daemon owner "
                f"cannot escalate via group-based sudo"
            ),
        )

    groups = ", ".join(admin_groups)
    return CheckResult(
        status="warn",
        name="daemon owner sudo",
        detail=(
            f"operator-rootless daemon owner {owner!r} is a sudoer (member of "
            f"{groups}); because the daemon owner can sudo, a (rare, gVisor-fronted) "
            f"escape reaching the daemon owner could escalate to root — re-enlarging "
            f"the blast radius the separate-user dead-end account shrinks"
        ),
        remediation=(
            "this is an informed-tradeoff signal (WARN, never FAIL). To shrink the "
            "blast radius, either (a) run sandboxes as a dedicated non-sudo operator "
            f"account (one in no {groups} group), or (b) set "
            "docker_execution_mode = separate-user to run the daemon as a dedicated "
            "dead-end user behind the machinectl crossing"
        ),
    )


__all__ = ["check_daemon_owner_sudo"]
