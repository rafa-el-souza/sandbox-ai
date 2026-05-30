"""Unit tests for ``core.setup.l3_sudoers_polkit`` (L3 — privilege rule).

Covers: the V9 golden-file render + zero-``"`` assertion across every
``Cmnd_Spec``; the F-004 render-time refusal (a ``"`` in a body / a bad
op-name); the content-aware probe (ALREADY_CORRECT -> DRIFT on a stale Op enum,
driven by the shared ``assert_phase_content_aware`` fixture); SUDO vs POLKIT
auth-mode branching; visudo-failure refusal; the rollback removing the
just-installed drop-in. (Operator resolution itself now lives in
``core.setup.l0_identity.resolve_operator`` and is tested there; L3 reads the
already-resolved operator off the :class:`SetupContext`.)
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from core.dispatch import Op
from core.host_config import (
    DockerExecutionMode,
    HostConfig,
    MachinectlAuth,
    minimal_host_config,
)
from core.setup import l3_sudoers_polkit as l3
from core.setup.l3_sudoers_polkit import (
    PHASE,
    RuleRenderError,
    render_polkit_rule,
    render_sudoers_rule,
)
from core.setup.phase_runner import Identity, PhaseResult, SetupContext

if TYPE_CHECKING:
    from collections.abc import Callable

_FIXTURE = Path(__file__).parent / "fixtures" / "sudoers_rule_v9.golden"


@pytest.fixture(autouse=True)
def _stable_machinectl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the L0 machinectl resolution to the golden's typical value."""
    monkeypatch.setattr(
        l3, "resolve_machinectl_path", lambda _hc: "/usr/bin/machinectl"
    )


@pytest.fixture(autouse=True)
def _stable_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "core.setup.l3_sudoers_polkit.socket.gethostname",
        lambda: "testhost",
    )


def _hc(auth: MachinectlAuth = MachinectlAuth.SUDO) -> HostConfig:
    return minimal_host_config("sandbox", auth)


def _ctx(auth: MachinectlAuth = MachinectlAuth.SUDO) -> SetupContext:
    return SetupContext(host_config=_hc(auth), operator="alice")


# ── golden-file render + zero-quote assertion ────────────────────────────────


def test_render_matches_v9_golden() -> None:
    rendered = render_sudoers_rule(
        "/usr/bin/machinectl", "alice", "testhost", "sandbox"
    )
    assert rendered == _FIXTURE.read_text()


def test_render_has_zero_double_quotes_in_every_cmnd_spec() -> None:
    rendered = render_sudoers_rule(
        "/usr/bin/machinectl", "alice", "testhost", "sandbox"
    )
    # The Cmnd_Spec lines are inlined into the operator's user-spec (no
    # Cmnd_Alias — F-020); they run from the ``NOPASSWD: NOSETENV: \`` header to
    # the end. Assert NONE contains a ``"``.
    cmnd_block = rendered.split("NOPASSWD: NOSETENV: \\\n", 1)[1]
    for line in cmnd_block.splitlines():
        assert '"' not in line, f"Cmnd_Spec contains a quote: {line!r}"


def test_render_enumerates_every_op_once() -> None:
    rendered = render_sudoers_rule(
        "/usr/bin/machinectl", "alice", "testhost", "sandbox"
    )
    for op in Op:
        assert f"dispatch\\ {op.value}" in rendered


def test_no_arg_ops_omit_trailing_glob() -> None:
    rendered = render_sudoers_rule(
        "/usr/bin/machinectl", "alice", "testhost", "sandbox"
    )
    assert "dispatch\\ auth-probe," in rendered
    # No wildcard for no-arg ops (V9 B7 anti-arg-smuggling preserved).
    assert "dispatch\\ auth-probe\\ *" not in rendered
    assert "dispatch\\ compose-up\\ *," in rendered


def test_no_arg_ops_also_grant_exact_check_probe_shape() -> None:
    """G2/F-016 sibling: no-arg ops grant BOTH ``<op>`` and ``<op>\\ --check``.

    L3a probes every op with a trailing ``--check``; for no-arg ops (no
    ``\\ *`` wildcard) the rule must grant the exact ``--check`` shape too, or
    the probe can't match the bare ``<op>`` grant (round-5 fedora: a
    password-operator's L3a got "sudo: a password is required" on
    ``auth-probe``). The ``--check`` grant is exact (no wildcard) so
    arg-smuggling stays denied.
    """
    rendered = render_sudoers_rule(
        "/usr/bin/machinectl", "alice", "testhost", "sandbox"
    )
    for no_arg in ("auth-probe", "compose-ls", "docker-version"):
        assert f"dispatch\\ {no_arg}," in rendered  # exact runtime shape
        assert f"dispatch\\ {no_arg}\\ --check" in rendered  # exact probe shape
        assert f"dispatch\\ {no_arg}\\ *" not in rendered  # never a wildcard
    # Arg-ops' ``\\ *`` already covers ``--check``, so they get NO separate
    # ``--check`` entry.
    assert "dispatch\\ compose-up\\ --check" not in rendered


