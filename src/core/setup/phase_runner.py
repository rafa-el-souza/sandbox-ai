# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase-runner contract for setup's probe-act-reverify ceremony.

``sandbox setup`` runs a phased host-provisioning ceremony (L0..L8 plus the
named sub-phases L6a/L6.5). Each phase is a self-contained module under
:mod:`core.setup` that exports a single module-level ``PHASE: Phase`` object;
this module discovers them, schedules them by their ``depends_on`` graph, and
runs the two-pass plan/apply ceremony over them (design D5).

Two passes, one code path:

- **Plan pass** (:func:`run_plan_pass`) — every phase's probe runs; nothing is
  mutated. ``sandbox setup --dry-run`` IS this pass. Output is a list of
  :class:`PhasePlanOutcome`.
- **Apply pass** (:func:`run_apply_pass`) — re-probe; if a phase is already
  correct, skip; otherwise ``act`` then ``reverify``. A phase failure marks its
  transitive dependents ``BLOCKED_BY`` and the pass *continues* with the
  independent phases (operators see every reachable failure in one run, design
  D5). A phase that carries a ``rollback`` callable has it fired when the phase
  fails (the L3a sudoers-probe case, design D1) — other phases are never rolled
  back.

Content-aware probe contract (design D10 — load-bearing, shared by every phase
author): a phase's ``probe`` MUST be **content-aware**. It computes the
*expected* state from the current source of truth (the ``core.dispatch.Op``
enum, the dispatcher source bundle, a ``BINARY_REGISTRY`` pin, the operator's
hand-edited toml, …) and compares it against the *observed* state on disk. A
naive file-exists probe is forbidden: it would silently skip work after a wheel
upgrade changed the expected state (a stale sudoers rule, a stale dispatcher
binary). The :class:`Phase` docstring restates this so all phase authors share
the contract; the ``assert_phase_content_aware`` test fixture (in
``tests/unit/core/setup/conftest.py``) mechanically enforces it by driving a
phase's probe against synthetic stale-content inputs and asserting it returns
``DRIFT`` rather than ``ALREADY_CORRECT``.

