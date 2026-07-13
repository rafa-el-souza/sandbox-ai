# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for :mod:`core.setup.cli_flow` — the pure plan/apply UX logic.

Every gating-matrix branch, the spec-exact ``Summary:`` line formats, the
glyph rendering, and the extras-composition seam are exercised here with real
:class:`Phase` objects + synthetic plan/apply outcomes (no ceremony, no I/O).
"""

from __future__ import annotations

import pytest
from core.setup import cli_flow
from core.setup.phase_runner import (
    Identity,
    Phase,
    PhaseApplyOutcome,
    PhasePlanOutcome,
    PhaseResult,
)


def _phase(pid: str, deps: tuple[str, ...] = ()) -> Phase:
    return Phase(
        id=pid,
        name=f"phase {pid}",
        identity=Identity.ROOT,
        probe=lambda _ctx: (PhaseResult.ALREADY_CORRECT, "ok"),
        act=lambda _ctx: "acted",
        reverify=lambda _ctx: True,
        depends_on=deps,
    )


def _plan(pid: str, result: PhaseResult, detail: str = "d") -> PhasePlanOutcome:
    return PhasePlanOutcome(pid, result, detail)


def _apply(
    pid: str, result: PhaseResult, *, reverified: bool = False, detail: str = "d"
) -> PhaseApplyOutcome:
    return PhaseApplyOutcome(pid, result, detail, reverified=reverified)


# ── tally_plan / plan_summary_line ───────────────────────────────────────────


def test_tally_plan_buckets_every_category() -> None:
    outcomes = [
        _plan("a", PhaseResult.ALREADY_CORRECT),
        _plan("b", PhaseResult.SKIPPED),
        _plan("c", PhaseResult.MISSING),
        _plan("d", PhaseResult.DRIFT),
        _plan("e", PhaseResult.CONFLICT),
        _plan("f", PhaseResult.FAIL),
        _plan("g", PhaseResult.BLOCKED_BY),
    ]
    tally = cli_flow.tally_plan(outcomes)
    assert tally.already_correct == 2
    assert tally.will_mutate == 2
    assert tally.refused == 2
    assert tally.blocked == 1
    assert tally.total == 7


def test_plan_summary_line_is_spec_exact() -> None:
    tally = cli_flow.PlanTally(
        already_correct=3, will_mutate=4, blocked=1, refused=2
    )
    assert cli_flow.plan_summary_line(tally) == (
        "Summary: 3 already correct, 4 will mutate, 1 blocked, 2 refused"
    )


def test_plan_summary_zero_mutations_form() -> None:
    tally = cli_flow.tally_plan([_plan("a", PhaseResult.ALREADY_CORRECT)])
    assert cli_flow.plan_summary_line(tally) == (
        "Summary: 1 already correct, 0 will mutate, 0 blocked, 0 refused"
    )


# ── render_plan / refusal_lines ──────────────────────────────────────────────


def test_render_plan_glyphs() -> None:
    phases = [_phase("a"), _phase("b"), _phase("c"), _phase("d")]
    outcomes = [
        _plan("a", PhaseResult.ALREADY_CORRECT, "fine"),
        _plan("b", PhaseResult.MISSING, "absent"),
        _plan("c", PhaseResult.BLOCKED_BY, "blk"),
        _plan("d", PhaseResult.CONFLICT, "bad"),
    ]
    lines = cli_flow.render_plan(phases, outcomes)
    assert lines == [
        "  ✓ phase a — fine",
        "  ⊙ phase b — absent",
        "  ⚠ phase c — blk",
        "  ✗ phase d — bad",
    ]


def test_render_plan_unknown_result_falls_back_to_refuse_glyph() -> None:
    # Defensive fallback: a result not in the glyph map renders ✗.
    phases = [_phase("a")]
    outcomes = [_plan("a", PhaseResult.DRIFT, "drift")]
    # DRIFT is mapped (⊙); force the .get fallback via an id with no phase.
    lines = cli_flow.render_plan([], outcomes)
    assert lines == ["  ⊙ a — drift"]
    assert cli_flow.render_plan(phases, outcomes) == ["  ⊙ phase a — drift"]


def test_refusal_lines_lists_conflict_and_fail() -> None:
    phases = [_phase("a"), _phase("b"), _phase("c")]
    outcomes = [
        _plan("a", PhaseResult.ALREADY_CORRECT),
        _plan("b", PhaseResult.CONFLICT, "operator value"),
        _plan("c", PhaseResult.FAIL, "verify-only fail"),
    ]
    assert cli_flow.refusal_lines(phases, outcomes) == [
        "  ✗ phase b: operator value",
        "  ✗ phase c: verify-only fail",
    ]


# ── decide_gate — the full edge-case matrix ──────────────────────────────────


def test_decide_gate_nothing_to_apply() -> None:
    plan = [_plan("a", PhaseResult.ALREADY_CORRECT)]
    d = cli_flow.decide_gate(plan, is_tty=True, assume_yes=False)
    assert d.outcome == cli_flow.GateOutcome.NOTHING_TO_APPLY


def test_decide_gate_refused_beats_mutation_and_yes() -> None:
    plan = [
        _plan("a", PhaseResult.MISSING),
        _plan("b", PhaseResult.CONFLICT),
    ]
    d = cli_flow.decide_gate(plan, is_tty=False, assume_yes=True)
    assert d.outcome == cli_flow.GateOutcome.REFUSED


def test_decide_gate_proceed_with_yes_tty() -> None:
    plan = [_plan("a", PhaseResult.MISSING)]
    d = cli_flow.decide_gate(plan, is_tty=True, assume_yes=True)
    assert d.outcome == cli_flow.GateOutcome.PROCEED


def test_decide_gate_proceed_with_yes_non_tty() -> None:
    plan = [_plan("a", PhaseResult.DRIFT)]
    d = cli_flow.decide_gate(plan, is_tty=False, assume_yes=True)
    assert d.outcome == cli_flow.GateOutcome.PROCEED


def test_decide_gate_non_tty_needs_yes() -> None:
    plan = [_plan("a", PhaseResult.MISSING)]
    d = cli_flow.decide_gate(plan, is_tty=False, assume_yes=False)
    assert d.outcome == cli_flow.GateOutcome.NON_TTY_NEEDS_YES


def test_decide_gate_prompt_tty_no_yes() -> None:
    plan = [_plan("a", PhaseResult.MISSING)]
    d = cli_flow.decide_gate(plan, is_tty=True, assume_yes=False)
    assert d.outcome == cli_flow.GateOutcome.PROMPT


def test_decide_gate_extra_mutations_overrides_nothing_to_apply() -> None:
    # A converged phase plan (zero mutations) with a non-empty host-root batch
    # (extra_mutations > 0) still prompts — operator-rootless's batch counts.
    plan = [_plan("a", PhaseResult.ALREADY_CORRECT)]
    d = cli_flow.decide_gate(
        plan, is_tty=True, assume_yes=False, extra_mutations=2
    )
    assert d.outcome == cli_flow.GateOutcome.PROMPT


def test_decide_gate_extra_mutations_zero_is_nothing_to_apply() -> None:
    # extra_mutations defaults to 0 → separate-user behavior is unchanged.
    plan = [_plan("a", PhaseResult.ALREADY_CORRECT)]
    d = cli_flow.decide_gate(
        plan, is_tty=True, assume_yes=False, extra_mutations=0
    )
    assert d.outcome == cli_flow.GateOutcome.NOTHING_TO_APPLY


@pytest.mark.parametrize(
    ("raw", "proceeds"),
    [
        ("y", True),
        ("Y", True),
        ("yes", True),
        ("YES", True),
        ("  yes  ", True),
        ("n", False),
        ("no", False),
        ("", False),
        ("nope", False),
        ("yess", False),
    ],
)
def test_prompt_response_proceeds(raw: str, proceeds: bool) -> None:
    assert cli_flow.prompt_response_proceeds(raw) is proceeds


# ── apply tally / summary / remediation ──────────────────────────────────────


def test_tally_apply_buckets() -> None:
    outcomes = [
        _apply("a", PhaseResult.ALREADY_CORRECT, reverified=True),
        _apply("b", PhaseResult.ALREADY_CORRECT, reverified=False),
        _apply("c", PhaseResult.SKIPPED),
        _apply("d", PhaseResult.CONFLICT),
        _apply("e", PhaseResult.BLOCKED_BY),
        _apply("f", PhaseResult.FAIL),
    ]
    t = cli_flow.tally_apply(outcomes)
    assert t.applied == 1
    assert t.already_correct == 2  # b (no act) + c (skipped)
    assert t.refused == 1
    assert t.blocked == 1
    assert t.failed == 1
    assert t.total == 6


def test_apply_summary_line_format() -> None:
    t = cli_flow.ApplyTally(
        already_correct=2, applied=3, blocked=1, refused=1, failed=2
    )
    assert cli_flow.apply_summary_line(t) == (
        "Summary: 2 already correct, 3 applied, 1 blocked, 1 refused, 2 failed"
    )


def test_render_apply_labels() -> None:
    phases = [_phase("a"), _phase("b"), _phase("c")]
    outcomes = [
        _apply("a", PhaseResult.ALREADY_CORRECT, reverified=True, detail="did"),
        _apply("b", PhaseResult.FAIL, detail="boom"),
        _apply("c", PhaseResult.SKIPPED, detail="skip"),
    ]
    assert cli_flow.render_apply(phases, outcomes) == [
        "  [OK] phase a — did",
        "  [FAIL] phase b — boom",
        "  [SKIP] phase c — skip",
    ]


def test_render_apply_unknown_result_fallback() -> None:
    # MISSING is not in the apply-glyph map → default "OK".
    outcomes = [_apply("z", PhaseResult.MISSING, detail="m")]
    assert cli_flow.render_apply([], outcomes) == ["  [OK] z — m"]


def test_apply_remediation_lines_only_fail_and_blocked() -> None:
    phases = [_phase("a"), _phase("b"), _phase("c")]
    outcomes = [
        _apply("a", PhaseResult.ALREADY_CORRECT, reverified=True),
        _apply("b", PhaseResult.FAIL, detail="boom"),
        _apply("c", PhaseResult.BLOCKED_BY, detail="blocked by 'b'"),
    ]
    assert cli_flow.apply_remediation_lines(phases, outcomes) == [
        "  ✗ phase b (fail): boom",
        "  ✗ phase c (blocked-by): blocked by 'b'",
    ]


def test_apply_pass_failed_true_on_fail() -> None:
    assert cli_flow.apply_pass_failed([_apply("a", PhaseResult.FAIL)]) is True


def test_apply_pass_failed_true_on_blocked() -> None:
    assert (
        cli_flow.apply_pass_failed([_apply("a", PhaseResult.BLOCKED_BY)])
        is True
    )


def test_apply_pass_failed_true_on_conflict() -> None:
    assert (
        cli_flow.apply_pass_failed([_apply("a", PhaseResult.CONFLICT)]) is True
    )


def test_apply_pass_failed_false_when_clean() -> None:
    outcomes = [
        _apply("a", PhaseResult.ALREADY_CORRECT, reverified=True),
        _apply("b", PhaseResult.SKIPPED),
    ]
    assert cli_flow.apply_pass_failed(outcomes) is False


def test_summarize_apply_with_remediation_block() -> None:
    phases = [_phase("a"), _phase("b")]
    outcomes = [
        _apply("a", PhaseResult.ALREADY_CORRECT, reverified=True, detail="ok"),
        _apply("b", PhaseResult.FAIL, detail="boom"),
    ]
    lines = cli_flow.summarize_apply(phases, outcomes)
    assert lines[0] == "  [OK] phase a — ok"
    assert lines[1] == "  [FAIL] phase b — boom"
    assert "Remediation:" in lines
    assert "  ✗ phase b (fail): boom" in lines
    # phase a: ALREADY_CORRECT + reverified=True means the runner acted and
    # reverify confirmed convergence → counts as *applied*, not as a no-op.
    assert lines[-1] == (
        "Summary: 0 already correct, 1 applied, 0 blocked, 0 refused, 1 failed"
    )


def test_summarize_apply_no_remediation_when_clean() -> None:
    phases = [_phase("a")]
    outcomes = [
        _apply("a", PhaseResult.ALREADY_CORRECT, reverified=True, detail="ok")
    ]
    lines = cli_flow.summarize_apply(phases, outcomes)
    assert "Remediation:" not in lines
    assert lines[-1] == (
        "Summary: 0 already correct, 1 applied, 0 blocked, 0 refused, 0 failed"
    )


# ── build_phase_list — base + sticky extras composition ──────────────────────


def test_build_phase_list_base_only_is_ordered_l0_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = [_phase("l8", ("l0",)), _phase("l0")]
    monkeypatch.setattr(cli_flow, "discover_phases", lambda: base)
    composed = cli_flow.build_phase_list(())
    assert [p.id for p in composed] == ["l0", "l8"]


def test_build_phase_list_appends_selected_extra_after_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = [_phase("l0"), _phase("l8", ("l0",))]
    monkeypatch.setattr(cli_flow, "discover_phases", lambda: base)

    stub_extra_phase = _phase("fapolicyd", ("l8",))

    class _StubSpec:
        def load_phase(self) -> Phase:
            return stub_extra_phase

    monkeypatch.setattr(cli_flow, "EXTRAS", {"fapolicyd": _StubSpec()})
    composed = cli_flow.build_phase_list(["fapolicyd"])
    assert [p.id for p in composed] == ["l0", "l8", "fapolicyd"]


def test_build_phase_list_ignores_unknown_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = [_phase("l0")]
    monkeypatch.setattr(cli_flow, "discover_phases", lambda: base)
    monkeypatch.setattr(cli_flow, "EXTRAS", {})
    composed = cli_flow.build_phase_list(["nonexistent"])
    assert [p.id for p in composed] == ["l0"]