# ── F-020 multi-operator: no shared Cmnd_Alias namespace ─────────────────────


def test_render_defines_no_cmnd_alias() -> None:
    """F-020: the rule MUST NOT declare a ``Cmnd_Alias`` (a global-namespace
    identifier that collides when a second operator's drop-in coexists). The
    specs are inlined into the operator's user-spec instead.
    """
    rendered = render_sudoers_rule(
        "/usr/bin/machinectl", "alice", "testhost", "sandbox"
    )
    assert "Cmnd_Alias" not in rendered
    # The grant is the operator's own user-spec carrying the inlined cmnd list.
    assert "alice testhost=(root) NOPASSWD: NOSETENV: \\\n" in rendered


def test_two_operators_pass_visudo_without_duplicate_alias(
    tmp_path: Path,
) -> None:
    """F-020 regression: two operators' drop-ins, loaded together, MUST parse
    cleanly — no ``duplicate Cmnd_Alias``. Pre-fix (each drop-in declared
    ``Cmnd_Alias SANDBOX_OPS``) ``visudo -cf`` on the combined file failed with
    exactly that error.
    """
    visudo = shutil.which("visudo")
    if visudo is None:
        pytest.skip("visudo not available on this host")
    alice = render_sudoers_rule("/usr/bin/machinectl", "alice", "testhost", "sandbox")
    bob = render_sudoers_rule("/usr/bin/machinectl", "bob", "testhost", "sandbox")
    # sudo loads every /etc/sudoers.d/* file into ONE policy; concatenation
    # reproduces that (a duplicate Cmnd_Alias across files is the real failure).
    combined = tmp_path / "combined"
    combined.write_text(alice + bob)
    result = subprocess.run(
        [visudo, "-cf", str(combined)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"visudo -cf rejected two coexisting operator drop-ins: "
        f"{result.stdout}{result.stderr}"
    )
    assert "duplicate Cmnd_Alias" not in (result.stdout + result.stderr)


# ── F-004 render-time refusal ────────────────────────────────────────────────


def test_render_refuses_quote_in_cmnd_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``"`` reaching a Cmnd_Spec body MUST raise BEFORE visudo."""
    monkeypatch.setattr(
        l3,
        "_cmnd_specs",
        lambda _m, _u, _op: ['machinectl shell s@.host /bin/bash -c "x"'],
    )
    with pytest.raises(RuleRenderError, match="F-004"):
        render_sudoers_rule(
            "/usr/bin/machinectl", "alice", "testhost", "sandbox"
        )


def test_render_refuses_bad_op_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """An op-name not matching [a-z0-9-]+ MUST raise at render time."""

    class _BadOp:
        value = "evil op'"

    monkeypatch.setattr(l3, "Op", [_BadOp()])
    with pytest.raises(RuleRenderError, match=r"\[a-z0-9-\]"):
        render_sudoers_rule(
            "/usr/bin/machinectl", "alice", "testhost", "sandbox"
        )


# ── POLKIT branch ────────────────────────────────────────────────────────────


def test_polkit_rule_is_action_level() -> None:
    rendered = render_polkit_rule("alice", "sandbox")
    assert 'action.id == "org.freedesktop.machine1.shell"' in rendered
    assert 'subject.user == "alice"' in rendered
    assert 'action.lookup("user") == "sandbox"' in rendered
    assert "polkit.Result.YES" in rendered


def test_probe_polkit_missing_then_after_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "49-sandbox-ai-machinectl.rules"
    monkeypatch.setattr(l3, "_POLKIT_RULE_PATH", target)
    ctx = _ctx(MachinectlAuth.POLKIT)
    result, _ = l3._probe(ctx)
    assert result == PhaseResult.MISSING
    target.write_text(render_polkit_rule("alice", "sandbox"))
    result2, _ = l3._probe(ctx)
    assert result2 == PhaseResult.ALREADY_CORRECT


# ── content-aware probe (SUDO) ───────────────────────────────────────────────


def test_probe_is_content_aware(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    assert_phase_content_aware: Callable[..., None],
) -> None:
    """The SUDO probe must report DRIFT when the Op enum grew (stale rule)."""
    monkeypatch.setattr(l3, "_SUDOERS_DIR", tmp_path)
    ctx = _ctx()
    drop_in = tmp_path / "sandbox-ai-machinectl-alice"
    drop_in.write_text(l3._expected_body(ctx.host_config, "alice"))

    full_ops = list(Op)

    def make_stale() -> None:
        # Simulate a wheel upgrade that added an op: the on-disk rule is now
        # missing the new op's Cmnd_Spec -> byte-different -> DRIFT.
        class _NewOp:
            value = "brand-new-op"

        monkeypatch.setattr(l3, "Op", [*full_ops, _NewOp()])

    assert_phase_content_aware(PHASE, ctx, make_stale)


def test_probe_missing_when_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(l3, "_SUDOERS_DIR", tmp_path)
    result, detail = l3._probe(_ctx())
    assert result == PhaseResult.MISSING
    assert "absent" in detail


# ── act / visudo / install ───────────────────────────────────────────────────


def test_act_sudo_stages_validates_installs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(l3, "_SUDOERS_DIR", tmp_path)
    calls: list[list[str]] = []

    def _fake_run(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[0] == "install":
            Path(argv[-1]).write_text(Path(argv[-2]).read_text())
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(
        "core.setup.l3_sudoers_polkit.subprocess.run", _fake_run
    )
    detail = l3._act(_ctx())
    assert "installed" in detail
    assert calls[0][0] == "visudo"
    assert calls[1][0] == "install"
    assert "0440" in calls[1]


def test_act_polkit_skips_visudo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "49-sandbox-ai-machinectl.rules"
    monkeypatch.setattr(l3, "_POLKIT_RULE_PATH", target)
    calls: list[list[str]] = []

    def _fake_run(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[0] == "install":
            Path(argv[-1]).write_text(Path(argv[-2]).read_text())
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(
        "core.setup.l3_sudoers_polkit.subprocess.run", _fake_run
    )
    l3._act(_ctx(MachinectlAuth.POLKIT))
    assert all(c[0] != "visudo" for c in calls)
    assert "0644" in calls[0]


def test_act_visudo_failure_refuses_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(l3, "_SUDOERS_DIR", tmp_path)
    calls: list[list[str]] = []

    def _fake_run(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[0] == "visudo":
            return subprocess.CompletedProcess(argv, 1, ">>> syntax error", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(
        "core.setup.l3_sudoers_polkit.subprocess.run", _fake_run
    )
    with pytest.raises(RuleRenderError, match="visudo -cf rejected"):
        l3._act(_ctx())
    assert all(c[0] != "install" for c in calls)


# ── reverify + rollback ──────────────────────────────────────────────────────


def test_reverify_true_when_byte_identical(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(l3, "_SUDOERS_DIR", tmp_path)
    ctx = _ctx()
    (tmp_path / "sandbox-ai-machinectl-alice").write_text(
        l3._expected_body(ctx.host_config, "alice")
    )
    assert l3._reverify(ctx) is True


def test_reverify_false_when_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(l3, "_SUDOERS_DIR", tmp_path)
    assert l3._reverify(_ctx()) is False


def test_reverify_false_when_drifted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(l3, "_SUDOERS_DIR", tmp_path)
    (tmp_path / "sandbox-ai-machinectl-alice").write_text("stale")
    assert l3._reverify(_ctx()) is False


def test_rollback_removes_drop_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(l3, "_SUDOERS_DIR", tmp_path)
    drop_in = tmp_path / "sandbox-ai-machinectl-alice"
    drop_in.write_text("anything")
    l3._rollback(_ctx())
    assert not drop_in.exists()


def test_rollback_idempotent_when_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(l3, "_SUDOERS_DIR", tmp_path)
    l3._rollback(_ctx())  # must not raise


# ── PHASE wiring ─────────────────────────────────────────────────────────────


def test_phase_identity_and_graph() -> None:
    assert PHASE.id == "l3"
    assert PHASE.depends_on == ("l7",)
    assert PHASE.identity == Identity.ROOT
    assert PHASE.rollback is l3._rollback
    # no crossing → no sudoers/polkit AUTH GATE → separate-user only.
    assert PHASE.applies_in == frozenset({DockerExecutionMode.SEPARATE_USER})
