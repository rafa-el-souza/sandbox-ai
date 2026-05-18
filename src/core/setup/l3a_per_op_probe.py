"""L3a — per-op probe: the F-004 silent-footgun defense for the L3 rule.

``l3a_per_op_probe`` matches the phase-discovery regex, so it MUST export a
valid module-level ``PHASE`` (orchestrator decision 2). It is modelled as a
phase whose **work is verification**: there is no cheap idempotent skip for
"is every enumerated op actually granted by the installed rule," so

- :func:`_probe` always returns ``MISSING`` (the sweep must run every time);
- :func:`_act` performs the per-op ``--check`` MATCH sweep over every op in
  :class:`core.dispatch.Op`, raising on the first non-MATCH so the phase-runner
  classifies the phase ``FAIL`` and fires :func:`_rollback`;
- :func:`_reverify` re-runs the sweep (a clean second pass confirms);
- :func:`_rollback` removes the L3 drop-in (idempotent ``rm`` — same target as
  L3's own rollback; both removals are idempotent so the half-written-grant
  window is closed regardless of which phase fails first).

``depends_on`` is ``("l3",)`` (the rule must be installed before it can be
probed); ``identity`` is OPERATOR (the probe runs as the operator — it verifies
the operator's *own* sudo grant works).

**Inner-exit recovery (C-001 Finding-J / F-004 — load-bearing, do NOT
simplify).** Each op is probed via
``pipe_cmd(<operator>) → sudo -n machinectl shell <user>@.host /bin/bash -c
'<dispatch> <op> --check'`` with **relative** ``machinectl`` (byte-identical to
``core.host_config.machinectl_cmd()``'s runtime output — an absolute
``/usr/bin/machinectl`` would spuriously MATCH on a host where the real
relative-form orchestrator call fails, the exact footgun this probe defeats).
``pipe_cmd`` propagates only sudo/machinectl's exit; ``machinectl shell``
**masks the inner ``/bin/bash -c`` exit**, so a dispatcher reject
(unknown/absent/mis-pathed op → inner exit 2) would be masked as a sudoers
MATCH. L3a therefore recovers the **inner** exit via the sentinel mechanism
(:class:`core.executor.Executor` ``run(..., sentinel=True)`` — mirroring how
``core.dispatch.probe()`` recovers a masked inner exit) and branches on the
**recovered inner exit**, NEVER the raw ``pipe_cmd`` exit. The dispatcher's
journald ``check=1`` record is audit-only and is NEVER the control signal.

Decision matrix on the recovered inner exit, per op:

- recovered inner exit ``0`` → MATCH (healthy);
- non-zero recovered inner exit with ``password is required`` on stderr → the
  sudoers rule did not grant this op (a missed backslash-escape per F-004, OR
  ``MACHINECTL_PATH`` drift between L0 resolution and the live ``secure_path``);
- any other non-zero recovered inner exit (incl. dispatcher reject exit 2 —
  absent / mis-pathed / unknown-op binary) → broken / absent / mis-pathed rule
  or binary.

Any non-MATCH outcome raises (→ phase-runner FAIL → ``_rollback`` removes the
drop-in) with a diagnostic naming the failing op, the recovered inner exit, and
the resolved-vs-pinned machinectl path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.dispatch import _DISPATCH_BINARY, Op
from core.exceptions import SandboxExecutionError
from core.executor import Executor
from core.host_config import pipe_cmd
from core.setup.l0_identity import resolve_machinectl_path
from core.setup.l3_sudoers_polkit import _drop_in_path
from core.setup.phase_runner import Identity, Phase, PhaseResult

if TYPE_CHECKING:
    from core.host_config import HostConfig
    from core.setup.phase_runner import SetupContext

# stderr marker sudo emits when the operator has no matching NOPASSWD grant for
# the resolved Cmnd — the F-004 / MACHINECTL_PATH-drift signature.
_PASSWORD_REQUIRED_MARKER = "password is required"


class PerOpProbeError(SandboxExecutionError):
    """An enumerated op did not resolve to a sudoers MATCH.

    Carries the failing op, the recovered inner exit, and the resolved-vs-
    pinned machinectl path so the operator-facing FAIL detail is actionable.
    """


def _probe_argv(host_config: HostConfig, operator: str, op: Op) -> list[str]:
    """Build the per-op probe argv (relative ``machinectl``, B-3).

    ``pipe_cmd(<operator>) + ["sudo", "-n", "machinectl", "shell",
    "<user>@.host", "/bin/bash", "-c", "<dispatch> <op> --check"]`` — byte-
    identical to what the orchestrator emits at runtime except for the
    ``sudo -n`` (non-interactive: a missing grant fails fast with the
    ``password is required`` marker instead of prompting) and the ``--check``
    no-op-success op shape.
    """
    sandbox_user = host_config.host.docker_unprivileged_user
    inner = f"{_DISPATCH_BINARY} {op.value} --check"
    return [
        *pipe_cmd(operator),
        "sudo",
        "-n",
        "machinectl",
        "shell",
        f"{sandbox_user}@.host",
        "/bin/bash",
        "-c",
        inner,
    ]


def _classify(host_config: HostConfig, operator: str, op: Op) -> None:
    """Probe one op; raise :class:`PerOpProbeError` on any non-MATCH.

    The recovered inner exit is the control signal. :class:`Executor` with
    ``sentinel=True`` injects ``…; echo __SANDBOX_EXIT_<tok>_$?`` into the
    ``/bin/bash -c`` payload and raises :class:`SandboxExecutionError` on a
    non-zero *inner* exit (mirroring ``core.dispatch.probe()``). A clean run
    (recovered inner exit 0) is MATCH; the raised error carries the recovered
    inner exit + stderr, which we re-classify into the decision matrix.
    """
    argv = _probe_argv(host_config, operator, op)
    try:
        Executor().run(argv, sentinel=True)
    except SandboxExecutionError as exc:
        message = str(exc)
        resolved = resolve_machinectl_path(host_config)
        if _PASSWORD_REQUIRED_MARKER in message:
            reason = (
                "sudoers rule did not grant this op — a missed backslash-"
                "escape (F-004) OR MACHINECTL_PATH drift between L0 and the "
                "live secure_path"
            )
        else:
            reason = (
                "broken / absent / mis-pathed rule or dispatcher binary "
                "(incl. dispatcher reject exit 2)"
            )
        raise PerOpProbeError(
            f"L3a per-op probe FAILED for op {op.value!r}: {reason}. "
            f"Recovered inner exit / trace: {message}. "
            f"Resolved machinectl path (pinned in the rule): {resolved!r}."
        ) from exc


def _sweep(ctx: SetupContext) -> str:
    """Run the per-op ``--check`` MATCH sweep over every op; raise on failure."""
    operator = ctx.operator
    for op in Op:
        _classify(ctx.host_config, operator, op)
    return f"every op in core.dispatch.Op ({len(list(Op))}) resolved to MATCH"


def _probe(_ctx: SetupContext) -> tuple[PhaseResult, str]:
    """Always ``MISSING``: the verification sweep has no cheap idempotent skip.

    (Orchestrator decision 2 — L3a's *work* is verification; the sweep must run
    every apply pass. ``MISSING`` makes the phase-runner proceed to ``act``.)
    """
    return (
        PhaseResult.MISSING,
        "per-op rule probe must run (no idempotent skip — verification phase)",
    )


def _act(ctx: SetupContext) -> str:
    """Perform the per-op MATCH sweep; raising → phase-runner FAIL → rollback."""
    return _sweep(ctx)


def _reverify(ctx: SetupContext) -> bool:
    """Re-run the sweep; a clean second pass confirms convergence.

    A non-MATCH here raises (the sweep raises on the first non-MATCH); the
    phase-runner catches it, classifies the phase ``FAIL`` and fires
    :func:`_rollback`.
    """
    _sweep(ctx)
    return True


def _rollback(ctx: SetupContext) -> None:
    """Remove the L3 drop-in (idempotent ``rm`` — same target as L3's rollback).

    On any non-MATCH the just-installed rule is broken; removing it closes the
    half-written-grant window (design D1). ``missing_ok=True`` keeps the
    removal idempotent.
    """
    _drop_in_path(ctx.host_config, ctx.operator).unlink(missing_ok=True)


PHASE = Phase(
    id="l3a",
    name="per-op rule probe (F-004 silent-footgun defense)",
    identity=Identity.OPERATOR,
    probe=_probe,
    act=_act,
    reverify=_reverify,
    depends_on=("l3",),
    rollback=_rollback,
)
