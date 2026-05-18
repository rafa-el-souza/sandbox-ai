"""Unit tests for the ``sandbox setup`` Typer command (Group 8).

Covers the full apply-gating edge-case matrix from tasks.md 8.3 with the
spec-exact literal strings, the non-root refusal, operator-resolution error
surfacing, ``--update-runsc`` (L6a-only, force=True), the SIGINT→130 path, the
sticky-opt-in extras inclusion, and ``--yes`` skipping both prompts. Subprocess
/ filesystem / stdin / signals are all mocked — the real ceremony never runs.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from cli.main import _SetupAborted, app
from core.setup.l0_identity import OperatorResolutionError
from core.setup.phase_runner import (
    Identity,
    Phase,
    PhaseApplyOutcome,
    PhasePlanOutcome,
    PhaseResult,
    SetupContext,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from typer.testing import CliRunner


def _phase(pid: str) -> Phase:
    return Phase(
        id=pid,
        name=f"phase {pid}",
        identity=Identity.ROOT,
        probe=lambda _ctx: (PhaseResult.MISSING, "absent"),
        act=lambda _ctx: "acted",
        reverify=lambda _ctx: True,
    )


def _plan(pid: str, result: PhaseResult) -> PhasePlanOutcome:
    return PhasePlanOutcome(pid, result, f"{pid}-detail")


def _apply(
    pid: str, result: PhaseResult, *, reverified: bool = True
) -> PhaseApplyOutcome:
    return PhaseApplyOutcome(pid, result, f"{pid}-detail", reverified=reverified)


# ── non-root refusal ─────────────────────────────────────────────────────────


def test_refuses_non_root(runner: CliRunner) -> None:
    with patch("cli.main.os.geteuid", return_value=1000):
        result = runner.invoke(app, ["setup"])
    assert result.exit_code == 1
    assert (
        "sandbox setup must be run as root. Re-invoke as: sudo sandbox setup"
        in result.output
    )


# ── operator resolution error surfacing ──────────────────────────────────────


def test_operator_resolution_error_surfaced(runner: CliRunner) -> None:
    with (
        patch("cli.main.os.geteuid", return_value=0),
        patch(
            "cli.main.resolve_operator",
            side_effect=OperatorResolutionError("cannot resolve operator user."),
        ),
    ):
        result = runner.invoke(app, ["setup"])
    assert result.exit_code == 1
    assert "cannot resolve operator user." in result.output


# ── helper: a root + resolved-operator context for the happy paths ──────────


@contextlib.contextmanager
def _root_ctx() -> Iterator[None]:
    """Patch the root check + operator + distro gate + (empty) extras."""
    with (
        patch("cli.main.os.geteuid", return_value=0),
        patch("cli.main.resolve_operator", return_value="dev"),
        patch("cli.main.emit_distro_gate"),
        patch("cli.main.selected_extras", return_value=[]),
    ):
        yield


# ── zero-mutations: no prompt, exit 0 ────────────────────────────────────────


def test_zero_mutations_no_prompt_exit_0(runner: CliRunner) -> None:
    phases = [_phase("l0")]
    plan = [_plan("l0", PhaseResult.ALREADY_CORRECT)]
    with (
        _root_ctx(),
        patch("cli.main.cli_flow.build_phase_list", return_value=phases),
        patch("cli.main.run_plan_pass", return_value=plan),
        patch("cli.main._stdin_is_tty", return_value=True),
        patch("cli.main.run_apply_pass") as apply_mock,
    ):
        result = runner.invoke(app, ["setup"])
    assert result.exit_code == 0
    assert "Nothing to apply. Setup is complete." in result.output
    assert (
        "Summary: 1 already correct, 0 will mutate, 0 blocked, 0 refused"
        in result.output
    )
    apply_mock.assert_not_called()


# ── ≥1 refusal: no prompt, exit non-zero, apply never runs ───────────────────


def test_refusal_no_prompt_exit_nonzero(runner: CliRunner) -> None:
    phases = [_phase("l1")]
    plan = [_plan("l1", PhaseResult.CONFLICT)]
    with (
        _root_ctx(),
        patch("cli.main.cli_flow.build_phase_list", return_value=phases),
        patch("cli.main.run_plan_pass", return_value=plan),
        patch("cli.main._stdin_is_tty", return_value=True),
        patch("cli.main.run_apply_pass") as apply_mock,
    ):
        result = runner.invoke(app, ["setup"])
    assert result.exit_code == 1
    assert "Setup will not enter the apply pass." in result.output
    assert "0 already correct, 0 will mutate, 0 blocked, 1 refused" in result.output
    apply_mock.assert_not_called()


# ── ≥1 mutation, non-TTY, no --yes: refuse ───────────────────────────────────


def test_non_tty_without_yes_refuses(runner: CliRunner) -> None:
    phases = [_phase("l4")]
    plan = [_plan("l4", PhaseResult.MISSING)]
    with (
        _root_ctx(),
        patch("cli.main.cli_flow.build_phase_list", return_value=phases),
        patch("cli.main.run_plan_pass", return_value=plan),
        patch("cli.main._stdin_is_tty", return_value=False),
        patch("cli.main.run_apply_pass") as apply_mock,
    ):
        result = runner.invoke(app, ["setup"])
    assert result.exit_code == 1
    assert (
        "non-interactive context requires --yes flag to apply mutations"
        in result.output
    )
    apply_mock.assert_not_called()


# ── TTY prompt path: y/Y/yes/YES proceed ─────────────────────────────────────


@pytest.mark.parametrize("answer", ["y", "Y", "yes", "YES"])
def test_tty_prompt_affirmative_proceeds(runner: CliRunner, answer: str) -> None:
    phases = [_phase("l4")]
    plan = [_plan("l4", PhaseResult.MISSING)]
    apply_outcomes = [_apply("l4", PhaseResult.ALREADY_CORRECT, reverified=True)]
    with (
        _root_ctx(),
        patch("cli.main.cli_flow.build_phase_list", return_value=phases),
        patch("cli.main.run_plan_pass", return_value=plan),
        patch("cli.main._stdin_is_tty", return_value=True),
        patch("cli.main.run_apply_pass", return_value=apply_outcomes),
    ):
        result = runner.invoke(app, ["setup"], input=f"{answer}\n")
    assert result.exit_code == 0
    assert "Proceed with apply? [y/N]: " in result.output
    assert "Summary: 0 already correct, 1 applied" in result.output


@pytest.mark.parametrize("answer", ["n", "no", "", "garbage"])
def test_tty_prompt_negative_aborts_exit_0(
    runner: CliRunner, answer: str
) -> None:
    phases = [_phase("l4")]
    plan = [_plan("l4", PhaseResult.MISSING)]
    with (
        _root_ctx(),
        patch("cli.main.cli_flow.build_phase_list", return_value=phases),
        patch("cli.main.run_plan_pass", return_value=plan),
        patch("cli.main._stdin_is_tty", return_value=True),
        patch("cli.main.run_apply_pass") as apply_mock,
    ):
        result = runner.invoke(app, ["setup"], input=f"{answer}\n")
    assert result.exit_code == 0
    assert "aborted by operator (n). No mutations applied." in result.output
    apply_mock.assert_not_called()


# ── --yes skips the prompt (TTY) ─────────────────────────────────────────────


def test_yes_skips_prompt_and_proceeds(runner: CliRunner) -> None:
    phases = [_phase("l4")]
    plan = [_plan("l4", PhaseResult.MISSING)]
    apply_outcomes = [_apply("l4", PhaseResult.ALREADY_CORRECT, reverified=True)]
    with (
        _root_ctx(),
        patch("cli.main.cli_flow.build_phase_list", return_value=phases),
        patch("cli.main.run_plan_pass", return_value=plan),
        patch("cli.main._stdin_is_tty", return_value=True),
        patch("cli.main.run_apply_pass", return_value=apply_outcomes),
    ):
        result = runner.invoke(app, ["setup", "--yes"])
    assert result.exit_code == 0
    assert "Proceed with apply?" not in result.output


def test_yes_passes_assume_yes_to_distro_gate(runner: CliRunner) -> None:
    phases = [_phase("l4")]
    plan = [_plan("l4", PhaseResult.MISSING)]
    apply_outcomes = [_apply("l4", PhaseResult.ALREADY_CORRECT)]
    with (
        patch("cli.main.os.geteuid", return_value=0),
        patch("cli.main.resolve_operator", return_value="dev"),
        patch("cli.main.selected_extras", return_value=[]),
        patch("cli.main.emit_distro_gate") as gate_mock,
        patch("cli.main.cli_flow.build_phase_list", return_value=phases),
        patch("cli.main.run_plan_pass", return_value=plan),
        patch("cli.main._stdin_is_tty", return_value=False),
        patch("cli.main.run_apply_pass", return_value=apply_outcomes),
    ):
        result = runner.invoke(app, ["setup", "--yes"])
    assert result.exit_code == 0
    # --yes must reach emit_distro_gate so it skips the untested-distro prompt
    # while still emitting the warning.
    _, kwargs = gate_mock.call_args
    assert kwargs["assume_yes"] is True


# ── apply pass with a failure → non-zero exit ────────────────────────────────


def test_apply_failure_exits_nonzero(runner: CliRunner) -> None:
    phases = [_phase("l4")]
    plan = [_plan("l4", PhaseResult.MISSING)]
    apply_outcomes = [_apply("l4", PhaseResult.FAIL, reverified=False)]
    with (
        _root_ctx(),
        patch("cli.main.cli_flow.build_phase_list", return_value=phases),
        patch("cli.main.run_plan_pass", return_value=plan),
        patch("cli.main._stdin_is_tty", return_value=False),
        patch("cli.main.run_apply_pass", return_value=apply_outcomes),
    ):
        result = runner.invoke(app, ["setup", "--yes"])
    assert result.exit_code == 1
    assert "Remediation:" in result.output


# ── --dry-run: only the plan pass; no apply ──────────────────────────────────


def test_dry_run_runs_plan_only(runner: CliRunner) -> None:
    phases = [_phase("l4")]
    plan = [_plan("l4", PhaseResult.MISSING)]
    with (
        _root_ctx(),
        patch("cli.main.cli_flow.build_phase_list", return_value=phases),
        patch("cli.main.run_plan_pass", return_value=plan),
        patch("cli.main._stdin_is_tty", return_value=True),
        patch("cli.main.run_apply_pass") as apply_mock,
    ):
        result = runner.invoke(app, ["setup", "--dry-run"])
    assert result.exit_code == 0
    assert "0 already correct, 1 will mutate, 0 blocked, 0 refused" in result.output
    assert "Proceed with apply?" not in result.output
    apply_mock.assert_not_called()


# ── --update-runsc: ONLY the l6a phase, force=True ───────────────────────────


def test_update_runsc_runs_only_l6a_with_force(runner: CliRunner) -> None:
    l6a = _phase("l6a")
    other = _phase("l0")
    plan = [_plan("l6a", PhaseResult.DRIFT)]
    apply_outcomes = [_apply("l6a", PhaseResult.ALREADY_CORRECT)]
    with (
        patch("cli.main.os.geteuid", return_value=0),
        patch("cli.main.resolve_operator", return_value="dev"),
        patch("cli.main.set_force_update") as force_mock,
        patch(
            "cli.main.cli_flow.build_phase_list", return_value=[other, l6a]
        ),
        patch("cli.main.run_plan_pass", return_value=plan) as plan_mock,
        patch(
            "cli.main.run_apply_pass", return_value=apply_outcomes
        ) as apply_mock,
        patch("cli.main.emit_distro_gate") as gate_mock,
    ):
        result = runner.invoke(app, ["setup", "--update-runsc"])
    assert result.exit_code == 0
    force_mock.assert_called_once_with(True)
    # The distro gate is NOT consulted on the --update-runsc fast path.
    gate_mock.assert_not_called()
    # Only the l6a phase is passed to the passes.
    phases_arg = plan_mock.call_args[0][0]
    assert [p.id for p in phases_arg] == ["l6a"]
    apply_phases_arg = apply_mock.call_args[0][0]
    assert [p.id for p in apply_phases_arg] == ["l6a"]


def test_update_runsc_nonzero_on_apply_failure(runner: CliRunner) -> None:
    l6a = _phase("l6a")
    plan = [_plan("l6a", PhaseResult.DRIFT)]
    apply_outcomes = [_apply("l6a", PhaseResult.FAIL, reverified=False)]
    with (
        patch("cli.main.os.geteuid", return_value=0),
        patch("cli.main.resolve_operator", return_value="dev"),
        patch("cli.main.set_force_update"),
        patch("cli.main.cli_flow.build_phase_list", return_value=[l6a]),
        patch("cli.main.run_plan_pass", return_value=plan),
        patch("cli.main.run_apply_pass", return_value=apply_outcomes),
    ):
        result = runner.invoke(app, ["setup", "--update-runsc"])
    assert result.exit_code == 1


# ── SIGINT → exit 130, message to stderr, no mutations ───────────────────────


def test_sigint_during_ceremony_exits_130(runner: CliRunner) -> None:
    phases = [_phase("l4")]
    with (
        _root_ctx(),
        patch("cli.main.cli_flow.build_phase_list", return_value=phases),
        patch("cli.main.run_plan_pass", side_effect=_SetupAborted),
    ):
        result = runner.invoke(app, ["setup"])
    assert result.exit_code == 130
    assert (
        "aborted by operator (SIGINT). No mutations applied." in result.output
    )


def test_sigint_handler_installed_then_restored(runner: CliRunner) -> None:
    phases = [_phase("l4")]
    plan = [_plan("l4", PhaseResult.ALREADY_CORRECT)]
    captured: dict[str, object] = {}

    def _capture_plan(_phases: object, _ctx: object) -> list[PhasePlanOutcome]:
        import signal as _sig

        captured["during"] = _sig.getsignal(_sig.SIGINT)
        return plan

    import signal as _sig

    before = _sig.getsignal(_sig.SIGINT)
    with (
        _root_ctx(),
        patch("cli.main.cli_flow.build_phase_list", return_value=phases),
        patch("cli.main.run_plan_pass", side_effect=_capture_plan),
        patch("cli.main._stdin_is_tty", return_value=True),
    ):
        result = runner.invoke(app, ["setup"])
    assert result.exit_code == 0
    # A custom handler is installed while the ceremony runs ...
    assert captured["during"] is not before
    assert callable(captured["during"])
    # ... and the original handler is restored afterwards.
    assert _sig.getsignal(_sig.SIGINT) is before


def test_sigint_handler_callback_raises_setup_aborted(
    runner: CliRunner,
) -> None:
    phases = [_phase("l4")]
    plan = [_plan("l4", PhaseResult.ALREADY_CORRECT)]
    holder: dict[str, object] = {}

    def _grab(_phases: object, _ctx: object) -> list[PhasePlanOutcome]:
        import signal as _sig

        holder["handler"] = _sig.getsignal(_sig.SIGINT)
        return plan

    with (
        _root_ctx(),
        patch("cli.main.cli_flow.build_phase_list", return_value=phases),
        patch("cli.main.run_plan_pass", side_effect=_grab),
        patch("cli.main._stdin_is_tty", return_value=True),
    ):
        runner.invoke(app, ["setup"])
    handler = holder["handler"]
    assert callable(handler)
    with pytest.raises(_SetupAborted):
        handler(2, None)


# ── fresh-host bootstrap: absent toml → defaults-based HostConfig ────────────


@pytest.mark.no_host_config_mock
def test_fresh_host_uses_minimal_host_config(runner: CliRunner) -> None:
    phases = [_phase("l0")]
    plan = [_plan("l0", PhaseResult.ALREADY_CORRECT)]
    captured: list[SetupContext] = []

    def _capture(
        phs: object, ctx: SetupContext
    ) -> list[PhasePlanOutcome]:
        captured.append(ctx)
        return plan

    with (
        patch("cli.main.os.geteuid", return_value=0),
        patch("cli.main.resolve_operator", return_value="dev"),
        patch("cli.main.emit_distro_gate"),
        patch("cli.main.selected_extras", return_value=[]),
        patch(
            "cli.main.HostConfig.from_toml",
            side_effect=FileNotFoundError("no toml"),
        ),
        patch("cli.main.cli_flow.build_phase_list", return_value=phases),
        patch("cli.main.run_plan_pass", side_effect=_capture),
        patch("cli.main._stdin_is_tty", return_value=True),
    ):
        result = runner.invoke(app, ["setup"])
    assert result.exit_code == 0
    ctx = captured[0]
    # Defaults-based config: same defaults L4's _SEED_DEFAULTS writes.
    assert ctx.host_config.host.docker_unprivileged_user == "sandbox"
    assert ctx.operator == "dev"


# ── sticky-opt-in extras inclusion is wired into the phase list ──────────────


def test_extras_flags_passed_to_selected_extras(runner: CliRunner) -> None:
    phases = [_phase("l0")]
    plan = [_plan("l0", PhaseResult.ALREADY_CORRECT)]
    with (
        patch("cli.main.os.geteuid", return_value=0),
        patch("cli.main.resolve_operator", return_value="dev"),
        patch("cli.main.emit_distro_gate"),
        patch(
            "cli.main.selected_extras", return_value=["fapolicyd"]
        ) as sel_mock,
        patch(
            "cli.main.cli_flow.build_phase_list", return_value=phases
        ) as build_mock,
        patch("cli.main.run_plan_pass", return_value=plan),
        patch("cli.main._stdin_is_tty", return_value=True),
    ):
        result = runner.invoke(
            app,
            [
                "setup",
                "--enable-fapolicyd-integration",
                "--enable-aide-integration",
            ],
        )
    assert result.exit_code == 0
    flags_arg = sel_mock.call_args[0][0]
    assert flags_arg == {"fapolicyd": True, "aide": True}
    build_mock.assert_called_once_with(["fapolicyd"])
