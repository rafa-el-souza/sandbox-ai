# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for ``core.setup.l3_sudoers`` (L3 — privilege rule).

Covers: the V9 golden-file render + zero-``"`` assertion across every
``Cmnd_Spec``; the F-004 render-time refusal (a ``"`` in a body / a bad
op-name); the content-aware probe (ALREADY_CORRECT -> DRIFT on a stale Op enum,
driven by the shared ``assert_phase_content_aware`` fixture); visudo-failure
refusal; the rollback removing the
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
from core.setup import l3_sudoers as l3
from core.setup.l3_sudoers import (
    PHASE,
    RuleRenderError,
    render_sudoers_rule,
)
from core.setup.phase_runner import Identity, PhaseResult, SetupContext

if TYPE_CHECKING:
    from collections.abc import Callable

_FIXTURE = Path(__file__).parent / "fixtures" / "sudoers_rule_v9.golden"


@pytest.fixture(autouse=True)
def _stable_systemd_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the L0 launcher resolution to the golden's typical value."""
    monkeypatch.setattr(
        l3, "resolve_systemd_run_path", lambda _hc: "/usr/bin/systemd-run"
    )


@pytest.fixture(autouse=True)
def _stable_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "core.setup.l3_sudoers.socket.gethostname",
        lambda: "testhost",
    )


def _hc(auth: MachinectlAuth = MachinectlAuth.SUDO) -> HostConfig:
    return minimal_host_config("sandbox", auth, DockerExecutionMode.SEPARATE_USER)


def _ctx(auth: MachinectlAuth = MachinectlAuth.SUDO) -> SetupContext:
    return SetupContext(host_config=_hc(auth), operator="alice")


# ── golden-file render + zero-quote assertion ────────────────────────────────


def test_render_matches_v9_golden() -> None:
    rendered = render_sudoers_rule(
        "/usr/bin/systemd-run", "alice", "testhost", "sandbox"
    )
    assert rendered == _FIXTURE.read_text()


def test_render_has_zero_double_quotes_in_every_cmnd_spec() -> None:
    rendered = render_sudoers_rule(
        "/usr/bin/systemd-run", "alice", "testhost", "sandbox"
    )
    # The Cmnd_Spec lines are inlined into the operator's user-spec (no
    # Cmnd_Alias — F-020); they run from the ``NOPASSWD: NOSETENV: \`` header to
    # the end. Assert NONE contains a ``"``.
    cmnd_block = rendered.split("NOPASSWD: NOSETENV: \\\n", 1)[1]
    for line in cmnd_block.splitlines():
        assert '"' not in line, f"Cmnd_Spec contains a quote: {line!r}"


def test_render_enumerates_every_op_once() -> None:
    rendered = render_sudoers_rule(
        "/usr/bin/systemd-run", "alice", "testhost", "sandbox"
    )
    for op in Op:
        assert f"dispatch\\ {op.value}" in rendered


def test_no_arg_ops_omit_trailing_glob() -> None:
    rendered = render_sudoers_rule(
        "/usr/bin/systemd-run", "alice", "testhost", "sandbox"
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
        "/usr/bin/systemd-run", "alice", "testhost", "sandbox"
    )
    for no_arg in ("auth-probe", "compose-ls", "docker-version", "preflight"):
        assert f"dispatch\\ {no_arg}," in rendered  # exact runtime shape
        assert f"dispatch\\ {no_arg}\\ --check" in rendered  # exact probe shape
        assert f"dispatch\\ {no_arg}\\ *" not in rendered  # never a wildcard
    # Arg-ops' ``\\ *`` already covers ``--check``, so they get NO separate
    # ``--check`` entry.
    assert "dispatch\\ compose-up\\ --check" not in rendered


# ── C-009 D4: pipe-only spec, no machinectl operator spec, SSOT no-drift ──────


def test_render_emits_pipe_spec_per_arg_op() -> None:
    """Each arg op's spec is the ``systemd-run --pipe`` argv + trailing ``\\ *``."""
    rendered = render_sudoers_rule(
        "/usr/bin/systemd-run", "alice", "testhost", "sandbox"
    )
    assert (
        "/usr/bin/systemd-run -q --pipe --uid=sandbox /bin/bash -c "
        "/usr/local/libexec/sandbox-ai/dispatch\\ compose-up\\ *" in rendered
    )


