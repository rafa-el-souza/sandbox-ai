"""Plan/apply two-pass rendering + gating logic for ``sandbox setup``.

This module owns the *pure* parts of the ``sandbox setup`` UX so the Typer
command in :mod:`cli.main` stays a thin I/O shell (root check, operator
resolution, SIGINT handler, prompt/exit-code wiring). Everything here is a
pure function of its inputs — no stdin, no signals, no ``typer`` — which is
what makes the full edge-case matrix (design D5 / spec "Plan/Apply Two-Pass
UX") unit-testable without a real ceremony.

Split of concerns:

- :func:`build_phase_list` — base ``discover_phases()`` (dependency-ordered)
  followed by the sticky-opt-in extra phases (design D11).
- :func:`render_plan` / :func:`render_apply` — doctor-style per-phase glyph
  lines (``✓ / ⊙ / ⚠ / ✗``) + the spec-exact ``Summary:`` line.
- :func:`decide_gate` — the pure gating decision over the plan outcomes +
  TTY/``--yes`` state. Returns a typed :class:`GateDecision`; the CLI maps
  that to a prompt / refusal / proceed + exit code.
- :func:`summarize_apply` — the apply-pass finalization summary + remediation
  pointers for FAIL / BLOCKED-BY phases.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from core.setup.extras import EXTRAS
from core.setup.phase_runner import (
    Phase,
    PhaseResult,
    discover_phases,
    order_phases,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from core.setup.phase_runner import PhaseApplyOutcome, PhasePlanOutcome

# ── Phase-list composition ───────────────────────────────────────────────────


def build_phase_list(selected_extra_names: Sequence[str]) -> list[Phase]:
    """Compose the invocation's phase list: base ceremony + active extras.

    The base is :func:`core.setup.phase_runner.discover_phases` ordered by its
    ``depends_on`` graph (L0..L8). Each selected extra (already filtered by the
    sticky-opt-in predicate in :mod:`core.setup.extras`) contributes its lazily
    loaded ``Phase`` *after* L8 — the extra modules set
    ``depends_on = ("l8",)`` so :func:`order_phases` keeps them last. Unknown
    extra names are ignored (the caller passes registry keys).
    """
    base = order_phases(discover_phases())
    extra_phases: list[Phase] = []
    for name in selected_extra_names:
        spec = EXTRAS.get(name)
        if spec is None:
            continue
        extra_phases.append(spec.load_phase())
    return base + extra_phases


# ── Glyph mapping (doctor-style, spec "Plan/Apply Two-Pass UX") ───────────────

# Plan-pass severity markers, spec-exact:
#   ✓ already correct   ⊙ missing → will mutate
#   ⚠ blocked → reason  ✗ verify-only failure → refuse
_PLAN_GLYPH: dict[PhaseResult, str] = {
    PhaseResult.ALREADY_CORRECT: "✓",
    PhaseResult.SKIPPED: "✓",
    PhaseResult.MISSING: "⊙",
    PhaseResult.DRIFT: "⊙",
    PhaseResult.BLOCKED_BY: "⚠",
    PhaseResult.CONFLICT: "✗",
    PhaseResult.FAIL: "✗",
}

# A plan result counts as a "mutation pending" iff its act would change the
# host. ``CONFLICT`` is a refusal, not a mutation; ``BLOCKED_BY`` cannot occur
# in the plan pass (no phase failed yet) but is mapped defensively.
_PLAN_MUTATION_RESULTS = frozenset({PhaseResult.MISSING, PhaseResult.DRIFT})
_PLAN_CORRECT_RESULTS = frozenset(
    {PhaseResult.ALREADY_CORRECT, PhaseResult.SKIPPED}
)
# A plan result that unconditionally blocks the apply pass (a refusal).
_PLAN_REFUSAL_RESULTS = frozenset({PhaseResult.CONFLICT, PhaseResult.FAIL})


@dataclass(frozen=True)
class PlanTally:
    """The four spec-summary counts over a plan pass (every phase once)."""

    already_correct: int
    will_mutate: int
    blocked: int
    refused: int

    @property
    def total(self) -> int:
        """A + M + B + R — equals the invocation's total phase count."""
        return (
            self.already_correct
            + self.will_mutate
            + self.blocked
            + self.refused
        )


