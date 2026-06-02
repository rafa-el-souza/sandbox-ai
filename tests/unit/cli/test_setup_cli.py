"""Unit tests for the ``sandbox setup`` Typer command (Group 8).

Covers the full apply-gating edge-case matrix from tasks.md 8.3 with the
spec-exact literal strings, the non-root refusal, operator-resolution error
surfacing, ``--update-runsc`` (L6a-only, force=True), the SIGINT→130 path, the
sticky-opt-in extras inclusion, and ``--yes`` skipping both prompts. Subprocess
/ filesystem / stdin / signals are all mocked — the real ceremony never runs.
"""

from __future__ import annotations

import contextlib
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
import typer
from cli.main import (
    _build_setup_context_with_operator,
    _refuse_wrong_setup_identity,
    _resolve_setup_operator,
    _SetupAborted,
    _SetupFlagRefused,
    _SetupModeConflict,
    app,
    resolve_effective_mode,
)
from core.host_config import DockerExecutionMode, MachinectlAuth, minimal_host_config
from core.setup.host_batch import BatchItem, BatchParams
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
    # Mode-conditional identity gate (§8-C): the separate-user root requirement is
    # checked AFTER the context build resolves the mode. With a resolvable operator
    # and the default separate-user mode, a non-root invocation is refused with the
    # exact existing message (the gate is reached through the real command path).
    with (
        patch("cli.main.os.geteuid", return_value=1000),
        patch("cli.main.resolve_operator", return_value="dev"),
        patch("cli.main.read_mode", return_value=None),
    ):
        result = runner.invoke(app, ["setup"])
    assert result.exit_code == 1
    assert (
        "sandbox setup must be run as root. Re-invoke as: sudo sandbox setup"
        in result.output
    )


def test_setup_refuses_root_in_operator_rootless(runner: CliRunner) -> None:
    # operator-rootless setup REFUSES root (§8-C / D5): with the op-rootless mode
    # selected and a resolvable non-root daemon owner, euid==0 is refused before
    # any mutation, through the real command path.
    with (
        patch("cli.main.os.geteuid", return_value=0),
        patch("cli.main.resolve_operator", return_value="dev"),
        patch("cli.main.read_mode", return_value=None),
        patch("core.host_config.getpass.getuser", return_value="dev"),
    ):
        result = runner.invoke(
            app, ["setup", "--docker-execution-mode", "operator-rootless"]
        )
    assert result.exit_code == 1
    assert "operator-rootless setup must NOT be run as root" in result.output


# ── _refuse_wrong_setup_identity: the mode-conditional gate, in isolation ──────


def _identity_ctx(mode: DockerExecutionMode) -> SetupContext:
    return SetupContext(
        host_config=minimal_host_config("sandbox", MachinectlAuth.SUDO, mode),
        operator="dev",
    )


def test_identity_gate_separate_user_requires_root() -> None:
    ctx = _identity_ctx(DockerExecutionMode.SEPARATE_USER)
    with patch("cli.main.os.geteuid", return_value=1000), pytest.raises(typer.Exit):
        _refuse_wrong_setup_identity(ctx)


def test_identity_gate_separate_user_root_ok() -> None:
    ctx = _identity_ctx(DockerExecutionMode.SEPARATE_USER)
    with patch("cli.main.os.geteuid", return_value=0):
        _refuse_wrong_setup_identity(ctx)  # no raise — root is correct here


def test_identity_gate_operator_rootless_refuses_root() -> None:
    ctx = _identity_ctx(DockerExecutionMode.OPERATOR_ROOTLESS)
    with patch("cli.main.os.geteuid", return_value=0), pytest.raises(typer.Exit):
        _refuse_wrong_setup_identity(ctx)


def test_identity_gate_operator_rootless_nonroot_ok() -> None:
    ctx = _identity_ctx(DockerExecutionMode.OPERATOR_ROOTLESS)
    with patch("cli.main.os.geteuid", return_value=1000):
        _refuse_wrong_setup_identity(ctx)  # no raise — non-root is correct here


# ── _resolve_setup_operator: euid-aware operator resolution (§8-D) ─────────────


