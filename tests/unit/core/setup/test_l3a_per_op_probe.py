"""Unit tests for ``core.setup.l3a_per_op_probe`` (L3a — per-op probe).

Covers the F-004 / Finding-J inner-exit-recovery decision matrix: MATCH
(recovered inner exit 0), ``password is required`` (sudoers did not grant /
MACHINECTL_PATH drift), and a generic non-zero recovered inner exit (incl. the
dispatcher reject exit 2). Asserts the sweep iterates every ``core.dispatch.Op``,
that a non-MATCH raises, that ``_act`` raising fires the phase-runner's
rollback (which removes the L3 drop-in), and the PHASE wiring.

The control signal is the **recovered inner exit**, surfaced as a raised
:class:`SandboxExecutionError` by :class:`core.executor.Executor` run with
``framed=True`` (the dispatcher emits its own begin/exit framing; the crossed
payload stays the bare ``<dispatch> <op> --check`` the per-op rule matches —
F-018, NOT the pre-fix ``sentinel=True`` wrap). The tests mock ``Executor`` and
exercise the branch logic on that recovered signal — NOT on a raw outer exit —
while the captured argv is the real ``_probe_argv`` output (so the operator-drop
shape AND the pipe crossing are asserted for real; see
``test_probe_argv_crosses_via_pipe_under_sudo_u``, C-009 D4 / F-016).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from core.dispatch import Op
from core.exceptions import SandboxExecutionError
from core.host_config import (
    DockerExecutionMode,
    HostConfig,
    MachinectlAuth,
    minimal_host_config,
)
from core.setup import l3a_per_op_probe as l3a
from core.setup.l3a_per_op_probe import PHASE, PerOpProbeError
from core.setup.phase_runner import (
    Identity,
    Phase,
    PhaseResult,
    SetupContext,
    run_apply_pass,
)


@pytest.fixture(autouse=True)
def _stable_systemd_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        l3a, "resolve_systemd_run_path", lambda _hc: "/usr/bin/systemd-run"
    )


def _hc(auth: MachinectlAuth = MachinectlAuth.SUDO) -> HostConfig:
    return minimal_host_config("sandbox", auth)


def _ctx(auth: MachinectlAuth = MachinectlAuth.SUDO) -> SetupContext:
    return SetupContext(host_config=_hc(auth), operator="alice")


class _FakeExecutor:
    """Stand-in for :class:`core.executor.Executor`.

    ``behavior`` maps an op wire-name to either ``None`` (recovered inner exit
    0 — MATCH) or a :class:`SandboxExecutionError` to raise (the sentinel
    mechanism's non-zero-inner-exit signal).
    """

    def __init__(self, behavior: dict[str, SandboxExecutionError | None]):
        self._behavior = behavior
        self.calls: list[list[str]] = []

    def run(
        self, cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert kwargs.get("framed") is True, (
            "L3a MUST recover the inner exit via the dispatcher's begin/exit "
            "framing (framed=True), NOT inject a sentinel into the crossed "
            "payload (sentinel=True would make the authorized command "
            "unmatchable by the per-op Cmnd_Spec — F-018), and NOT branch on "
            "the raw outer (sudo/machinectl) exit"
        )
        self.calls.append(cmd)
        # The op wire-name is the token after `dispatch ` in the bash payload.
        inner = cmd[-1]
        op_name = inner.split("dispatch ", 1)[1].split(" ", 1)[0]
        outcome = self._behavior.get(op_name)
        if outcome is not None:
            raise outcome
        return subprocess.CompletedProcess(cmd, 0, "ok", "")


def _install_executor(
    monkeypatch: pytest.MonkeyPatch,
    behavior: dict[str, SandboxExecutionError | None],
) -> _FakeExecutor:
    fake = _FakeExecutor(behavior)
    monkeypatch.setattr(l3a, "Executor", lambda: fake)
    return fake


# ── probe is always MISSING (verification phase, decision 2) ─────────────────


def test_probe_always_missing() -> None:
    result, detail = l3a._probe(_ctx())
    assert result == PhaseResult.MISSING
    assert "no idempotent skip" in detail


# ── MATCH path: every op recovered inner exit 0 ──────────────────────────────


def test_sweep_all_match(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_executor(monkeypatch, {})
    detail = l3a._act(_ctx())
    assert "resolved to MATCH" in detail
    # Every op probed exactly once, in Op order.
    probed = [
        c[-1].split("dispatch ", 1)[1].split(" ", 1)[0] for c in fake.calls
    ]
    assert probed == [op.value for op in Op]


def test_probe_argv_is_relative_systemd_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_executor(monkeypatch, {})
    l3a._act(_ctx())
    argv = fake.calls[0]
    # `sudo -n systemd-run` (relative, B-3) — NOT an absolute
    # /usr/bin/systemd-run. The inner sudo is the one after the operator drop.
    inner_sudo = argv.index("sudo", argv.index("alice"))
    assert argv[inner_sudo + 1] == "-n"
    assert argv[inner_sudo + 2] == "systemd-run"
    assert "/usr/bin/systemd-run" not in argv
    # The M3-i rule renders ONLY the pipe Cmnd_Spec — machinectl is gone.
    assert "machinectl" not in argv


def test_probe_argv_crosses_via_pipe_under_sudo_u(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe crosses via the PIPE, dropped under ``sudo -u <operator>``.

    C-009 D4: the machinectl operator ``Cmnd_Spec`` was REMOVED in M3-i, so the
    probe MUST cross via ``pipe_cmd`` (the relative ``systemd-run -q --pipe
    --uid=<user>``) to match the freshly-installed rule — a machinectl probe
    would be unauthorized and would roll back a correct rule ("stranding").

    F-016: root drops to the operator via ``sudo_as_operator`` (``sudo -u``)
    because the operator-side command is the setuid ``sudo``; that ``sudo``
    cannot exec inside a ``pipe_cmd`` ``--uid`` transient unit (EXIT_EXEC 203).
    The authorized inner is the relative ``systemd-run`` ``pipe_cmd`` builds,
    whose ``--uid`` unit execs ``/bin/bash`` → dispatch (non-setuid), so F-016
    does not block the inner. The argv built here is the real ``_probe_argv``
    output (only the boundary call is faked).
    """
    fake = _install_executor(monkeypatch, {})
    l3a._act(_ctx())
    argv = fake.calls[0]
    # Normal-process operator drop, then the operator's own `sudo -n`.
    assert argv[:5] == ["sudo", "-u", "alice", "sudo", "-n"]
    # The authorized crossing is the relative systemd-run pipe (pipe_cmd) —
    # byte-identical to the rendered pipe Cmnd_Spec launcher prefix.
    assert argv[5:9] == ["systemd-run", "-q", "--pipe", "--uid=sandbox"]
    # The inner payload is the bare `<dispatch> <op> --check` the rule matches.
    assert argv[-3:] == [
        "/bin/bash",
        "-c",
        f"/usr/local/libexec/sandbox-ai/dispatch {Op.AUTH_PROBE.value} --check",
    ]


# ── password-required branch (F-004 / MACHINECTL_PATH drift) ─────────────────


def test_password_required_classifies_as_grant_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    err = SandboxExecutionError(
        "[FATAL] Inner command failed with exit status 1.\n"
        "Error Trace:\nsudo: a password is required"
    )
    _install_executor(monkeypatch, {Op.COMPOSE_UP.value: err})
    with pytest.raises(PerOpProbeError) as exc:
        l3a._act(_ctx())
    msg = str(exc.value)
    assert "compose-up" in msg
    assert "missed backslash-escape (F-004)" in msg
    assert "SYSTEMD_RUN_PATH drift" in msg
    assert "/usr/bin/systemd-run" in msg


# ── generic non-zero (incl. dispatcher reject exit 2) ────────────────────────


def test_dispatcher_reject_exit2_classifies_as_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    err = SandboxExecutionError(
        "[FATAL] Sandbox Execution Fault: Inner command failed with exit "
        "status 2."
    )
    _install_executor(monkeypatch, {Op.AUTH_PROBE.value: err})
    with pytest.raises(PerOpProbeError) as exc:
        l3a._act(_ctx())
    msg = str(exc.value)
    assert "auth-probe" in msg
    assert "broken / absent / mis-pathed" in msg
    assert "exit 2" in msg


def test_sweep_stops_at_first_non_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    err = SandboxExecutionError("Inner command failed with exit status 2.")
    fake = _install_executor(monkeypatch, {Op.AUTH_PROBE.value: err})
    with pytest.raises(PerOpProbeError):
        l3a._act(_ctx())
    # AUTH_PROBE is first in the Op enum; the sweep must NOT continue past it.
    assert len(fake.calls) == 1


# ── reverify re-runs the sweep ───────────────────────────────────────────────


def test_reverify_true_when_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_executor(monkeypatch, {})
    assert l3a._reverify(_ctx()) is True


def test_reverify_raises_on_non_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    err = SandboxExecutionError("Inner command failed with exit status 2.")
    _install_executor(monkeypatch, {Op.AUTH_PROBE.value: err})
    with pytest.raises(PerOpProbeError):
        l3a._reverify(_ctx())


# ── rollback removes the L3 drop-in ──────────────────────────────────────────


def test_rollback_removes_drop_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "core.setup.l3_sudoers_polkit._SUDOERS_DIR", tmp_path
    )
    drop_in = tmp_path / "sandbox-ai-machinectl-alice"
    drop_in.write_text("rule")
    l3a._rollback(_ctx())
    assert not drop_in.exists()