def tally_plan(outcomes: Sequence[PhasePlanOutcome]) -> PlanTally:
    """Bucket every plan outcome into the four spec-summary categories."""
    correct = mutate = blocked = refused = 0
    for o in outcomes:
        if o.result in _PLAN_CORRECT_RESULTS:
            correct += 1
        elif o.result in _PLAN_MUTATION_RESULTS:
            mutate += 1
        elif o.result in _PLAN_REFUSAL_RESULTS:
            refused += 1
        else:  # PhaseResult.BLOCKED_BY — defensive (not reachable in plan).
            blocked += 1
    return PlanTally(correct, mutate, blocked, refused)


def plan_summary_line(tally: PlanTally) -> str:
    """The spec-exact plan ``Summary:`` line.

    Format (spec "Plan summary line format"): ``Summary: <A> already
    correct, <M> will mutate, <B> blocked, <R> refused`` where A+M+B+R is the
    total phase count (including any sticky-opt-in integration phases).
    """
    return (
        f"Summary: {tally.already_correct} already correct, "
        f"{tally.will_mutate} will mutate, "
        f"{tally.blocked} blocked, {tally.refused} refused"
    )


def render_plan(
    phases: Sequence[Phase], outcomes: Sequence[PhasePlanOutcome]
) -> list[str]:
    """Render the doctor-style plan output lines (NOT including the summary).

    One ``  <glyph> <phase-name> — <detail>`` line per phase, in the
    dependency order the outcomes were produced. The summary line is appended
    separately by the caller so a refusal/prompt can be interleaved between
    them per the spec ordering ("final line before the prompt").
    """
    name_by_id = {p.id: p.name for p in phases}
    lines: list[str] = []
    for o in outcomes:
        glyph = _PLAN_GLYPH.get(o.result, "✗")
        name = name_by_id.get(o.phase_id, o.phase_id)
        lines.append(f"  {glyph} {name} — {o.detail}")
    return lines


def refusal_lines(
    phases: Sequence[Phase], outcomes: Sequence[PhasePlanOutcome]
) -> list[str]:
    """Enumerate each refused phase's remediation hint (spec ≥1-refusal path).

    A plan ``CONFLICT`` (the content-aware-probe refusal) or ``FAIL`` is a
    verify-only failure: the apply pass is unconditionally blocked. The detail
    text carries the per-phase remediation the phase author wrote.
    """
    name_by_id = {p.id: p.name for p in phases}
    out: list[str] = []
    for o in outcomes:
        if o.result in _PLAN_REFUSAL_RESULTS:
            name = name_by_id.get(o.phase_id, o.phase_id)
            out.append(f"  ✗ {name}: {o.detail}")
    return out


# ── Gating decision (pure; spec edge-case matrix) ────────────────────────────


class GateOutcome(StrEnum):
    """The pure gating decision over a plan pass + TTY/``--yes`` state.

    - ``NOTHING_TO_APPLY`` — every phase already correct (zero mutations, zero
      refusals): no prompt, ``Nothing to apply. Setup is complete.``, exit 0.
    - ``REFUSED`` — ≥1 refusal (CONFLICT/verify-only fail): no prompt, list
      the refusals, ``Setup will not enter the apply pass.``, exit non-zero;
      apply pass never runs (refusals beat pending mutations unconditionally).
    - ``NON_TTY_NEEDS_YES`` — ≥1 mutation, no refusals, non-TTY, no ``--yes``:
      refuse with ``non-interactive context requires --yes flag to apply
      mutations``, exit non-zero.
    - ``PROMPT`` — ≥1 mutation, no refusals, TTY, no ``--yes``: emit the
      ``Proceed with apply? [y/N]: `` prompt and read one stdin line.
    - ``PROCEED`` — ≥1 mutation, no refusals, ``--yes`` (TTY or not): skip the
      prompt, run the apply pass directly.
    """

    NOTHING_TO_APPLY = "nothing-to-apply"
    REFUSED = "refused"
    NON_TTY_NEEDS_YES = "non-tty-needs-yes"
    PROMPT = "prompt"
    PROCEED = "proceed"