def test_resolve_setup_operator_root_uses_precedence() -> None:
    # As root (separate-user), the canonical root-scoped precedence is used as-is.
    with (
        patch("cli.main.os.geteuid", return_value=0),
        patch("cli.main.resolve_operator", return_value="alice"),
    ):
        assert _resolve_setup_operator(None) == "alice"


def test_resolve_setup_operator_nonroot_falls_back_to_current_user() -> None:
    # Non-root (operator-rootless) with no resolvable operator → the invoking user
    # (spec "Operator-Run Least-Privilege Provisioning"; the canonical precedence
    # is root-scoped, so its refusal is the fallback trigger here).
    with (
        patch("cli.main.os.geteuid", return_value=1000),
        patch(
            "cli.main.resolve_operator",
            side_effect=OperatorResolutionError("cannot resolve operator user."),
        ),
        patch("cli.main.getpass.getuser", return_value="dev"),
    ):
        assert _resolve_setup_operator(None) == "dev"


def test_resolve_setup_operator_root_refusal_reraises() -> None:
    # As root, a refusal is preserved (F-021 — no current-user fallback under root).
    with (
        patch("cli.main.os.geteuid", return_value=0),
        patch(
            "cli.main.resolve_operator",
            side_effect=OperatorResolutionError("cannot resolve operator user."),
        ),
        pytest.raises(OperatorResolutionError),
    ):
        _resolve_setup_operator(None)


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
    phases = [_phase("l5")]
    plan = [_plan("l5", PhaseResult.MISSING)]
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
    phases = [_phase("l5")]
    plan = [_plan("l5", PhaseResult.MISSING)]
    apply_outcomes = [_apply("l5", PhaseResult.ALREADY_CORRECT, reverified=True)]
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
    phases = [_phase("l5")]
    plan = [_plan("l5", PhaseResult.MISSING)]
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
    phases = [_phase("l5")]
    plan = [_plan("l5", PhaseResult.MISSING)]
    apply_outcomes = [_apply("l5", PhaseResult.ALREADY_CORRECT, reverified=True)]
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
    phases = [_phase("l5")]
    plan = [_plan("l5", PhaseResult.MISSING)]
    apply_outcomes = [_apply("l5", PhaseResult.ALREADY_CORRECT)]
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
    phases = [_phase("l5")]
    plan = [_plan("l5", PhaseResult.MISSING)]
    apply_outcomes = [_apply("l5", PhaseResult.FAIL, reverified=False)]
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
    phases = [_phase("l5")]
    plan = [_plan("l5", PhaseResult.MISSING)]
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


def test_dry_run_with_conflict_plan_still_exits_0(runner: CliRunner) -> None:
    # --dry-run is preview-only: it returns 0 after the plan pass even when
    # the plan contains a CONFLICT (the gate decision is not computed on the
    # dry-run path; the apply pass never runs).
    phases = [_phase("l5")]
    plan = [_plan("l5", PhaseResult.CONFLICT)]
    with (
        _root_ctx(),
        patch("cli.main.cli_flow.build_phase_list", return_value=phases),
        patch("cli.main.run_plan_pass", return_value=plan),
        patch("cli.main._stdin_is_tty", return_value=True),
        patch("cli.main.run_apply_pass") as apply_mock,
    ):
        result = runner.invoke(app, ["setup", "--dry-run"])
    assert result.exit_code == 0
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
    # The --update-runsc fast path emits the same spec-exact Summary: line
    # every other invocation emits (P5).
    assert (
        "Summary: 0 already correct, 1 will mutate, 0 blocked, 0 refused"
        in result.output
    )
    # The distro gate is NOT consulted on the --update-runsc fast path.
    gate_mock.assert_not_called()
    # Only the l6a phase is passed to the passes.
    phases_arg = plan_mock.call_args[0][0]
    assert [p.id for p in phases_arg] == ["l6a"]
    apply_phases_arg = apply_mock.call_args[0][0]
    assert [p.id for p in apply_phases_arg] == ["l6a"]
    # Both passes MUST be told to allow external deps — the single-phase l6a
    # subset has a dangling ``l6`` edge that order_phases' strict mode rejects
    # (the round-5 fedora 12.3 PhaseDependencyError crash). See the real-phase
    # tie-in in test_l6a_runsc.py.
    assert plan_mock.call_args.kwargs["allow_external_deps"] is True
    assert apply_mock.call_args.kwargs["allow_external_deps"] is True


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
    phases = [_phase("l5")]
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
    phases = [_phase("l5")]
    plan = [_plan("l5", PhaseResult.ALREADY_CORRECT)]
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
    phases = [_phase("l5")]
    plan = [_plan("l5", PhaseResult.ALREADY_CORRECT)]
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