def test_render_emits_exactly_one_fwd_arg_spec_at_enum_tail() -> None:
    """C-010: ``fwd`` renders as an ordinary arg-bearing op — one ``\\ *`` spec.

    The streaming op (op 12, the attach ProxyCommand payload) is an arg-bearing
    op at the sudoers layer: a single ``\\ *`` spec covers both the runtime wire
    form (``fwd <inst> --project <P> --ip <IP>``) and the lone ``--check`` probe
    form, exactly like the other arg ops. It needs NO streaming-specific
    renderer treatment (the framed/streaming split lives only in the dispatcher,
    after authorization). It is NOT a no-arg op, so it gets exactly ONE spec
    (the ``\\ *`` form) and never the bare + exact-``--check`` double-spec.
    """
    rendered = render_sudoers_rule(
        "/usr/bin/systemd-run", "alice", "testhost", "sandbox"
    )
    base = "/usr/bin/systemd-run -q --pipe --uid=sandbox /bin/bash -c "
    fwd_arg_spec = f"{base}/usr/local/libexec/sandbox-ai/dispatch\\ fwd\\ *"
    # Exactly one ``fwd`` spec, in the ``\\ *`` arg-op form.
    assert rendered.count("dispatch\\ fwd") == 1
    assert fwd_arg_spec in rendered
    # ``fwd`` is NOT a no-arg op: no bare/exact-``--check`` double-spec.
    assert "dispatch\\ fwd," not in rendered
    assert "dispatch\\ fwd\\ --check" not in rendered
    # Enum-derived ordering: ``fwd`` is the last enum member, so its spec is the
    # final (trailing, comma-less) Cmnd_Spec in the rendered body.
    assert rendered.rstrip().endswith(fwd_arg_spec)
    # The no-arg double-spec set is unchanged: exactly these four no-arg ops.
    assert frozenset(
        {"auth-probe", "compose-ls", "docker-version", "preflight"}
    ) == l3._NO_ARG_OP_NAMES
    assert "fwd" not in l3._NO_ARG_OP_NAMES


def test_render_emits_no_machinectl_operator_spec() -> None:
    """D4: under SUDO the machinectl operator ``Cmnd_Spec`` is GONE entirely.

    Every op crosses via the pipe (``build_invocation`` → ``sudo_pipe_cmd``), so
    a ``machinectl shell <user>@.host`` operator grant would be dead authz. The
    rendered rule must contain no ``machinectl`` token at all.
    """
    rendered = render_sudoers_rule(
        "/usr/bin/systemd-run", "alice", "testhost", "sandbox"
    )
    assert "machinectl" not in rendered
    assert "shell sandbox@.host" not in rendered


def test_render_no_arg_op_pipe_specs_are_exact() -> None:
    """No-arg op gets the two EXACT pipe specs (bare + ``\\ --check``), no glob."""
    rendered = render_sudoers_rule(
        "/usr/bin/systemd-run", "alice", "testhost", "sandbox"
    )
    base = "/usr/bin/systemd-run -q --pipe --uid=sandbox /bin/bash -c "
    assert (
        f"{base}/usr/local/libexec/sandbox-ai/dispatch\\ auth-probe," in rendered
    )
    assert (
        f"{base}/usr/local/libexec/sandbox-ai/dispatch\\ auth-probe\\ --check"
        in rendered
    )
    assert (
        f"{base}/usr/local/libexec/sandbox-ai/dispatch\\ auth-probe\\ *"
        not in rendered
    )


def test_pipe_spec_prefix_matches_build_invocation_no_drift() -> None:
    """D4 SSOT drift meta-test: the rendered spec prefix == ``build_invocation``.

    For every op, the rendered pipe ``Cmnd_Spec`` (modulo the relative→absolute
    launcher substitution + the F-004 backslash-escaping + trailing
    ``\\ *``/exact) MUST equal the argv ``core.dispatch.build_invocation`` builds
    for that op. Both derive from the same ``sudo_pipe_crossing_argv`` /
    ``dispatch_payload`` primitives, so the grant and the crossing provably
    cannot diverge.
    """
    from core.dispatch import build_invocation, sudo_pipe_crossing_argv

    path = "/usr/bin/systemd-run"
    user = "sandbox"
    hc = _hc()
    for op in Op:
        # The grant base: sudo-stripped crossing argv (abspath launcher) +
        # /bin/bash -c + the bare <dispatch> <op> payload, F-004-escaped.
        expected_argv = [
            *sudo_pipe_crossing_argv(path, user),
            "/bin/bash",
            "-c",
            f"/usr/local/libexec/sandbox-ai/dispatch {op.value}",
        ]
        expected_base = " ".join(expected_argv).replace(
            f"dispatch {op.value}", f"dispatch\\ {op.value}"
        )
        specs = l3._cmnd_specs(path, user, op)
        # Every rendered spec for the op starts with the SSOT-derived base.
        for spec in specs:
            assert spec.startswith(expected_base), (op.value, spec)

    # For the no-arg ops (no registry-dependent wire expansion) compare the FULL
    # ``build_invocation`` argv: the leading ``sudo`` is stripped, the relative
    # launcher is abspath'd, and the inner is F-004-escaped — proving the
    # rendered grant is reconstructable from the live invocation primitive.
    for op in (Op.AUTH_PROBE, Op.COMPOSE_LS, Op.DOCKER_VERSION):
        inv = build_invocation(op, [], hc)
        # inv == [sudo, <relative-launcher>, -q, --pipe, --uid=user,
        #         /bin/bash, -c, "<dispatch> <op>"]
        bridged = [path, *inv[2:-1], inv[-1].replace(" ", "\\ ")]
        derived = " ".join(bridged)
        assert l3._cmnd_specs(path, user, op)[0] == derived, op.value