@dataclass(frozen=True)
class GateDecision:
    """The :func:`decide_gate` result: outcome + the plan tally it derived."""

    outcome: GateOutcome
    tally: PlanTally


def decide_gate(
    outcomes: Sequence[PhasePlanOutcome],
    *,
    is_tty: bool,
    assume_yes: bool,
    extra_mutations: int = 0,
) -> GateDecision:
    """Pure gating decision — the full spec edge-case matrix in one place.

    Precedence (spec "Plan/Apply Two-Pass UX"):

    1. ANY refusal → ``REFUSED`` (refusals block the apply pass
       unconditionally, even with pending mutations or ``--yes``).
    2. else zero mutations → ``NOTHING_TO_APPLY`` (idempotent converged
       re-run; no prompt).
    3. else (≥1 mutation, no refusals):
       - ``--yes`` → ``PROCEED`` (TTY or non-TTY; skip prompt).
       - non-TTY, no ``--yes`` → ``NON_TTY_NEEDS_YES``.
       - TTY, no ``--yes`` → ``PROMPT``.

    ``extra_mutations`` counts pending mutations the plan outcomes do NOT carry —
    operator-rootless setup's host-root escalation batch (classified separately
    from the phase plan). It adds to the mutation count so a converged phase plan
    with a non-empty host-root batch still prompts/proceeds rather than reporting
    ``NOTHING_TO_APPLY``. Separate-user callers leave it 0 (behavior unchanged).
    """
    tally = tally_plan(outcomes)
    if tally.refused > 0:
        return GateDecision(GateOutcome.REFUSED, tally)
    if tally.will_mutate + extra_mutations == 0:
        return GateDecision(GateOutcome.NOTHING_TO_APPLY, tally)
    if assume_yes:
        return GateDecision(GateOutcome.PROCEED, tally)
    if not is_tty:
        return GateDecision(GateOutcome.NON_TTY_NEEDS_YES, tally)
    return GateDecision(GateOutcome.PROMPT, tally)


# Accepted affirmative prompt responses (case-insensitive), spec-exact.
_AFFIRMATIVE = frozenset({"y", "yes"})


def prompt_response_proceeds(raw: str) -> bool:
    """``True`` iff ``raw`` is an affirmative confirm-prompt response.

    Only ``y`` / ``Y`` / ``yes`` / ``YES`` (case-insensitive, surrounding
    whitespace stripped) proceed; Enter (empty) or anything else aborts
    (default-N, spec "Plan/Apply Two-Pass UX").
    """
    return raw.strip().lower() in _AFFIRMATIVE


# ── Apply-pass finalization summary (spec task 8.5) ──────────────────────────

_APPLY_GLYPH: dict[PhaseResult, str] = {
    PhaseResult.ALREADY_CORRECT: "OK",
    PhaseResult.SKIPPED: "SKIP",
    PhaseResult.CONFLICT: "REFUSED",
    PhaseResult.FAIL: "FAIL",
    PhaseResult.BLOCKED_BY: "BLOCKED-BY",
}


@dataclass(frozen=True)
class ApplyTally:
    """The apply-pass finalization counts (spec task 8.5 summary variant)."""

    already_correct: int
    applied: int
    blocked: int
    refused: int
    failed: int

    @property
    def total(self) -> int:
        """A + applied + B + R + F — the invocation's total phase count."""
        return (
            self.already_correct
            + self.applied
            + self.blocked
            + self.refused
            + self.failed
        )