# ── D8: setup is toml-free — host_config from flags + defaults only ──────────


@pytest.mark.no_host_config_mock
def test_setup_is_toml_free(runner: CliRunner) -> None:
    """D8 regression: setup builds host_config from flags + defaults and NEVER
    reads the operator toml (`HostConfig.from_toml`).

    The pre-fix tree called `HostConfig.from_toml()` on the setup path (resolving
    to root's `/root/.sandbox-ai`); patching it to fail-on-call demonstrates the
    read is gone. The config still comes from the documented default
    (`docker_unprivileged_user="sandbox"`) and the resolved operator.
    """
    phases = [_phase("l0")]
    plan = [_plan("l0", PhaseResult.ALREADY_CORRECT)]
    captured: list[SetupContext] = []

    def _capture(
        phs: object, ctx: SetupContext
    ) -> list[PhasePlanOutcome]:
        captured.append(ctx)
        return plan

    def _explode() -> object:
        raise AssertionError("setup must not read the operator toml (D8)")

    with (
        patch("cli.main.os.geteuid", return_value=0),
        patch("cli.main.resolve_operator", return_value="dev"),
        patch("cli.main.emit_distro_gate"),
        patch("cli.main.selected_extras", return_value=[]),
        patch("cli.main.HostConfig.from_toml", side_effect=_explode),
        patch("cli.main.cli_flow.build_phase_list", return_value=phases),
        patch("cli.main.run_plan_pass", side_effect=_capture),
        patch("cli.main._stdin_is_tty", return_value=True),
    ):
        result = runner.invoke(app, ["setup"])
    assert result.exit_code == 0
    ctx = captured[0]
    # Defaults-based config: docker_unprivileged_user defaults to "sandbox"
    # (the same value `sandbox init` later seeds); operator from the resolver.
    assert ctx.host_config.host.docker_unprivileged_user == "sandbox"
    assert ctx.operator == "dev"


# ── F-022: --machinectl-auth input + POLKIT fence (D2) ───────────────────────


@pytest.mark.no_host_config_mock
def test_machinectl_auth_sudo_flag_accepted(runner: CliRunner) -> None:
    """`--machinectl-auth sudo` is the explicit SUDO selection; setup proceeds."""
    from core.host_config import MachinectlAuth

    phases = [_phase("l0")]
    plan = [_plan("l0", PhaseResult.ALREADY_CORRECT)]
    captured: list[SetupContext] = []

    def _capture(_phs: object, ctx: SetupContext) -> list[PhasePlanOutcome]:
        captured.append(ctx)
        return plan

    with (
        patch("cli.main.os.geteuid", return_value=0),
        patch("cli.main.resolve_operator", return_value="dev"),
        patch("cli.main.emit_distro_gate"),
        patch("cli.main.selected_extras", return_value=[]),
        patch("cli.main.cli_flow.build_phase_list", return_value=phases),
        patch("cli.main.run_plan_pass", side_effect=_capture),
        patch("cli.main._stdin_is_tty", return_value=True),
    ):
        result = runner.invoke(app, ["setup", "--machinectl-auth", "sudo"])
    assert result.exit_code == 0
    assert captured[0].host_config.host.machinectl_authentication == MachinectlAuth.SUDO


@pytest.mark.no_host_config_mock
def test_machinectl_auth_polkit_flag_refused(runner: CliRunner) -> None:
    """`--machinectl-auth polkit` is fenced (D2): refuse, exit 1, no plan pass."""
    with (
        patch("cli.main.os.geteuid", return_value=0),
        patch("cli.main.resolve_operator", return_value="dev"),
        patch("cli.main.run_plan_pass") as plan_mock,
    ):
        result = runner.invoke(app, ["setup", "--machinectl-auth", "polkit"])
    assert result.exit_code == 1
    assert "POLKIT auth mode is not yet supported" in result.output
    plan_mock.assert_not_called()


