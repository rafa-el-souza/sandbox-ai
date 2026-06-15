# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""L8 — fresh-session re-probe: post-ceremony verification (no mutation).

L8 is the last phase of the base ceremony and is **verification, not
mutation** (spec "Phase Execution Order": "L8 is verification, not mutation").
``depends_on`` is ``("l3a",)`` (the rule must be installed and per-op-probed
before the end-to-end re-probe is meaningful); ``identity`` is OPERATOR (it
verifies the *operator's* fresh-session state). ``rollback`` is ``None`` —
verification mutates nothing, so there is nothing to undo.

Two checks, each with the operator-drop primitive matched to its command:

1. ``pipe_cmd(<operator>) → id -G`` — the operator's group set MUST include the
   ``[host].workspace_bridge_group`` gid (the post-``usermod -aG sb-ws`` group
   is visible in a fresh ``--uid`` transient unit, whose ``initgroups`` reflects
   the post-``usermod`` set even though the operator's login session predates
   the ``usermod`` — empirically validated V0/V3, the whole reason this re-probe
   is a *fresh* session). ``id`` is a plain binary, so ``pipe_cmd`` is correct.
2. ``sudo_as_operator(<operator>) → sudo -n <SYSTEMD_RUN_PATH> -q --pipe
   --uid=<user> /bin/bash -c '<dispatch> auth-probe'`` — the relative
   transient-unit launcher (B-3, byte-identical to
   ``core.host_config.pipe_cmd()``'s runtime output) — confirms the
   dispatcher is reachable end-to-end through the just-installed rule, crossing
   via the PIPE. The M3-i rule renders ONLY the pipe ``Cmnd_Spec`` (the
   machinectl operator spec was REMOVED), so this check MUST cross via
   ``pipe_cmd`` or it is unauthorized → "password required" → L8 false-FAILs a
   correct rule (C-009 design D4). The operator-side command is still the setuid
   binary ``sudo``, so root drops to the operator via ``sudo_as_operator`` (a
   normal-process ``sudo -u``), NOT ``pipe_cmd``: a ``pipe_cmd`` ``--uid``
   transient unit EXIT_EXECs (203) on setuid ``sudo`` (F-016). F-016 does not
   block the crossing's inner — the authorized transient-unit launcher's
   ``--uid`` unit execs ``/bin/bash`` → dispatch, a non-setuid binary. The
   crossed payload is
   the bare ``<dispatch> auth-probe`` (so it MATCHES the per-op ``Cmnd_Spec`` — a
   wrapping ``sentinel=True`` would not, F-018), and the inner exit is recovered
   via the dispatcher's begin/exit framing (``framed=True``) so a masked
   dispatcher reject is not read as success (same Finding-J class as L3a).

Like L3a, L8's *work* is verification: :func:`_probe` always returns
``MISSING`` (the re-probe has no cheap idempotent skip) and :func:`_act`
performs the checks, raising on any failure so the phase-runner classifies it
``FAIL`` (no rollback — design D1: only phases carrying a ``rollback`` callable
are rolled back; L8 carries none).
"""

from __future__ import annotations

import grp
from typing import TYPE_CHECKING

from core.dispatch import Op, dispatch_payload
from core.exceptions import SandboxExecutionError
from core.executor import Executor
from core.host_config import (
    DockerExecutionMode,
    pipe_cmd,
    resolve_daemon_owner_settings,
    sudo_as_operator,
)
from core.setup.phase_runner import Identity, Phase, PhaseResult

if TYPE_CHECKING:
    from core.host_config import HostConfig
    from core.setup.phase_runner import SetupContext


class FreshSessionError(SandboxExecutionError):
    """A post-ceremony fresh-session check did not pass.

    Raised by :func:`_act` / :func:`_reverify` so the phase-runner classifies
    L8 ``FAIL`` with an operator-actionable diagnostic.
    """


def _bridge_gid(host_config: HostConfig) -> int:
    """Resolve the ``[host].workspace_bridge_group`` gid (raw ``grp`` lookup).

    L8 only needs the gid to check membership in ``id -G`` output; the subgid-
    range validation lives in the phases that *use* the gid as a
    ``--group-add`` arg, not here.
    """
    group = host_config.host.workspace_bridge_group
    try:
        return grp.getgrnam(group).gr_gid
    except KeyError as exc:
        raise FreshSessionError(
            f"workspace bridge group {group!r} does not exist on this host; "
            f"setup's group phase must create it before L8 can verify "
            f"membership"
        ) from exc


def _check_group_set(host_config: HostConfig, operator: str) -> None:
    """Verify the operator's fresh-session group set includes the bridge gid."""
    bridge_gid = _bridge_gid(host_config)
    argv = [*pipe_cmd(operator), "id", "-G"]
    try:
        result = Executor().run(argv)
    except SandboxExecutionError as exc:
        raise FreshSessionError(
            f"fresh-session `id -G` for operator {operator!r} failed: {exc}"
        ) from exc
    gids = result.stdout.split()
    if str(bridge_gid) not in gids:
        raise FreshSessionError(
            f"operator {operator!r}'s fresh-session group set {gids!r} does "
            f"not include the workspace bridge gid {bridge_gid} "
            f"({host_config.host.workspace_bridge_group!r}); the "
            f"`usermod -aG` group membership is not effective"
        )


def _check_dispatcher_reachable(
    host_config: HostConfig, operator: str
) -> None:
    """Verify the dispatcher is reachable end-to-end through the new rule.

    Crosses via the PIPE (``pipe_cmd``) — the M3-i rule renders ONLY the pipe
    ``Cmnd_Spec`` (the machinectl operator spec was REMOVED), so a machinectl
    crossing would be unauthorized and false-FAIL a correct rule (C-009 design
    D4). The crossing prefix is derived from ``pipe_cmd`` (the relative
    transient-unit launcher, B-3) and the inner payload from ``dispatch_payload``
    — never a hand-typed launcher literal — so it stays byte-identical to the
    rendered ``auth-probe`` ``Cmnd_Spec``. Root drops to the operator via
    ``sudo_as_operator`` (the setuid ``sudo`` cannot exec inside a ``pipe_cmd``
    ``--uid`` transient unit — F-016); F-016 does not block the crossing's inner
    (the authorized transient-unit launcher's ``--uid`` unit execs ``/bin/bash``
    → dispatch, a non-setuid binary).

    The inner ``/bin/bash -c '<dispatch> auth-probe'`` exit is recovered via the
    dispatcher's begin/exit framing (``Executor().run(..., framed=True)``) — a
    masked dispatcher reject must not be read as a healthy grant (Finding-J
    class). ``framed=True`` (NOT the pre-F-018 ``sentinel=True``) is load-
    bearing here: the crossed payload must stay the bare ``<dispatch>
    auth-probe`` so it MATCHES the per-op ``Cmnd_Spec`` — a wrapping
    ``sentinel=True`` made the authorized command unmatchable and silently
    failed this check for every SUDO-mode password-operator (F-018).
    """
    sandbox_user = resolve_daemon_owner_settings(host_config.host)
    inner = dispatch_payload(Op.AUTH_PROBE.value, [])
    argv = [
        *sudo_as_operator(operator),
        "sudo",
        "-n",
        *pipe_cmd(sandbox_user),
        "/bin/bash",
        "-c",
        inner,
    ]
    try:
        Executor().run(argv, framed=True)
    except SandboxExecutionError as exc:
        raise FreshSessionError(
            f"the dispatcher is NOT reachable end-to-end through the new rule "
            f"for operator {operator!r} (recovered inner exit / trace: "
            f"{exc})"
        ) from exc


def _verify(ctx: SetupContext) -> str:
    """Run both fresh-session checks; raise on any failure."""
    operator = ctx.operator
    host_config = ctx.host_config
    _check_group_set(host_config, operator)
    _check_dispatcher_reachable(host_config, operator)
    return (
        f"fresh-session verified for operator {operator!r}: bridge gid in "
        f"group set; dispatcher reachable through the new rule"
    )


def _probe(_ctx: SetupContext) -> tuple[PhaseResult, str]:
    """Always ``MISSING``: the re-probe has no cheap idempotent skip.

    (L8's *work* is verification — the fresh-session re-probe must run every
    apply pass. ``MISSING`` makes the phase-runner proceed to ``act``.)
    """
    return (
        PhaseResult.MISSING,
        "fresh-session re-probe must run (no idempotent skip — verification)",
    )


def _act(ctx: SetupContext) -> str:
    """Perform the fresh-session re-probe; raising → phase-runner FAIL."""
    return _verify(ctx)


def _reverify(ctx: SetupContext) -> bool:
    """Re-run the re-probe; a clean second pass confirms (no mutation)."""
    _verify(ctx)
    return True


PHASE = Phase(
    id="l8",
    name="fresh-session re-probe",
    identity=Identity.OPERATOR,
    probe=_probe,
    act=_act,
    reverify=_reverify,
    depends_on=("l3a",),
    rollback=None,
    # operator-rootless has no privilege-boundary crossing and no boundary
    # group to re-probe; the operator's subuid/linger posture is a doctor
    # concern (C-005), not L8's. So L8 SKIPS entirely in operator-rootless.
    applies_in=frozenset({DockerExecutionMode.SEPARATE_USER}),
)
