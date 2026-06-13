# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
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

**Operator drop (F-016 — load-bearing, do NOT revert to ``pipe_cmd`` for the
drop).** The probe must run as the operator so it verifies the *operator's own*
sudoers grant, and the operator-side command is the setuid binary ``sudo``. The
operator drop is therefore ``sudo_as_operator(<operator>)`` — a NORMAL-PROCESS
``sudo -u <operator>`` drop — NOT ``pipe_cmd``. Execing setuid ``sudo`` from
inside the ``--uid`` transient unit ``pipe_cmd`` builds fails with
``EXIT_EXEC`` (203) on a real host → empty output → "sentinel not found" →
this phase FAILs and rolls back a *correct* rule. That defect was mock-hidden
for the whole change (every unit test stubs ``Executor.run``); see F-016. A
``sudo -u`` drop is also faithful to runtime: the operator's real login process
runs ``sudo <SYSTEMD_RUN_PATH> …``, never a transient unit *as the drop*. F-016
does not block the crossing's INNER, though: the rule the operator's ``sudo -n``
invokes authorizes the relative transient-unit launcher (``pipe_cmd``) whose
``--uid`` transient unit execs ``/bin/bash`` → dispatch — a non-setuid binary —
so the ``--uid`` unit does NOT EXIT_EXEC here.

**Pipe crossing (C-009 design D4 — load-bearing, do NOT revert to machinectl).**
The M3-i rule renders ONLY the pipe ``Cmnd_Spec``
(``<SYSTEMD_RUN_PATH> -q --pipe --uid=<user> /bin/bash -c <dispatch>\\ <op>[…]``);
the machinectl operator spec was REMOVED. So the probe MUST cross via the pipe
(``pipe_cmd``) — a machinectl probe would be unauthorized by the freshly-
installed rule → "password required" → this phase rolls back a *correct* rule
("stranding"). The crossing is derived from ``pipe_cmd`` (the relative
transient-unit launcher, B-3) and the inner from ``dispatch_payload`` so it is
byte-identical to the rendered ``Cmnd_Spec`` per op (setup converges).

**Inner-exit recovery (C-001 Finding-J / F-004 — load-bearing, do NOT
simplify).** Each op is probed via
``sudo_as_operator(<operator>) → sudo -n <launcher> -q --pipe
--uid=<user> /bin/bash -c '<dispatch> <op> --check'`` with ``<launcher>`` the
**relative** transient-unit launcher (byte-identical to ``core.host_config.pipe_cmd()``'s
runtime output — an absolute ``<SYSTEMD_RUN_PATH>`` would spuriously MATCH on a
host where the real relative-form orchestrator call fails, the exact footgun
this probe defeats). The ``--uid`` transient unit **masks the inner ``/bin/bash -c``
exit**, so a dispatcher reject (unknown/absent/mis-pathed op → inner exit 2)
would be masked as a sudoers MATCH. L3a therefore recovers the **inner** exit
via the dispatcher's own begin/exit framing (:class:`core.executor.Executor`
``run(..., framed=True)`` — the same masked-inner-exit recovery
``core.dispatch.probe()`` performs). This
is ``framed=True``, NOT the pre-F-018 ``sentinel=True``: the latter WRAPPED the
crossed payload (``{ <dispatch> <op> --check; }; echo __SANDBOX_EXIT_<tok>_$?``),
which no per-op ``Cmnd_Spec`` could match — silently breaking the probe (and the
runtime grant) for every SUDO-mode password-operator. With ``framed=True`` the
crossed payload is the bare ``<dispatch> <op> --check`` the rule matches, and the
dispatcher emits the nonce-bound framing. L3a branches on the **recovered inner
exit**, NEVER the raw outer exit. The dispatcher's journald ``check=1`` record
is audit-only and is NEVER the control signal.

Decision matrix on the recovered inner exit, per op:

- recovered inner exit ``0`` → MATCH (healthy);
- non-zero recovered inner exit with ``password is required`` on stderr → the
  sudoers rule did not grant this op (a missed backslash-escape per F-004, OR
  ``SYSTEMD_RUN_PATH`` drift between L0 resolution and the live ``secure_path``);
- any other non-zero recovered inner exit (incl. dispatcher reject exit 2 —
  absent / mis-pathed / unknown-op binary) → broken / absent / mis-pathed rule
  or binary.