@pytest.mark.no_host_config_mock
def test_machinectl_auth_invalid_value_refused(runner: CliRunner) -> None:
    """An out-of-domain `--machinectl-auth` value is refused with a clear message."""
    with (
        patch("cli.main.os.geteuid", return_value=0),
        patch("cli.main.resolve_operator", return_value="dev"),
        patch("cli.main.run_plan_pass") as plan_mock,
    ):
        result = runner.invoke(app, ["setup", "--machinectl-auth", "bogus"])
    assert result.exit_code == 1
    assert "Invalid --machinectl-auth value" in result.output
    plan_mock.assert_not_called()


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


# ── C-004 §4.2: resolve_effective_mode (marker decision + conflict refuse) ───


def test_resolve_effective_mode_no_entry_no_flag_defaults_separate_user() -> None:
    """No marker entry + no flag → default SEPARATE_USER (D6)."""
    with patch("cli.main.read_mode", return_value=None):
        assert (
            resolve_effective_mode("alice", None) is DockerExecutionMode.SEPARATE_USER
        )


def test_resolve_effective_mode_no_entry_with_flag_uses_flag() -> None:
    """No marker entry + flag → the requested mode (D6)."""
    with patch("cli.main.read_mode", return_value=None):
        assert (
            resolve_effective_mode("alice", DockerExecutionMode.OPERATOR_ROOTLESS)
            is DockerExecutionMode.OPERATOR_ROOTLESS
        )


def test_resolve_effective_mode_entry_no_flag_uses_recorded() -> None:
    """Marker entry + no flag → the recorded mode (idempotent re-run, D6)."""
    with patch(
        "cli.main.read_mode", return_value=DockerExecutionMode.OPERATOR_ROOTLESS
    ):
        assert (
            resolve_effective_mode("alice", None)
            is DockerExecutionMode.OPERATOR_ROOTLESS
        )


def test_resolve_effective_mode_entry_matching_flag_ok() -> None:
    """Marker entry + matching flag → no conflict, recorded mode returned."""
    with patch(
        "cli.main.read_mode", return_value=DockerExecutionMode.OPERATOR_ROOTLESS
    ):
        assert (
            resolve_effective_mode("alice", DockerExecutionMode.OPERATOR_ROOTLESS)
            is DockerExecutionMode.OPERATOR_ROOTLESS
        )


def test_resolve_effective_mode_conflicting_flag_refused() -> None:
    """Marker entry + conflicting flag → _SetupModeConflict (D6)."""
    with (
        patch(
            "cli.main.read_mode", return_value=DockerExecutionMode.OPERATOR_ROOTLESS
        ),
        pytest.raises(_SetupModeConflict) as exc,
    ):
        resolve_effective_mode("alice", DockerExecutionMode.SEPARATE_USER)
    msg = str(exc.value)
    assert "provisioned as operator-rootless" in msg
    assert "switching to separate-user requires teardown first" in msg


# ── C-004 §4.3/§4.4: setup flag threading + refuse-all guards ────────────────


def test_invalid_mode_flag_refused(runner: CliRunner) -> None:
    """An out-of-domain --docker-execution-mode value is refused, no plan pass."""
    with (
        patch("cli.main.os.geteuid", return_value=0),
        patch("cli.main.resolve_operator", return_value="dev"),
        patch("cli.main.read_mode", return_value=None),
        patch("cli.main.run_plan_pass") as plan_mock,
    ):
        result = runner.invoke(
            app, ["setup", "--docker-execution-mode", "bogus"]
        )
    assert result.exit_code == 1
    assert "Invalid --docker-execution-mode value" in result.output
    plan_mock.assert_not_called()


def test_flags_threaded_into_host_config() -> None:
    """--docker-unprivileged-user and --workspace-bridge-group thread into host_config."""
    with (
        patch("cli.main.resolve_operator", return_value="dev"),
        patch("cli.main.read_mode", return_value=None),
    ):
        ctx = _build_setup_context_with_operator(
            None,
            None,
            mode_flag="separate-user",
            docker_unprivileged_user="customsvc",
            workspace_bridge_group="custom-ws",
        )
    assert ctx.host_config.host.docker_unprivileged_user == "customsvc"
    assert ctx.host_config.host.workspace_bridge_group == "custom-ws"
    assert (
        ctx.host_config.host.docker_execution_mode is DockerExecutionMode.SEPARATE_USER
    )


