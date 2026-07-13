# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""L5 — linger + rootless dockerd install (content-aware).

**Mode split (D2/D4 — the daemon owner + crossing differ by execution mode).**
The daemon owner is :func:`daemon_owner_user` (the dedicated ``sandbox`` user in
separate-user; the invoking operator in operator-rootless) and the crossing is
:func:`daemon_owner_crossing` (``machinectl_cmd`` in separate-user; an empty
LOCAL prefix in operator-rootless, where setup already runs as the operator). In
**operator-rootless** L5 owns ONLY the rootless dockerd install, run as a plain
LOCAL subprocess in the operator's own session — **linger is host-root-batch-owned**
(the ``host_batch`` ``LINGER`` item, since self-linger is polkit-gated on most
distros) so L5 does not enable it; and there is no machinectl crossing, so the
exit-recovering sentinel is off (a local command's exit is not masked).

In **separate-user** L5 has two mutations, two identities (byte-unchanged):

1. ``loginctl enable-linger <sandbox-user>`` — runs **inline as ROOT** (the
   ``sudo sandbox setup`` process itself; ``loginctl enable-linger`` is a
   root-side ``systemd-logind`` call, not a cross-boundary command).
2. ``dockerd-rootless-setuptool.sh install`` — runs **as the sandbox user**,
   crossed via ``machinectl_cmd`` (the SANDBOX identity primitive); the phase
   ``identity`` is therefore ``SANDBOX`` (the primary crossing).

**Linger rationale (load-bearing — do NOT weaken the L5-before-L6.5
ordering).** ``enable-linger`` is an architectural prerequisite of *two*
downstream phases, not one:

- (a) rootless ``dockerd`` needs the persistent per-user systemd manager;
- (b) **L6.5's ``core.dispatch.compile_dispatcher`` builds inside
  ``/run/user/<daemon-uid>/…``** (C-001 Finding-L). That per-user runtime dir
  is created by ``systemd-logind`` *only while the daemon user is lingering*;
  no linger ⇒ no ``/run/user/<uid>`` ⇒ the offline Go compile fails.

Both dependencies are cited here so a future refactor does not move or skip
``enable-linger`` and silently break L6.5.

Content-aware probe (design D10): the converged state is *linger enabled AND
rootless dockerd reachable*. ``loginctl show-user --property=Linger`` reports
linger; ``docker info`` (crossed as the sandbox user) reports dockerd. Both
true → ``ALREADY_CORRECT``; otherwise ``MISSING`` (the act enables linger then
runs the rootless install only when dockerd is not already up — the install
tool is itself idempotent but skipping it keeps a converged re-run fast).

**Post-linger user-manager readiness gate (F-014 same-class).** A freshly
created sandbox user is unknown to ``systemd-machined`` / ``loginctl`` until
linger is enabled AND the per-user manager (``user@<uid>.service``) has come
up. Two consequences:

- ``_linger_enabled`` tolerates ANY ``loginctl show-user`` failure as
  "linger absent" (the early-state observation that triggered the round-3
  recurrence). Linger-absent is MISSING, so we fail-safe to the MISSING
  branch instead of raising into the systemic phase-runner guard.
- After ``enable-linger`` in ``_act``, we bounded-poll ``user@<uid>.service``
  for ``active`` BEFORE the rootless-install crossing. ``machinectl shell``
  against a manager that is not yet ready returns an empty stdout and the
  sentinel-not-found fail-closed fires — diagnostically opaque and a real
  observed failure mode on Fedora.
"""

from __future__ import annotations

import pwd
from typing import TYPE_CHECKING

from core.exceptions import SandboxExecutionError
from core.executor import Executor
from core.host_config import is_operator_rootless
from core.setup.phase_runner import (
    Identity,
    Phase,
    PhaseResult,
    daemon_owner_crossing,
    daemon_owner_user,
    probe_sandbox_pw_or_missing,
    wait_user_manager_ready,
)

if TYPE_CHECKING:
    from core.setup.phase_runner import SetupContext


def _linger_enabled(user: str) -> bool:
    """``True`` iff ``loginctl`` reports ``Linger=yes`` for ``user``.

    Tolerates any ``SandboxExecutionError`` from ``loginctl show-user`` as
    "linger absent". A freshly-created, never-logged-in user is unknown to
    ``systemd-logind`` ("User ID N is not logged in or lingering", exit 1)
    until linger is enabled. Treating that as MISSING lets the apply pass
    proceed to ``enable-linger`` instead of crashing into the systemic
    phase-runner FAIL guard (F-014 same-class fix; mirrors the symmetric
    ``_dockerd_reachable`` catch).
    """
    try:
        result = Executor().run(
            ["loginctl", "show-user", user, "--property=Linger"]
        )
    except SandboxExecutionError:
        return False
    return "Linger=yes" in (result.stdout or "")


def _dockerd_reachable(ctx: SetupContext) -> bool:
    """``True`` iff ``docker info`` succeeds as the daemon owner (mode-aware crossing).

    separate-user: crossed via ``machinectl_cmd`` into the sandbox user, sentinel
    on (``machinectl shell`` masks the inner exit). operator-rootless: a LOCAL
    subprocess in the operator's session, sentinel off (no exit masking).
    """
    cmd = [*daemon_owner_crossing(ctx), "/bin/bash", "-c", "docker info"]
    try:
        Executor().run(cmd, sentinel=not is_operator_rootless(ctx.host_config))
    except SandboxExecutionError:
        return False
    return True


def _probe(ctx: SetupContext) -> tuple[PhaseResult, str]:
    """Content-aware probe: linger enabled AND rootless dockerd reachable.

    The plan pass runs every probe before any phase's ``act``, so on the
    canonical fresh-host first run the sandbox user does not exist yet (L2
    creates it). ``loginctl show-user`` / ``docker info`` against an absent
    user would raise (a *different* failure mode than the ``pwd.getpwnam``
    one — ``loginctl``'s "No such process") — so check the sandbox user via
    the shared guard FIRST and return ``MISSING`` before any ``loginctl`` /
    ``docker info`` runs. ``act``/``reverify`` are unguarded — by act-time
    ``depends_on=("l2a",)`` (which transitively depends on L2, the user's
    creator) means the user exists. Other L5 errors with the user present
    still propagate (systemic guard → FAIL).
    """
    op_rootless = is_operator_rootless(ctx.host_config)
    if not op_rootless:
        # separate-user: the sandbox user is created by L2, so a fresh-host plan
        # pass sees it absent — the MISSING signal. operator-rootless's owner is
        # the invoking operator (always present), so this guard does not apply.
        pw = probe_sandbox_pw_or_missing(ctx.host_config)
        if not isinstance(pw, pwd.struct_passwd):
            result, detail = pw
            return (
                result,
                f"sandbox user {ctx.host_config.host.docker_unprivileged_user!r} "
                f"does not exist yet (created by L2); dockerd will be installed "
                f"({detail})",
            )
    user = daemon_owner_user(ctx)
    # Linger is L5's concern only in separate-user; in operator-rootless it is
    # owned by the host-root batch (``LINGER`` item), so L5 neither checks nor
    # enables it here.
    if not op_rootless and not _linger_enabled(user):
        return (
            PhaseResult.MISSING,
            f"linger not enabled for {user!r}; will enable + install dockerd",
        )
    if not _dockerd_reachable(ctx):
        return (
            PhaseResult.MISSING,
            f"rootless dockerd not reachable as {user!r}; will install",
        )
    return (
        PhaseResult.ALREADY_CORRECT,
        f"rootless dockerd reachable for {user!r}",
    )


def _act(ctx: SetupContext) -> str:
    """Install rootless dockerd if absent; enable linger first in separate-user.

    The rootless-install tool is only invoked when ``docker info`` does not
    already succeed (the tool is idempotent, but skipping the multi-step
    install on a converged host keeps the apply pass fast).

    separate-user: enable linger inline as root + the post-linger user-manager
    readiness gate, then cross into the sandbox user via ``machinectl``.
    operator-rootless: linger is host-root-batch-owned (not touched here) and the
    install runs as a LOCAL subprocess in the operator's own (already-live)
    session — no ``enable-linger``, no readiness gate, no machinectl crossing.
    """
    op_rootless = is_operator_rootless(ctx.host_config)
    user = daemon_owner_user(ctx)
    if not op_rootless:
        Executor().run(["loginctl", "enable-linger", user])
        # Post-linger readiness gate: the per-user systemd manager
        # (``user@<uid>.service``) takes a moment to come up on a freshly-lingered,
        # never-logged-in user. Crossing via ``machinectl shell`` before the
        # manager is ready returns empty stdout (sentinel-not-found fail-closed).
        # The gate is shared with L6 (post-dockerd-restart) — see
        # ``phase_runner.wait_user_manager_ready``. operator-rootless runs in the
        # operator's already-live session, so the gate is unnecessary there.
        wait_user_manager_ready(user)

    if _dockerd_reachable(ctx):
        return f"rootless dockerd already up for {user!r}"

    cmd = [
        *daemon_owner_crossing(ctx),
        "/bin/bash",
        "-c",
        "dockerd-rootless-setuptool.sh install",
    ]
    Executor().run(cmd, sentinel=not op_rootless)
    return f"rootless dockerd installed for {user!r}"


def _reverify(ctx: SetupContext) -> bool:
    """Confirm rootless dockerd is reachable (+ linger, in separate-user).

    Linger is L5's concern only in separate-user; in operator-rootless it is
    host-root-batch-owned, so reverify checks only dockerd reachability there.
    """
    if not is_operator_rootless(ctx.host_config) and not _linger_enabled(
        daemon_owner_user(ctx)
    ):
        return False
    return _dockerd_reachable(ctx)


PHASE = Phase(
    id="l5",
    name="linger + rootless dockerd install",
    identity=Identity.SANDBOX,
    probe=_probe,
    act=_act,
    reverify=_reverify,
    depends_on=("l2a",),
)