def test_rollback_idempotent_when_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "core.setup.l3_sudoers_polkit._SUDOERS_DIR", tmp_path
    )
    l3a._rollback(_ctx())  # must not raise


# ── rollback fires through the phase-runner on FAIL ──────────────────────────


def test_phase_runner_fires_rollback_on_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-MATCH op -> _act raises -> phase-runner FAIL -> rollback rm.

    Drives the real ``run_apply_pass`` over just the L3a phase so the rollback
    is fired by the runner (design D1), not called directly.
    """
    monkeypatch.setattr(
        "core.setup.l3_sudoers_polkit._SUDOERS_DIR", tmp_path
    )
    drop_in = tmp_path / "sandbox-ai-machinectl-alice"
    drop_in.write_text("rule that L3a will reject")
    err = SandboxExecutionError("Inner command failed with exit status 2.")
    _install_executor(monkeypatch, {Op.AUTH_PROBE.value: err})

    # A satisfied stub for the `l3` dependency so the graph resolves; it
    # probes ALREADY_CORRECT and is never acted on.
    l3_stub = Phase(
        id="l3",
        name="l3 stub",
        identity=Identity.ROOT,
        probe=lambda _hc: (PhaseResult.ALREADY_CORRECT, "stub"),
        act=lambda _hc: "noop",
        reverify=lambda _hc: True,
    )

    outcomes = run_apply_pass([l3_stub, PHASE], _ctx())

    l3a_outcome = next(o for o in outcomes if o.phase_id == "l3a")
    assert l3a_outcome.result == PhaseResult.FAIL
    assert "rolled back" in l3a_outcome.detail
    assert not drop_in.exists()


# ── PHASE wiring ─────────────────────────────────────────────────────────────


def test_phase_identity_and_graph() -> None:
    assert PHASE.id == "l3a"
    assert PHASE.depends_on == ("l3",)
    assert PHASE.identity == Identity.OPERATOR
    assert PHASE.rollback is l3a._rollback
    # no L3 rule to probe in operator-rootless → separate-user only.
    assert PHASE.applies_in == frozenset({DockerExecutionMode.SEPARATE_USER})