Any non-MATCH outcome raises (→ phase-runner FAIL → ``_rollback`` removes the
drop-in) with a diagnostic naming the failing op, the recovered inner exit, and
the resolved-vs-pinned transient-unit launcher path (the pipe ``Cmnd_Spec``
launcher, i.e. ``SYSTEMD_RUN_PATH``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.dispatch import Op, dispatch_payload
from core.exceptions import SandboxExecutionError
from core.executor import Executor
from core.host_config import DockerExecutionMode, pipe_cmd, sudo_as_operator
from core.setup.l0_identity import resolve_systemd_run_path
from core.setup.l3_sudoers import drop_in_path
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
    """Build the per-op probe argv (relative transient-unit launcher, B-3).

    ``sudo_as_operator(<operator>) + ["sudo", "-n", *pipe_cmd(<user>),
    "/bin/bash", "-c", "<dispatch> <op> --check"]`` — the
    ``sudo … <SYSTEMD_RUN_PATH> -q --pipe --uid=<user> /bin/bash -c …`` tail is
    byte-identical to what the operator emits at runtime except for the
    ``sudo -n`` (non-interactive: a missing grant fails fast with the
    ``password is required`` marker instead of prompting) and the ``--check``
    no-op-success op shape. The crossing is derived from
    :func:`core.host_config.pipe_cmd` and the inner payload from
    :func:`core.dispatch.dispatch_payload` — never a hand-typed launcher
    literal — so the probe MATCHES the rendered pipe ``Cmnd_Spec`` per op and
    setup converges (a non-match still rolls back the drop-in).

    The probe crosses the boundary via the PIPE (``pipe_cmd``), matching the
    M3-i pipe ``Cmnd_Spec`` — the machinectl operator spec was REMOVED, so a
    machinectl probe would be unauthorized and would roll back the freshly-
    installed rule (design D4). ``pipe_cmd`` here emits the RELATIVE
    transient-unit launcher (B-3: byte-identical to the runtime form; sudo
    resolves it to an absolute path via ``secure_path``, exactly as the rendered
    ``Cmnd_Spec`` abspaths its launcher).

    The ``sudo_as_operator`` (``sudo -u <operator>``) prefix drops root to the
    operator in a normal process so the setuid ``sudo`` that follows can exec
    (F-016 — a ``pipe_cmd`` ``--uid`` transient-unit drop EXIT_EXECs on a setuid
    binary). F-016 does NOT block this crossing's inner: the operator's
    ``sudo -n`` invokes the rule, and the authorized command is the relative
    transient-unit launcher whose ``--uid`` transient unit execs ``/bin/bash`` →
    dispatch — a NON-setuid binary — so the ``--uid`` unit does not EXIT_EXEC.
    """
    sandbox_user = host_config.host.docker_unprivileged_user
    inner = dispatch_payload(op.value, ["--check"])
    return [
        *sudo_as_operator(operator),
        "sudo",
        "-n",
        *pipe_cmd(sandbox_user),
        "/bin/bash",
        "-c",
        inner,
    ]


def _classify(host_config: HostConfig, operator: str, op: Op) -> None:
    """Probe one op; raise :class:`PerOpProbeError` on any non-MATCH.

    The recovered inner exit is the control signal. :class:`Executor` with
    ``framed=True`` does NOT wrap the crossed payload — the bare
    ``<dispatch> <op> --check`` is what sudo authorizes, so it MATCHES the
    per-op ``Cmnd_Spec`` (a wrapping ``sentinel=True`` injected
    ``…; echo __SANDBOX_EXIT_<tok>_$?`` and made the authorized command
    unmatchable — F-018, the round-7 fix). The dispatcher itself emits the
    ``__SANDBOX_BEGIN_<nonce>`` / ``__SANDBOX_EXIT_<nonce>_$?`` framing (the
    ``--check`` short-circuit emits ``_0``), and the Executor raises
    :class:`SandboxExecutionError` on a non-zero *recovered* inner exit. A clean
    run (recovered inner exit 0) is MATCH; a sudo refusal never runs the
    dispatcher → no framing → the Executor fail-closes with the
    ``password is required`` stderr, which we re-classify into the decision
    matrix below.
    """
    argv = _probe_argv(host_config, operator, op)
    try:
        Executor().run(argv, framed=True)
    except SandboxExecutionError as exc:
        message = str(exc)
        resolved = resolve_systemd_run_path(host_config)
        if _PASSWORD_REQUIRED_MARKER in message:
            reason = (
                "sudoers rule did not grant this op — a missed backslash-"
                "escape (F-004) OR SYSTEMD_RUN_PATH drift between L0 and the "
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
            f"Resolved SYSTEMD_RUN_PATH (pinned in the rule): {resolved!r}."
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
    drop_in_path(ctx.operator).unlink(missing_ok=True)


PHASE = Phase(
    id="l3a",
    name="per-op rule probe (F-004 silent-footgun defense)",
    identity=Identity.OPERATOR,
    probe=_probe,
    act=_act,
    reverify=_reverify,
    depends_on=("l3",),
    rollback=_rollback,
    # operator-rootless installs no L3 boundary rule, so there is no per-op
    # sudoers grant to probe.
    applies_in=frozenset({DockerExecutionMode.SEPARATE_USER}),
)
