"""L5 — linger + rootless dockerd install (content-aware).

Two mutations, two identities:

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
"""

from __future__ import annotations

import pwd
from typing import TYPE_CHECKING

from core.exceptions import SandboxExecutionError
from core.executor import Executor
from core.host_config import machinectl_cmd
from core.setup.phase_runner import (
    Identity,
    Phase,
    PhaseResult,
    probe_sandbox_pw_or_missing,
)

if TYPE_CHECKING:
    from core.host_config import HostConfig
    from core.setup.phase_runner import SetupContext


def _sandbox_user(host_config: HostConfig) -> str:
    """The unprivileged docker user the rootless daemon runs as."""
    return host_config.host.docker_unprivileged_user


def _linger_enabled(user: str) -> bool:
    """``True`` iff ``loginctl`` reports ``Linger=yes`` for ``user``."""
    result = Executor().run(
        ["loginctl", "show-user", user, "--property=Linger"]
    )
    return "Linger=yes" in (result.stdout or "")


def _dockerd_reachable(host_config: HostConfig) -> bool:
    """``True`` iff ``docker info`` succeeds crossed as the sandbox user."""
    cmd = [
        *machinectl_cmd(
            _sandbox_user(host_config),
            host_config.host.machinectl_authentication,
        ),
        "/bin/bash",
        "-c",
        "docker info",
    ]
    try:
        Executor().run(cmd, sentinel=True)
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
    ``depends_on=("l4",)`` plus L2 having acted, the user exists. Other L5
    errors with the user present still propagate (systemic guard → FAIL).
    """
    pw = probe_sandbox_pw_or_missing(ctx.host_config)
    if not isinstance(pw, pwd.struct_passwd):
        result, detail = pw
        return (
            result,
            f"sandbox user {ctx.host_config.host.docker_unprivileged_user!r} "
            f"does not exist yet (created by L2); dockerd will be installed "
            f"({detail})",
        )
    host_config = ctx.host_config
    user = _sandbox_user(host_config)
    if not _linger_enabled(user):
        return (
            PhaseResult.MISSING,
            f"linger not enabled for {user!r}; will enable + install dockerd",
        )
    if not _dockerd_reachable(host_config):
        return (
            PhaseResult.MISSING,
            f"rootless dockerd not reachable as {user!r}; will install",
        )
    return (
        PhaseResult.ALREADY_CORRECT,
        f"linger enabled and rootless dockerd reachable for {user!r}",
    )


def _act(ctx: SetupContext) -> str:
    """Enable linger inline as root, then install rootless dockerd if absent.

    The rootless-install tool is only invoked when ``docker info`` does not
    already succeed (the tool is idempotent, but skipping the multi-step
    install on a converged host keeps the apply pass fast).
    """
    host_config = ctx.host_config
    user = _sandbox_user(host_config)
    Executor().run(["loginctl", "enable-linger", user])

    if _dockerd_reachable(host_config):
        return f"linger enabled for {user!r}; rootless dockerd already up"

    cmd = [
        *machinectl_cmd(user, host_config.host.machinectl_authentication),
        "/bin/bash",
        "-c",
        "dockerd-rootless-setuptool.sh install",
    ]
    Executor().run(cmd, sentinel=True)
    return f"linger enabled for {user!r}; rootless dockerd installed"


def _reverify(ctx: SetupContext) -> bool:
    """Confirm linger is enabled and rootless dockerd is reachable."""
    host_config = ctx.host_config
    return _linger_enabled(_sandbox_user(host_config)) and _dockerd_reachable(
        host_config
    )


PHASE = Phase(
    id="l5",
    name="linger + rootless dockerd install",
    identity=Identity.SANDBOX,
    probe=_probe,
    act=_act,
    reverify=_reverify,
    depends_on=("l4",),
)