def tally_apply(outcomes: Sequence[PhaseApplyOutcome]) -> ApplyTally:
    """Bucket apply outcomes into the finalization-summary categories.

    A phase that *acted and reverified* is ``applied``; a phase the apply pass
    found already-correct (no mutation) is ``already_correct``; ``BLOCKED_BY``
    is blocked; ``CONFLICT`` is refused; ``FAIL`` is failed.
    """
    correct = applied = blocked = refused = failed = 0
    for o in outcomes:
        if o.result == PhaseResult.FAIL:
            failed += 1
        elif o.result == PhaseResult.BLOCKED_BY:
            blocked += 1
        elif o.result == PhaseResult.CONFLICT:
            refused += 1
        elif o.result == PhaseResult.SKIPPED:
            correct += 1
        elif o.reverified:
            applied += 1
        else:  # ALREADY_CORRECT, did not act.
            correct += 1
    return ApplyTally(correct, applied, blocked, refused, failed)


def apply_summary_line(tally: ApplyTally) -> str:
    """Apply-pass finalization summary (spec task 8.5 — same format, swapping
    ``will mutate`` for ``applied`` and adding ``<F> failed``)."""
    return (
        f"Summary: {tally.already_correct} already correct, "
        f"{tally.applied} applied, {tally.blocked} blocked, "
        f"{tally.refused} refused, {tally.failed} failed"
    )


def render_apply(
    phases: Sequence[Phase], outcomes: Sequence[PhaseApplyOutcome]
) -> list[str]:
    """Render per-phase apply progress lines (``OK`` / ``FAIL`` / ``SKIP`` …)."""
    name_by_id = {p.id: p.name for p in phases}
    lines: list[str] = []
    for o in outcomes:
        label = _APPLY_GLYPH.get(o.result, "OK")
        name = name_by_id.get(o.phase_id, o.phase_id)
        lines.append(f"  [{label}] {name} — {o.detail}")
    return lines


def apply_remediation_lines(
    phases: Sequence[Phase], outcomes: Sequence[PhaseApplyOutcome]
) -> list[str]:
    """Remediation pointers for every FAIL / BLOCKED-BY phase (spec 8.5)."""
    name_by_id = {p.id: p.name for p in phases}
    out: list[str] = []
    for o in outcomes:
        if o.result in (PhaseResult.FAIL, PhaseResult.BLOCKED_BY):
            name = name_by_id.get(o.phase_id, o.phase_id)
            out.append(
                f"  ✗ {name} ({o.result.value}): {o.detail}"
            )
    return out


def apply_pass_failed(outcomes: Sequence[PhaseApplyOutcome]) -> bool:
    """``True`` iff any phase failed / was blocked / refused in the apply pass.

    Drives the command's non-zero exit (spec "Apply pass continues past
    non-rollback failures": final exit code is non-zero when any phase did not
    converge).
    """
    return any(
        o.result
        in (PhaseResult.FAIL, PhaseResult.BLOCKED_BY, PhaseResult.CONFLICT)
        for o in outcomes
    )


__all__ = [
    "ApplyTally",
    "GateDecision",
    "GateOutcome",
    "PlanTally",
    "apply_pass_failed",
    "apply_remediation_lines",
    "apply_summary_line",
    "build_phase_list",
    "decide_gate",
    "plan_summary_line",
    "prompt_response_proceeds",
    "refusal_lines",
    "render_apply",
    "render_plan",
    "summarize_apply",
    "tally_apply",
    "tally_plan",
]


def summarize_apply(
    phases: Sequence[Phase], outcomes: Sequence[PhaseApplyOutcome]
) -> list[str]:
    """Compose the full apply finalization block: progress + remediation +
    summary line. Returned as ordered lines for the caller to print."""
    lines = render_apply(phases, outcomes)
    remediation = apply_remediation_lines(phases, outcomes)
    if remediation:
        lines.append("")
        lines.append("Remediation:")
        lines.extend(remediation)
    lines.append("")
    lines.append(apply_summary_line(tally_apply(outcomes)))
    return lines