Identity routing (design D3): setup straddles three identities, each with a
fixed cross-boundary primitive. :func:`route` maps an :class:`Identity` to the
argv prefix — ``[]`` for ROOT, ``pipe_cmd(<operator>)`` for OPERATOR,
``machinectl_cmd(<sandbox-user>)`` for SANDBOX. Setup modules may
import ``machinectl_cmd`` directly: they match the pre-existing
``src/core/setup/*.py`` allowlist category the ``host-config`` capability
defines (no allowlist amendment by this change).

Phase context (the explicit transport): every phase callback (``probe``,
``act``, ``reverify``, ``rollback``) receives a single immutable
:class:`SetupContext` carrying the parsed :class:`~core.host_config.HostConfig`
and the already-resolved ``operator`` user name. The ``--operator`` flag is CLI
input that is NOT re-derivable from the host, so it is *transported* into the
phases via this object — there is no hidden module state / environment
side-channel (the project bans it). ``operator`` is resolved once by
:func:`core.setup.l0_identity.resolve_operator`; the sandbox user is read from
``ctx.host_config.host.docker_unprivileged_user`` (single source — it is not a
separate context field).
"""

from __future__ import annotations

import importlib
import pkgutil
import pwd
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import core.setup as _setup_package
from core.executor import Executor
from core.host_config import (
    DockerExecutionMode,
    is_operator_rootless,
    machinectl_cmd,
    pipe_cmd,
)

if TYPE_CHECKING:
    from types import ModuleType

    from core.host_config import HostConfig


@dataclass(frozen=True)
class SetupContext:
    """Immutable per-run context threaded through every phase callback.

    The explicit transport for state a phase needs but cannot re-derive from
    the host alone (the project bans hidden module / environment state):

    Attributes:
        host_config: The parsed :class:`~core.host_config.HostConfig`. The
            sandbox user is ``host_config.host.docker_unprivileged_user`` (a
            single source — deliberately NOT a separate context field).
        operator: The already-resolved operator user name (the
            :func:`core.setup.l0_identity.resolve_operator` result — the
            ``--operator`` flag is CLI input, not host-re-derivable, so it is
            carried here rather than re-resolved per phase).
    """

    host_config: HostConfig
    operator: str


class Identity(StrEnum):
    """The identity a phase's cross-boundary work runs as (design D3).

    - ``ROOT`` — no prefix; the ``sudo sandbox setup`` process itself.
    - ``OPERATOR`` — crossed via ``pipe_cmd(<operator>)``.
    - ``SANDBOX`` — crossed via ``machinectl_cmd(<sandbox-user>)``.
    """

    ROOT = "root"
    OPERATOR = "operator"
    SANDBOX = "sandbox"


class PhaseResult(StrEnum):
    """The outcome classification of a single phase's probe or apply.

    - ``ALREADY_CORRECT`` — observed state matches expected; nothing to do.
    - ``MISSING`` — the owned state is absent; the act would create it.
    - ``DRIFT`` — the owned state exists but does not match expected source
      (the content-aware-probe signal; the act would converge it).
    - ``CONFLICT`` — observed state is incompatible and cannot be safely
      converged (e.g. an operator value the phase refuses to overwrite).
    - ``FAIL`` — the act or reverify failed.
    - ``BLOCKED_BY`` — a transitive ``depends_on`` ancestor failed; this phase
      was not run.
    - ``SKIPPED`` — the phase was deliberately not run (e.g. plan pass for a
      phase outside the requested subset, or a non-applicable phase).
    """

    ALREADY_CORRECT = "already-correct"
    MISSING = "missing"
    DRIFT = "drift"
    CONFLICT = "conflict"
    FAIL = "fail"
    BLOCKED_BY = "blocked-by"
    SKIPPED = "skipped"


# A probe inspects observed vs. expected state and returns a (result, detail)
# pair. It MUST NOT mutate the host. ``detail`` is operator-facing text.
ProbeFn = Callable[["SetupContext"], "tuple[PhaseResult, str]"]

# An act performs the phase's mutation. It returns operator-facing detail text;
# raising signals failure (the runner catches and classifies as FAIL).
ActFn = Callable[["SetupContext"], str]

# A reverify re-checks state after the act. It returns ``True`` iff the act
# converged the phase to the expected state.
ReverifyFn = Callable[["SetupContext"], bool]

# A rollback undoes a failed phase's partial mutation (the L3a case).
RollbackFn = Callable[["SetupContext"], None]


@dataclass(frozen=True)
class Phase:
    """An immutable setup phase: probe-act-reverify with explicit dependencies.

    **Content-aware probe contract (design D10 — every phase author MUST honor
    this).** ``probe`` MUST be content-aware: it computes the *expected* state
    from the current source of truth (the ``core.dispatch.Op`` enum, the
    dispatcher source bundle, a ``BINARY_REGISTRY`` pin, the operator's
    hand-edited toml, …) and compares it against the *observed* state on disk.
    A file-exists-only probe is FORBIDDEN — it would silently skip work after a
    wheel upgrade changed the expected state (a stale sudoers rule after the Op
    enum grew, a stale dispatcher binary after the source bundle changed). A
    probe that finds the owned state present but not matching current source
    MUST return ``DRIFT`` (not ``ALREADY_CORRECT``); absent state returns
    ``MISSING``; an unconvergeable conflict returns ``CONFLICT``. The
    ``assert_phase_content_aware`` test fixture enforces this mechanically.

    ``probe``, ``act``, ``reverify`` and ``rollback`` each receive a single
    :class:`SetupContext` (the parsed :class:`~core.host_config.HostConfig` plus
    the already-resolved ``operator``). A phase reads the sandbox user from
    ``ctx.host_config.host.docker_unprivileged_user`` and the operator from
    ``ctx.operator`` — it never re-resolves the operator itself.

    Attributes:
        id: Stable phase identifier (e.g. ``"l0"``, ``"l6a"``, ``"l65"``).
        name: Human-readable phase name for operator-facing output.
        identity: Which :class:`Identity` the phase's cross-boundary work runs
            as (drives :func:`route`).
        probe: Content-aware probe (see contract above). No mutations.
        act: Performs the phase's mutation; returns detail, raises on failure.
        reverify: Re-checks state after ``act``; ``True`` iff converged.
        depends_on: Phase ids that must succeed before this phase runs. The
            canonical L0→…→L8 order is expressed via these edges, NOT a literal
            hardcoded sequence (orchestrator decision 1).
        rollback: Optional undo for a failed phase (the L3a sudoers-probe case,
            design D1). ``None`` for phases that are not rolled back.
        applies_in: The set of :class:`~core.host_config.DockerExecutionMode`
            values in which this phase runs. Defaults to ALL modes (every phase
            is mode-agnostic unless it opts out). A phase whose ``applies_in``
            EXCLUDES the active ``docker_execution_mode`` is reported
            ``SKIPPED`` (not run — its probe/act/reverify are never called) in
            BOTH the plan and apply passes. A mode-skip is "not applicable",
            NOT a failure: it does NOT block its dependents (a phase depending
            on a mode-skipped phase still runs).
    """

    id: str
    name: str
    identity: Identity
    probe: ProbeFn
    act: ActFn
    reverify: ReverifyFn
    depends_on: tuple[str, ...] = ()
    rollback: RollbackFn | None = None
    applies_in: frozenset[DockerExecutionMode] = frozenset(DockerExecutionMode)


@dataclass(frozen=True)
class PhasePlanOutcome:
    """Immutable result of a single phase's plan-pass probe.

    Distinct from :class:`core.dispatch.ProbeOutcome` (the dispatcher-op probe
    result) — this is the setup-phase-local plan outcome (orchestrator
    decision 2).

    Attributes:
        phase_id: The phase's :attr:`Phase.id`.
        result: The :class:`PhaseResult` classification.
        detail: Operator-facing human-readable detail text.
    """

    phase_id: str
    result: PhaseResult
    detail: str


@dataclass(frozen=True)
class PhaseApplyOutcome:
    """Immutable result of a single phase's apply-pass run.

    Attributes:
        phase_id: The phase's :attr:`Phase.id`.
        result: The :class:`PhaseResult` classification.
        detail: Operator-facing human-readable detail text.
        reverified: ``True`` iff the phase acted and its reverify confirmed
            convergence; ``False`` for skipped / blocked / failed phases.
    """

    phase_id: str
    result: PhaseResult
    detail: str
    reverified: bool


class PhaseDependencyError(ValueError):
    """The phase graph is malformed: a dependency cycle or an unknown id.

    Raised by :func:`order_phases` (and therefore by both passes) before any
    probe or act runs — a malformed graph is a programming error in the phase
    modules, not an operator-recoverable condition.
    """


class PhaseDiscoveryError(ValueError):
    """A discovered phase module is missing its module-level ``PHASE`` object.

    Every ``l<digits>[a]?_*`` submodule of :mod:`core.setup` MUST export a
    module-level ``PHASE: Phase``. A module that matches the phase-module name
    pattern but lacks ``PHASE`` is a programming error (fail loud, do not skip
    silently) — orchestrator decision / design constraint.
    """


# A phase module's name matches ``l<digits>[a]?_<rest>`` — e.g. ``l0_identity``,
# ``l1_kernel``, ``l6a_runsc``, ``l65_dispatcher``, ``l3a_per_op_probe``. The
# package marker (``__init__``), this runner, and helper modules (``conftest``,
# the ``extras`` sub-package) do not match and are skipped. The optional single
# ``a`` is the L6a / L3a sub-phase convention; ``_<rest>`` must be non-empty.
_PHASE_MODULE_RE = re.compile(r"^l\d+a?_.+$")


def _is_phase_module_name(name: str) -> bool:
    """``True`` iff ``name`` is a ``l<digits>[a]?_<rest>`` phase-module name."""
    return _PHASE_MODULE_RE.match(name) is not None


def discover_phases(package: ModuleType = _setup_package) -> list[Phase]:
    """Import every phase submodule of ``package`` and collect its ``PHASE``.

    ``package`` is a test seam passed as a parameter (orchestrator decision 1,
    anti-hack rule 5): production callers pass nothing and discovery runs over
    the real :mod:`core.setup` package; tests pass a synthetic package whose
    ``lN_*`` stub modules each expose a ``PHASE`` (or deliberately omit it to
    exercise the missing-``PHASE`` contract).

    A submodule whose name matches the ``l<digits>[a]?_*`` phase-module pattern
    MUST expose a module-level ``PHASE: Phase``; one that does not raises
    :class:`PhaseDiscoveryError` (fail loud — a phase module without ``PHASE``
    is a programming error). Non-matching submodules (helpers, sub-packages)
    are skipped. The returned list is unordered; callers pass it to
    :func:`run_plan_pass` / :func:`run_apply_pass`, which order it by the
    ``depends_on`` graph.

    Returns:
        Every discovered phase, in module-iteration order (NOT dependency
        order — the passes topologically sort).
    """
    phases: list[Phase] = []
    for module_info in pkgutil.iter_modules(package.__path__):
        if not _is_phase_module_name(module_info.name):
            continue
        full_name = f"{package.__name__}.{module_info.name}"
        module = importlib.import_module(full_name)
        phase = getattr(module, "PHASE", None)
        if not isinstance(phase, Phase):
            raise PhaseDiscoveryError(
                f"phase module {full_name!r} matches the phase-module pattern "
                f"but does not export a module-level `PHASE: Phase` object"
            )
        phases.append(phase)
    return phases


def order_phases(
    phases: list[Phase], *, allow_external_deps: bool = False
) -> list[Phase]:
    """Topologically sort ``phases`` by their ``depends_on`` edges.

    Deterministic: ties are broken by phase ``id`` so the operator-facing
    ordering is stable across runs. Raises :class:`PhaseDependencyError` on an
    unknown dependency id or a dependency cycle.

    ``allow_external_deps=True`` is for a deliberate subset run — a
    ``depends_on`` id that is not itself in ``phases`` is then treated as
    already-satisfied rather than an error. This is what ``--update-runsc``
    needs: it re-runs ONLY ``l6a``, whose ``l6`` dependency is known-satisfied
    on the converged host it runs against. The default is strict (an unknown
    dep is a :class:`PhaseDependencyError`) so the full-ceremony path keeps its
    misconfiguration guard.
    """
    by_id: dict[str, Phase] = {p.id: p for p in phases}
    if not allow_external_deps:
        for phase in phases:
            for dep in phase.depends_on:
                if dep not in by_id:
                    raise PhaseDependencyError(
                        f"phase {phase.id!r} depends on unknown phase {dep!r}"
                    )
    ordered: list[Phase] = []
    placed: set[str] = set()
    # Kahn-style with deterministic id-sorted selection. A dep absent from
    # ``by_id`` is an external (assumed-satisfied) dep — only reachable when
    # ``allow_external_deps`` is set, since the strict path rejected it above.
    remaining = sorted(by_id.values(), key=lambda p: p.id)
    while remaining:
        ready = [
            p
            for p in remaining
            if all(d in placed or d not in by_id for d in p.depends_on)
        ]
        if not ready:
            cycle = sorted(p.id for p in remaining)
            raise PhaseDependencyError(
                f"dependency cycle among phases: {cycle}"
            )
        nxt = ready[0]
        ordered.append(nxt)
        placed.add(nxt.id)
        remaining.remove(nxt)
    return ordered


def route(identity: Identity, ctx: SetupContext) -> list[str]:
    """Return the argv prefix for ``identity``'s cross-boundary work (design D3).

    - :attr:`Identity.ROOT` → ``[]`` (the ``sudo sandbox setup`` process runs
      these directly as root).
    - :attr:`Identity.OPERATOR` → ``pipe_cmd(ctx.operator)`` (the byte-pipe
      crossing into the operator; its ``--uid`` transient unit re-runs
      ``initgroups`` so a fresh unit reflects the post-``usermod`` group set).
    - :attr:`Identity.SANDBOX` →
      ``machinectl_cmd(ctx.host_config.host.docker_unprivileged_user)`` — the
      dedicated sandbox user is read from the context's host config; identical to
      the runtime orchestrator's primitive. ``Identity.SANDBOX`` is intrinsically
      the separate-user dedicated account (op-rootless has no machinectl crossing),
      so this is a literal-field read of that account, NOT owner-resolution.
    """
    if identity == Identity.ROOT:
        return []
    if identity == Identity.OPERATOR:
        return pipe_cmd(ctx.operator)
    user = ctx.host_config.host.docker_unprivileged_user
    if user is None:
        raise ValueError("Identity.SANDBOX crossing requires docker_unprivileged_user")
    return machinectl_cmd(user)


def daemon_owner_user(ctx: SetupContext) -> str:
    """The OS user that owns the rootless docker daemon.

    separate-user: the dedicated ``host.docker_unprivileged_user``.
    operator-rootless: the operator (the daemon runs as the operator's own user).

    NOTE — this is the SETUP-time owner resolver and intentionally does NOT
    delegate to ``resolve_daemon_owner_settings``: in op-rootless it returns the
    setup-resolved ``ctx.operator`` (which setup may derive from ``$SUDO_USER`` /
    ``--operator`` when run under sudo), NOT ``getpass.getuser()``. The two
    coincide on the runtime path (the operator runs as themselves) but diverge in
    setup, so the owner must come from the context, not the process identity.
    """
    if is_operator_rootless(ctx.host_config):
        return ctx.operator
    user = ctx.host_config.host.docker_unprivileged_user
    if user is None:
        raise ValueError("separate-user setup context is missing docker_unprivileged_user")
    return user


def daemon_owner_crossing(ctx: SetupContext) -> list[str]:
    """argv prefix to run a ``/bin/bash -c`` as the rootless-daemon owner in their user session.

    separate-user: ``machinectl_cmd(<sandbox-user>)`` — BYTE-IDENTICAL to
    what L5/L6/L7 build inline (so the L5/L6/L7 separate-user path is unchanged).
    Root must still drop into the dedicated ``sandbox`` user.

    operator-rootless: a LOCAL ``env …`` prefix that re-injects the operator's
    user-session environment (no ``sudo`` drop — setup already runs AS the operator,
    D5). There is no root→operator privilege drop, so the C-prime ``sudo_as_operator``
    half is gone; but the **session env must still be injected explicitly**, because
    :class:`core.executor.Executor` runs every subprocess in a *sterile matrix*
    (only ``PATH`` survives — ``HOME`` / ``XDG_RUNTIME_DIR`` / ``DBUS_SESSION_BUS_ADDRESS``
    / ``DOCKER_HOST`` are scrubbed *regardless of who runs setup*). Rootless docker +
    ``systemctl --user`` need that session env: ``dockerd-rootless-setuptool.sh``
    fails with "HOME needs to be set", and ``docker`` falls back to the rootful
    ``/var/run/docker.sock`` (permission-denied) without ``DOCKER_HOST``. (Real-host
    finding 8.11 — the §10 convergence smoke; D4/D5's "no injection needed" reasoning
    was wrong: it accounted for the absent privilege drop but not the sterile Executor.)
    The C-prime ``machinectl`` recipe survives only on the separate-user branch above.
    """
    if is_operator_rootless(ctx.host_config):
        pw = pwd.getpwnam(ctx.operator)
        run_dir = f"/run/user/{pw.pw_uid}"
        return [
            "env",
            f"HOME={pw.pw_dir}",
            f"XDG_RUNTIME_DIR={run_dir}",
            f"DBUS_SESSION_BUS_ADDRESS=unix:path={run_dir}/bus",
            f"DOCKER_HOST=unix://{run_dir}/docker.sock",
        ]
    user = ctx.host_config.host.docker_unprivileged_user
    if user is None:
        raise ValueError("separate-user daemon-owner crossing requires docker_unprivileged_user")
    return machinectl_cmd(user)


class SandboxUserNotYetCreated(KeyError):
    """The sandbox OS user does not exist *yet* during a probe.

    The single-sourced guard type for the content-aware-probe contract: a
    phase whose probe resolves the sandbox user / uid / home before L2 has
    created it (the canonical fresh-host first run) must treat the absent user
    as the ``MISSING`` signal — a later phase (L2) creates it — NOT as a crash.
    :func:`resolve_sandbox_pw` raises this; :func:`probe_sandbox_pw_or_missing`
    converts it to a ``(MISSING, detail)`` probe outcome so the runner's plan
    and apply passes never see an escaping ``KeyError``.
    """


def resolve_sandbox_pw(host_config: HostConfig) -> pwd.struct_passwd:
    """Resolve the sandbox user's passwd entry, raising the typed guard.

    Wraps :func:`pwd.getpwnam` for the marker-sourced
    ``docker_unprivileged_user`` and re-raises a bare ``KeyError`` as
    :class:`SandboxUserNotYetCreated` so probe call-sites can branch on the
    not-yet-created case via :func:`probe_sandbox_pw_or_missing`.
    """
    user = host_config.host.docker_unprivileged_user
    if user is None:
        raise SandboxUserNotYetCreated(
            "separate-user host config is missing docker_unprivileged_user"
        )
    try:
        return pwd.getpwnam(user)
    except KeyError as exc:
        raise SandboxUserNotYetCreated(
            f"sandbox user {user!r} does not exist yet (created by an earlier "
            f"phase L2); treat as MISSING"
        ) from exc


def probe_sandbox_pw_or_missing(
    host_config: HostConfig,
) -> pwd.struct_passwd | tuple[PhaseResult, str]:
    """Return the sandbox passwd entry, or a ``(MISSING, detail)`` probe pair.

    The shared content-aware-probe guard (design D10): every setup probe that
    needs the sandbox user / uid / home calls this and, when it does NOT get a
    :class:`pwd.struct_passwd` back, returns the ``(MISSING, detail)`` pair
    verbatim as its probe outcome. The not-yet-created user IS the ``MISSING``
    signal (a later phase creates it) — never a crash escaping through the
    unwrapped plan/apply passes.

    Call-sites discriminate with ``isinstance(result, pwd.struct_passwd)``
    (the positive case) — NOT ``isinstance(result, tuple)``: ``struct_passwd``
    is itself a ``tuple`` subclass, so a bare-``tuple`` check would
    mis-classify a real passwd entry.
    """
    try:
        return resolve_sandbox_pw(host_config)
    except SandboxUserNotYetCreated as exc:
        return PhaseResult.MISSING, str(exc)


def wait_user_manager_ready(user: str, *, attempts: int = 30) -> None:
    """Bounded root-side poll until ``user@<uid>.service`` is active.

    The per-user systemd manager (``user@<uid>.service``) is not instantly
    available after linger is enabled (L5) NOR after a rootless-docker
    enable/restart churns it (L6). A ``machinectl shell`` crossing into the user
    against a not-yet-ready manager connects and terminates with **empty
    stdout** — the executor's sentinel-not-found fail-closed then fires,
    diagnostically opaque. Observed at both L5 (post-enable-linger) and L6
    (post-dockerd-restart) on real hosts; mocks cannot reproduce it (F-014).

    This gate is a root-readable query (``systemctl is-active
    user@<uid>.service``) — NO boundary crossing — in a bounded shell-retry
    loop, so it must run BEFORE the phase's sandbox-user crossing. Raises
    :class:`~core.exceptions.SandboxExecutionError` (the ``Executor`` default
    ``check=True``) if the manager never becomes active within ``attempts``
    seconds. Single source for the gate the L5 and L6 acts both need.
    """
    uid = pwd.getpwnam(user).pw_uid
    Executor().run(
        [
            "/bin/bash",
            "-c",
            f"for i in $(seq 1 {attempts}); do "
            f"systemctl is-active user@{uid}.service >/dev/null 2>&1 "
            f"&& exit 0; sleep 1; done; "
            f"echo 'user@{uid}.service did not become active' >&2; exit 1",
        ],
    )


# ``PhaseResult`` values that mean "the phase converged / nothing to do" — a
# phase in one of these is NOT a mutation and does NOT block dependents.
_NON_MUTATING_RESULTS: frozenset[PhaseResult] = frozenset(
    {PhaseResult.ALREADY_CORRECT, PhaseResult.SKIPPED}
)


def run_plan_pass(
    phases: list[Phase], ctx: SetupContext, *, allow_external_deps: bool = False
) -> list[PhasePlanOutcome]:
    """Run every phase's probe in dependency order. NO mutations.

    This IS ``sandbox setup --dry-run`` (design D5): the only difference from
    the apply pass is that ``act`` is never invoked. The probe is content-aware
    (design D10) so the plan reflects state-vs-source, not mere file presence.

    ``allow_external_deps`` is forwarded to :func:`order_phases` for a subset
    run (``--update-runsc``); see that function's docstring.

    Returns one :class:`PhasePlanOutcome` per phase in dependency order.
    """
    ordered = order_phases(phases, allow_external_deps=allow_external_deps)
    mode = ctx.host_config.host.docker_execution_mode
    outcomes: list[PhasePlanOutcome] = []
    for phase in ordered:
        if mode not in phase.applies_in:
            # Mode-gated: the phase is not applicable in the active execution
            # mode. Report SKIPPED without running its probe; a mode-skip is
            # "not applicable", never a failure (it does not block dependents —
            # the apply pass treats it as dependency-satisfied).
            outcomes.append(
                PhasePlanOutcome(
                    phase.id, PhaseResult.SKIPPED, f"skipped ({mode.value})"
                )
            )
            continue
        try:
            result, detail = phase.probe(ctx)
        except Exception as exc:
            # A raising probe must NOT crash the plan pass (the unwrapped-probe
            # B1 class). The runner is the boundary that turns it into a typed
            # FAIL outcome; the pass continues to the next phase.
            outcomes.append(
                PhasePlanOutcome(
                    phase.id, PhaseResult.FAIL, f"probe raised: {exc}"
                )
            )
            continue
        outcomes.append(PhasePlanOutcome(phase.id, result, detail))
    return outcomes


def run_apply_pass(
    phases: list[Phase], ctx: SetupContext, *, allow_external_deps: bool = False
) -> list[PhaseApplyOutcome]:
    """Re-probe each phase; act + reverify the mutable ones (design D5).

    For each phase in dependency order:

    - if any transitive ``depends_on`` ancestor failed (or was itself
      ``BLOCKED_BY``), record ``BLOCKED_BY`` and skip — the apply pass
      *continues* with independent phases so operators see every reachable
      failure in one run;
    - re-probe; ``ALREADY_CORRECT`` / ``SKIPPED`` → record as-is, no mutation,
      does NOT block dependents;
    - ``CONFLICT`` → a clean refusal: the observed state is incompatible and
      the phase will not overwrite it (design D10; the spec's "refuse with
      diagnostic, do NOT overwrite"). The runner records ``CONFLICT`` and
      NEVER calls ``act`` / ``reverify`` — the "never overwrite operator data
      on conflict" guarantee is centralized here, not delegated to every phase
      author remembering to raise inside ``act``. ``rollback`` is NOT fired (a
      refusal mutated nothing; rollback is only for a ``FAIL`` of a phase
      carrying a rollback callable). A ``CONFLICT`` phase marks its transitive
      dependents ``BLOCKED_BY`` (a refused prerequisite blocks its subtree);
    - otherwise ``act`` then ``reverify``. A raising ``act``, a raising
      ``reverify``, or a ``reverify`` that returns ``False`` is a ``FAIL``.

    On a ``FAIL`` for a phase that carries a ``rollback`` callable, the
    rollback is fired (the L3a sudoers-probe case, design D1) — other phases
    are never rolled back. A failed phase marks its transitive dependents
    ``BLOCKED_BY``.

    ``allow_external_deps`` is forwarded to :func:`order_phases` for a subset
    run (``--update-runsc``); see that function's docstring. External deps are
    never in ``failed_ids``, so they correctly never mark a subset phase
    ``BLOCKED_BY``.

    Returns one :class:`PhaseApplyOutcome` per phase in dependency order.
    """
    ordered = order_phases(phases, allow_external_deps=allow_external_deps)
    mode = ctx.host_config.host.docker_execution_mode
    outcomes: list[PhaseApplyOutcome] = []
    failed_ids: set[str] = set()

    for phase in ordered:
        if mode not in phase.applies_in:
            # Mode-gated: not applicable in the active execution mode. Emit
            # SKIPPED and continue WITHOUT adding the phase to ``failed_ids`` —
            # a mode-skip is dependency-satisfied, NOT a failure, so a phase
            # depending on a mode-skipped phase still runs (it is never marked
            # BLOCKED_BY). Surfaced identically to the plan pass.
            outcomes.append(
                PhaseApplyOutcome(
                    phase.id,
                    PhaseResult.SKIPPED,
                    f"skipped ({mode.value})",
                    reverified=False,
                )
            )
            continue

        blocker = next(
            (dep for dep in phase.depends_on if dep in failed_ids), None
        )
        if blocker is not None:
            failed_ids.add(phase.id)
            outcomes.append(
                PhaseApplyOutcome(
                    phase.id,
                    PhaseResult.BLOCKED_BY,
                    f"blocked by failed phase {blocker!r}",
                    reverified=False,
                )
            )
            continue

        try:
            result, detail = phase.probe(ctx)
        except Exception as exc:
            # A raising probe is a FAIL (the unwrapped-probe B1 class), mirrored
            # on _apply_one's act/reverify handling: record FAIL, mark the
            # phase failed so its transitive dependents are BLOCKED_BY, and
            # continue the pass. ``rollback`` is NOT fired — a probe mutates
            # nothing, so there is nothing to undo (same as the CONFLICT path).
            failed_ids.add(phase.id)
            outcomes.append(
                PhaseApplyOutcome(
                    phase.id,
                    PhaseResult.FAIL,
                    f"probe raised: {exc}",
                    reverified=False,
                )
            )
            continue
        if result in _NON_MUTATING_RESULTS:
            outcomes.append(
                PhaseApplyOutcome(phase.id, result, detail, reverified=False)
            )
            continue

        if result == PhaseResult.CONFLICT:
            # A clean refusal: the phase will not overwrite an incompatible
            # observed state. Never act / reverify / rollback; record the
            # refusal and block the dependent subtree.
            failed_ids.add(phase.id)
            outcomes.append(
                PhaseApplyOutcome(
                    phase.id, PhaseResult.CONFLICT, detail, reverified=False
                )
            )
            continue

        outcome = _apply_one(phase, ctx)
        if outcome.result == PhaseResult.FAIL:
            failed_ids.add(phase.id)
        outcomes.append(outcome)

    return outcomes


def _apply_one(phase: Phase, ctx: SetupContext) -> PhaseApplyOutcome:
    """Act + reverify a single mutable phase; fire rollback on failure.

    Extracted so the failure/rollback ceremony lives in one place (design D1:
    only a phase with a ``rollback`` callable is rolled back; the rollback's
    own failure is surfaced in the detail, never swallowed).
    """
    try:
        act_detail = phase.act(ctx)
        reverified = phase.reverify(ctx)
    except Exception as exc:
        # Any exception from act or reverify classifies the phase as FAIL;
        # the runner is the boundary that turns it into a typed outcome.
        return _failed(phase, ctx, f"act/reverify raised: {exc}")

    if reverified:
        return PhaseApplyOutcome(
            phase.id, PhaseResult.ALREADY_CORRECT, act_detail, reverified=True
        )
    return _failed(phase, ctx, "reverify did not confirm convergence")


def _failed(
    phase: Phase, ctx: SetupContext, detail: str
) -> PhaseApplyOutcome:
    """Build a ``FAIL`` outcome, firing ``phase.rollback`` if one is present."""
    if phase.rollback is None:
        return PhaseApplyOutcome(
            phase.id, PhaseResult.FAIL, detail, reverified=False
        )
    try:
        phase.rollback(ctx)
        rollback_note = "rolled back"
    except Exception as exc:
        # A failing rollback must be surfaced in the detail, never swallowed.
        rollback_note = f"rollback also failed: {exc}"
    return PhaseApplyOutcome(
        phase.id,
        PhaseResult.FAIL,
        f"{detail} ({rollback_note})",
        reverified=False,
    )


__all__ = [
    "ActFn",
    "Identity",
    "Phase",
    "PhaseApplyOutcome",
    "PhaseDependencyError",
    "PhaseDiscoveryError",
    "PhasePlanOutcome",
    "PhaseResult",
    "ProbeFn",
    "ReverifyFn",
    "RollbackFn",
    "SandboxUserNotYetCreated",
    "SetupContext",
    "daemon_owner_crossing",
    "daemon_owner_user",
    "discover_phases",
    "order_phases",
    "probe_sandbox_pw_or_missing",
    "resolve_sandbox_pw",
    "route",
    "run_apply_pass",
    "run_plan_pass",
    "wait_user_manager_ready",
]