def test_mode_flag_threaded_into_host_config() -> None:
    """--docker-execution-mode operator-rootless threads through (invoker = operator)."""
    with (
        patch("cli.main.resolve_operator", return_value="dev"),
        patch("cli.main.read_mode", return_value=None),
        patch("cli.main.getpass.getuser", return_value="dev"),
        patch("core.host_config.getpass.getuser", return_value="dev"),
    ):
        ctx = _build_setup_context_with_operator(
            None, None, mode_flag="operator-rootless"
        )
    assert (
        ctx.host_config.host.docker_execution_mode
        is DockerExecutionMode.OPERATOR_ROOTLESS
    )


def test_guard_refuses_docker_unprivileged_user_in_operator_rootless() -> None:
    """--docker-unprivileged-user in op-rootless is refused (inapplicable)."""
    with (
        patch("cli.main.resolve_operator", return_value="dev"),
        patch("cli.main.read_mode", return_value=None),
        patch("cli.main.getpass.getuser", return_value="dev"),
        patch("core.host_config.getpass.getuser", return_value="dev"),
        pytest.raises(_SetupFlagRefused) as exc,
    ):
        _build_setup_context_with_operator(
            None,
            None,
            mode_flag="operator-rootless",
            docker_unprivileged_user="foo",
        )
    assert "--docker-unprivileged-user does not apply in operator-rootless" in str(
        exc.value
    )


def test_guard_refuses_machinectl_auth_in_operator_rootless() -> None:
    """--machinectl-auth in op-rootless is refused (inapplicable)."""
    with (
        patch("cli.main.resolve_operator", return_value="dev"),
        patch("cli.main.read_mode", return_value=None),
        patch("cli.main.getpass.getuser", return_value="dev"),
        patch("core.host_config.getpass.getuser", return_value="dev"),
        pytest.raises(_SetupFlagRefused) as exc,
    ):
        _build_setup_context_with_operator(
            None,
            "sudo",
            mode_flag="operator-rootless",
        )
    assert "--machinectl-auth does not apply in operator-rootless" in str(exc.value)


def test_guard_refuses_operator_other_than_invoker_in_operator_rootless() -> None:
    """--operator naming another user in op-rootless is refused (G5)."""
    with (
        patch("cli.main.resolve_operator", return_value="someone-else"),
        patch("cli.main.read_mode", return_value=None),
        patch("cli.main.getpass.getuser", return_value="dev"),
        pytest.raises(_SetupFlagRefused) as exc,
    ):
        _build_setup_context_with_operator(
            "someone-else",
            None,
            mode_flag="operator-rootless",
        )
    assert "does not match the invoking user" in str(exc.value)


def test_guard_refuses_root_daemon_owner_separate_user() -> None:
    """A resolved daemon owner of root (uid 0) is refused (dangerous value)."""
    with (
        patch("cli.main.resolve_operator", return_value="dev"),
        patch("cli.main.read_mode", return_value=None),
        pytest.raises(_SetupFlagRefused) as exc,
    ):
        _build_setup_context_with_operator(
            None,
            None,
            mode_flag="separate-user",
            docker_unprivileged_user="root",
        )
    assert "root / uid 0" in str(exc.value)


def test_guard_refuses_root_aliased_daemon_owner() -> None:
    """A non-'root' name that resolves to uid 0 is also refused (_is_root_user)."""
    with (
        patch("cli.main.resolve_operator", return_value="dev"),
        patch("cli.main.read_mode", return_value=None),
        patch(
            "cli.main.pwd.getpwnam",
            return_value=type("PW", (), {"pw_uid": 0})(),
        ),
        pytest.raises(_SetupFlagRefused) as exc,
    ):
        _build_setup_context_with_operator(
            None,
            None,
            mode_flag="separate-user",
            docker_unprivileged_user="toor",
        )
    assert "root / uid 0" in str(exc.value)


def test_guard_unknown_daemon_owner_user_not_treated_as_root() -> None:
    """_is_root_user tolerates a KeyError (unknown user) and does not refuse."""

    def _missing(_name: str) -> object:
        raise KeyError("no such user")

    with (
        patch("cli.main.resolve_operator", return_value="dev"),
        patch("cli.main.read_mode", return_value=None),
        patch("cli.main.pwd.getpwnam", side_effect=_missing),
    ):
        ctx = _build_setup_context_with_operator(
            None,
            None,
            mode_flag="separate-user",
            docker_unprivileged_user="brand-new-svc",
        )
    assert ctx.host_config.host.docker_unprivileged_user == "brand-new-svc"