# ── C-009 authz1: injection-deny set on the pipe argv ─────────────────────────


def test_pipe_specs_deny_injection_authz1() -> None:
    """authz1 mirror: only the enumerated per-op argv matches; injections deny.

    The rendered specs are exact strings (plus a trailing ``\\ *`` for arg ops);
    none of the injection shapes appears verbatim, so sudo would never authorize
    them. (visudo accepts the file; runtime matching is per-Cmnd_Spec.)
    """
    rendered = render_sudoers_rule(
        "/usr/bin/systemd-run", "alice", "testhost", "sandbox"
    )
    # A transient-unit-naming flag, an ExecStartPre property, a non-enumerated
    # op, and an arbitrary command must NOT appear as a granted Cmnd_Spec.
    assert "--unit" not in rendered
    assert "--property" not in rendered
    assert "ExecStartPre" not in rendered
    assert "dispatch\\ not-an-op" not in rendered
    assert "/bin/bash -c /bin/sh" not in rendered
    # A trailing extra arg on a no-arg op (auth-probe) is denied: the only
    # auth-probe grants are the bare + exact ``--check`` shapes (no ``\\ *``).
    assert "dispatch\\ auth-probe\\ evil" not in rendered
    assert "dispatch\\ auth-probe\\ *" not in rendered


def test_pipe_specs_pass_visudo(tmp_path: Path) -> None:
    """The rendered pipe-only rule must still pass ``visudo -cf``."""
    visudo = shutil.which("visudo")
    if visudo is None:
        pytest.skip("visudo not available on this host")
    rule = render_sudoers_rule(
        "/usr/bin/systemd-run", "alice", "testhost", "sandbox"
    )
    staged = tmp_path / "staged"
    staged.write_text(rule)
    result = subprocess.run(
        [visudo, "-cf", str(staged)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"{result.stdout}{result.stderr}"


# ── F-020 multi-operator: no shared Cmnd_Alias namespace ─────────────────────


def test_render_defines_no_cmnd_alias() -> None:
    """F-020: the rule MUST NOT declare a ``Cmnd_Alias`` (a global-namespace
    identifier that collides when a second operator's drop-in coexists). The
    specs are inlined into the operator's user-spec instead.
    """
    rendered = render_sudoers_rule(
        "/usr/bin/systemd-run", "alice", "testhost", "sandbox"
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
    alice = render_sudoers_rule("/usr/bin/systemd-run", "alice", "testhost", "sandbox")
    bob = render_sudoers_rule("/usr/bin/systemd-run", "bob", "testhost", "sandbox")
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
            "/usr/bin/systemd-run", "alice", "testhost", "sandbox"
        )


def test_render_refuses_bad_op_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """An op-name not matching [a-z0-9-]+ MUST raise at render time."""

    class _BadOp:
        value = "evil op'"

    monkeypatch.setattr(l3, "Op", [_BadOp()])
    with pytest.raises(RuleRenderError, match=r"\[a-z0-9-\]"):
        render_sudoers_rule(
            "/usr/bin/systemd-run", "alice", "testhost", "sandbox"
        )


@pytest.mark.parametrize(
    "bad_user",
    ["", "has space", "UPPER", "a;b", "--property=x", "user!", "a" * 33],
)
def test_render_refuses_bad_sandbox_user(bad_user: str) -> None:
    """M-1: a ``sandbox_user`` not matching the POSIX grammar MUST raise.

    This is the render-time fail-closed guard (mirrors the op-name gate): a
    space/metacharacter in the ``--uid=<user>`` operand would corrupt the
    rendered rule, even if the ``HostSettings`` field validator were bypassed.
    """
    with pytest.raises(RuleRenderError, match="valid POSIX username"):
        render_sudoers_rule("/usr/bin/systemd-run", "alice", "testhost", bad_user)


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
        "core.setup.l3_sudoers.subprocess.run", _fake_run
    )
    detail = l3._act(_ctx())
    assert "installed" in detail
    assert calls[0][0] == "visudo"
    assert calls[1][0] == "install"
    assert "0440" in calls[1]


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
        "core.setup.l3_sudoers.subprocess.run", _fake_run
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
    # no crossing → no sudoers AUTH GATE → separate-user only.
    assert PHASE.applies_in == frozenset({DockerExecutionMode.SEPARATE_USER})