# ── `_bootstrap-host` hidden root-only escalation sub-step (§8-B) ─────────────

_BOOTSTRAP_PARAM_ARGS = [
    "--operator",
    "alice",
    "--operator-uid",
    "1001",
    "--bridge-group",
    "sb-ws",
    "--bridge-gid",
    "100100",
    "--distro-family",
    "arch",
    "--docker-execution-mode",
    "operator-rootless",
]


def test_bootstrap_host_refuses_non_root(runner: CliRunner) -> None:
    """`_bootstrap-host` is the root-only escalation step; non-root is refused."""
    with patch("cli.main.os.geteuid", return_value=1000):
        result = runner.invoke(
            app, ["_bootstrap-host", "--item", "subid", *_BOOTSTRAP_PARAM_ARGS]
        )
    assert result.exit_code == 1
    assert "_bootstrap-host must be run as root" in result.output


def test_bootstrap_host_applies_typed_batch_as_root(runner: CliRunner) -> None:
    """Root invocation reconstructs the typed batch + params and applies them."""
    with (
        patch("cli.main.os.geteuid", return_value=0),
        patch("cli.main.host_batch.apply_host_root_batch") as apply_mock,
    ):
        result = runner.invoke(
            app,
            [
                "_bootstrap-host",
                "--item",
                "subid",
                "--item",
                "groupadd",
                "--item",
                "marker",
                *_BOOTSTRAP_PARAM_ARGS,
            ],
        )
    assert result.exit_code == 0
    apply_mock.assert_called_once()
    items, params = apply_mock.call_args.args
    assert items == frozenset({BatchItem.SUBID, BatchItem.GROUPADD, BatchItem.MARKER})
    assert params == BatchParams(
        operator="alice",
        operator_uid=1001,
        bridge_group="sb-ws",
        bridge_gid=100100,
        distro_family="arch",
        mode=DockerExecutionMode.OPERATOR_ROOTLESS,
    )


def test_bootstrap_host_rejects_unknown_item(runner: CliRunner) -> None:
    """An ``--item`` that is not a BatchItem is refused before any apply."""
    with (
        patch("cli.main.os.geteuid", return_value=0),
        patch("cli.main.host_batch.apply_host_root_batch") as apply_mock,
    ):
        result = runner.invoke(
            app, ["_bootstrap-host", "--item", "bogus", *_BOOTSTRAP_PARAM_ARGS]
        )
    assert result.exit_code == 1
    assert "Invalid --item" in result.output
    apply_mock.assert_not_called()


def test_bootstrap_host_rejects_unknown_mode(runner: CliRunner) -> None:
    """An unrecognized ``--docker-execution-mode`` is refused before any apply."""
    with (
        patch("cli.main.os.geteuid", return_value=0),
        patch("cli.main.host_batch.apply_host_root_batch") as apply_mock,
    ):
        result = runner.invoke(
            app,
            [
                "_bootstrap-host",
                "--item",
                "subid",
                "--operator",
                "alice",
                "--operator-uid",
                "1001",
                "--bridge-group",
                "sb-ws",
                "--bridge-gid",
                "100100",
                "--distro-family",
                "arch",
                "--docker-execution-mode",
                "nonsense",
            ],
        )
    assert result.exit_code == 1
    assert "Invalid --docker-execution-mode" in result.output
    apply_mock.assert_not_called()


def test_bootstrap_host_applier_failure_exits_nonzero(runner: CliRunner) -> None:
    """A mid-batch applier failure surfaces as a non-zero exit + diagnostic."""
    with (
        patch("cli.main.os.geteuid", return_value=0),
        patch(
            "cli.main.host_batch.apply_host_root_batch",
            side_effect=subprocess.CalledProcessError(1, ["groupadd"]),
        ),
    ):
        result = runner.invoke(
            app, ["_bootstrap-host", "--item", "groupadd", *_BOOTSTRAP_PARAM_ARGS]
        )
    assert result.exit_code == 1
    assert "host-root batch failed" in result.output
