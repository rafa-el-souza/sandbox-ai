# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for :mod:`core.setup.phase_runner`.

Covers: the phase-module name predicate; ``discover_phases`` via a synthetic
package seam (empty / N-modules / matching-but-missing-``PHASE``);
``order_phases`` topological scheduling (unknown dep + cycle errors);
``route`` for all three identities; the plan pass (probe-only, no mutations);
the apply pass (already-correct skip, drift→act→reverify, FAIL on raising
act / raising reverify / falsy reverify, rollback fired vs. absent, rollback
itself failing, BLOCKED_BY transitive propagation); and the
``assert_phase_content_aware`` conftest fixture (consumed via the fixture
mechanism, never imported).
"""

from __future__ import annotations

import pwd
import subprocess
import sys
from types import ModuleType
from typing import TYPE_CHECKING, NoReturn

import pytest
from core.exceptions import SandboxExecutionError
from core.host_config import (
    DockerExecutionMode,
    MachinectlAuth,
    machinectl_cmd,
    minimal_host_config,
    pipe_cmd,
)
from core.setup.phase_runner import (
    Identity,
    Phase,
    PhaseApplyOutcome,
    PhaseDependencyError,
    PhaseDiscoveryError,
    PhasePlanOutcome,
    PhaseResult,
    SandboxUserNotYetCreated,
    SetupContext,
    _is_phase_module_name,
    daemon_owner_crossing,
    daemon_owner_user,
    discover_phases,
    order_phases,
    probe_sandbox_pw_or_missing,
    resolve_sandbox_pw,
    route,
    run_apply_pass,
    run_plan_pass,
    wait_user_manager_ready,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    SyntheticPkgFactory = Callable[[dict[str, str]], ModuleType]
    ContentAwareAssertion = Callable[
        [Phase, SetupContext, Callable[[], None]], None
    ]


# ── helpers ──────────────────────────────────────────────────────────────────


def _ctx(
    user: str = "sandboxuser",
    auth: MachinectlAuth = MachinectlAuth.SUDO,
    mode: DockerExecutionMode = DockerExecutionMode.SEPARATE_USER,
    operator: str = "op",
) -> SetupContext:
    return SetupContext(
        host_config=minimal_host_config(user, auth, mode), operator=operator
    )


def _phase(
    pid: str,
    *,
    identity: Identity = Identity.ROOT,
    probe_result: PhaseResult = PhaseResult.ALREADY_CORRECT,
    depends_on: tuple[str, ...] = (),
    act: Callable[[SetupContext], str] | None = None,
    reverify: Callable[[SetupContext], bool] | None = None,
    rollback: Callable[[SetupContext], None] | None = None,
    probe: Callable[[SetupContext], tuple[PhaseResult, str]] | None = None,
    applies_in: frozenset[DockerExecutionMode] | None = None,
) -> Phase:
    def _default_probe(_c: SetupContext) -> tuple[PhaseResult, str]:
        return probe_result, f"{pid} probed {probe_result}"

    return Phase(
        id=pid,
        name=f"phase {pid}",
        identity=identity,
        probe=probe if probe is not None else _default_probe,
        act=act if act is not None else (lambda _c: f"{pid} acted"),
        reverify=reverify if reverify is not None else (lambda _c: True),
        depends_on=depends_on,
        rollback=rollback,
        **({} if applies_in is None else {"applies_in": applies_in}),
    )


# ── _is_phase_module_name ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("l0_identity", True),
        ("l1_kernel", True),
        ("l6a_runsc", True),
        ("l65_dispatcher", True),
        ("l3a_per_op_probe", True),
        ("phase_runner", False),
        ("__init__", False),
        ("l_oops", False),  # no digit run
        ("l0", False),  # no underscore tail
        ("l0a", False),  # 'a' suffix but no underscore tail
        ("l0_", False),  # underscore but empty rest
        ("xl0_foo", False),  # does not start with l
        ("la_foo", False),  # no digits after l
        ("l0b_foo", False),  # only single 'a' sub-phase suffix allowed
    ],
)
def test_is_phase_module_name(name: str, expected: bool) -> None:
    assert _is_phase_module_name(name) is expected


# ── discover_phases (synthetic package seam) ─────────────────────────────────


@pytest.fixture
def synthetic_pkg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> SyntheticPkgFactory:
    """Factory: build an importable package on disk with the given modules.

    ``modules`` maps submodule-name → source text. Returns the imported
    package object suitable to pass as ``discover_phases(package=...)``.
    """
    import importlib
    import textwrap

    counter = {"n": 0}
    # tmp_path is unique per test; folding its basename into the package name
    # guarantees a globally-unique import name so importlib.import_module never
    # returns a *different* test's cached synthetic package.
    unique = tmp_path.name.replace("-", "_")

    def _build(modules: dict[str, str]) -> ModuleType:
        counter["n"] += 1
        pkg_name = f"_synthetic_setup_{unique}_{counter['n']}"
        pkg_dir = tmp_path / pkg_name
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        for mod_name, src in modules.items():
            (pkg_dir / f"{mod_name}.py").write_text(textwrap.dedent(src))
        monkeypatch.syspath_prepend(str(tmp_path))
        # Purge any cached package/submodule entries, then invalidate the
        # path-finder's directory-listing cache so the just-written package
        # dir is visible to pkgutil.iter_modules.
        for key in list(sys.modules):
            if key == pkg_name or key.startswith(f"{pkg_name}."):
                del sys.modules[key]
        importlib.invalidate_caches()
        return importlib.import_module(pkg_name)

    return _build


_PHASE_SRC = """
    from core.setup.phase_runner import Phase, Identity, PhaseResult

    def _probe(hc):
        return PhaseResult.ALREADY_CORRECT, "ok"

    PHASE = Phase(
        id="{pid}",
        name="{pid}",
        identity=Identity.ROOT,
        probe=_probe,
        act=lambda hc: "acted",
        reverify=lambda hc: True,
    )
"""


def test_discover_phases_empty_package(synthetic_pkg: SyntheticPkgFactory) -> None:
    pkg = synthetic_pkg({})
    assert discover_phases(pkg) == []


def test_discover_phases_skips_non_phase_modules(
    synthetic_pkg: SyntheticPkgFactory,
) -> None:
    pkg = synthetic_pkg(
        {
            "helpers": "X = 1\n",
            "phase_runner_local": "Y = 2\n",
        }
    )
    assert discover_phases(pkg) == []


def test_discover_phases_collects_phase_objects(
    synthetic_pkg: SyntheticPkgFactory,
) -> None:
    pkg = synthetic_pkg(
        {
            "l0_identity": _PHASE_SRC.format(pid="l0"),
            "l6a_runsc": _PHASE_SRC.format(pid="l6a"),
            "notes": "Z = 3\n",  # non-matching, skipped
        }
    )
    found = sorted(p.id for p in discover_phases(pkg))
    assert found == ["l0", "l6a"]


def test_discover_phases_missing_PHASE_raises(
    synthetic_pkg: SyntheticPkgFactory,
) -> None:
    pkg = synthetic_pkg({"l9_broken": "VALUE = 1\n"})  # matches pattern, no PHASE
    with pytest.raises(PhaseDiscoveryError, match="does not export"):
        discover_phases(pkg)


def test_discover_phases_PHASE_wrong_type_raises(
    synthetic_pkg: SyntheticPkgFactory,
) -> None:
    pkg = synthetic_pkg({"l8_bad": "PHASE = 'not a Phase'\n"})
    with pytest.raises(PhaseDiscoveryError, match="does not export"):
        discover_phases(pkg)


def test_discover_phases_default_is_real_core_setup() -> None:
    # Production default: the real core.setup package, now populated with the
    # twelve wired phase modules (l2a split out of l1 — the uid-scoped Delegate
    # drop-in needs L2 to have created the sandbox user first; L4 was deleted in
    # round-9/F-021 — the per-operator tree is `init`'s artifact, created as the
    # operator, never by root-running setup). This is the cross-module
    # integration check — it fails if any phase module drifts its id/depends_on
    # out of the canonical graph, or a module stops exporting a valid PHASE.
    discovered = discover_phases()
    assert sorted(p.id for p in discovered) == sorted(
        ["l0", "l1", "l2", "l2a", "l5", "l6", "l6a", "l65", "l7", "l3", "l3a", "l8"]
    )
    # The depends_on edges across all twelve modules must topologically resolve
    # to the single canonical setup chain — l2a sits between l2 and l5 (after
    # the user exists, before rootless dockerd needs cgroup delegation).
    assert [p.id for p in order_phases(discovered)] == [
        "l0", "l1", "l2", "l2a", "l5", "l6", "l6a", "l65", "l7", "l3", "l3a", "l8"
    ]


# ── order_phases ─────────────────────────────────────────────────────────────


def test_order_phases_topological_and_deterministic() -> None:
    a = _phase("a")
    b = _phase("b", depends_on=("a",))
    c = _phase("c", depends_on=("a",))
    d = _phase("d", depends_on=("b", "c"))
    ordered = [p.id for p in order_phases([d, c, b, a])]
    assert ordered[0] == "a"
    assert ordered[-1] == "d"
    assert ordered.index("b") < ordered.index("d")
    assert ordered.index("c") < ordered.index("d")
    # Deterministic id-sorted tie-break: b before c.
    assert ordered.index("b") < ordered.index("c")


def test_order_phases_unknown_dependency() -> None:
    with pytest.raises(PhaseDependencyError, match="unknown phase 'ghost'"):
        order_phases([_phase("a", depends_on=("ghost",))])


def test_order_phases_cycle() -> None:
    a = _phase("a", depends_on=("b",))
    b = _phase("b", depends_on=("a",))
    with pytest.raises(PhaseDependencyError, match="dependency cycle"):
        order_phases([a, b])


def test_order_phases_external_dep_strict_raises_by_default() -> None:
    """A single-phase subset with an out-of-list dep raises under the default.

    This is the --update-runsc crash shape (round-5 fedora 12.3): filtering the
    phase list to just ``l6a`` (which ``depends_on=("l6",)``) and ordering it
    strictly raises ``PhaseDependencyError: ... unknown phase 'l6'``.
    """
    l6a = _phase("l6a", depends_on=("l6",))
    with pytest.raises(PhaseDependencyError, match="unknown phase 'l6'"):
        order_phases([l6a])


def test_order_phases_external_dep_allowed_treated_as_satisfied() -> None:
    """``allow_external_deps=True`` treats an out-of-list dep as satisfied.

    The subset-run path (--update-runsc): the external ``l6`` edge is dangling
    in the filtered list but known-satisfied on the converged host, so ordering
    must succeed and place the single phase.
    """
    l6a = _phase("l6a", depends_on=("l6",))
    ordered = order_phases([l6a], allow_external_deps=True)
    assert [p.id for p in ordered] == ["l6a"]


def test_order_phases_external_dep_allowed_still_orders_internal_edges() -> None:
    """External deps are satisfied, but in-list edges still order correctly."""
    a = _phase("a", depends_on=("ext",))
    b = _phase("b", depends_on=("a",))
    ordered = [p.id for p in order_phases([b, a], allow_external_deps=True)]
    assert ordered == ["a", "b"]


def test_order_phases_external_dep_allowed_still_detects_cycle() -> None:
    """Allowing external deps must NOT mask a genuine in-list cycle."""
    a = _phase("a", depends_on=("b", "ext"))
    b = _phase("b", depends_on=("a",))
    with pytest.raises(PhaseDependencyError, match="dependency cycle"):
        order_phases([a, b], allow_external_deps=True)


# ── route ────────────────────────────────────────────────────────────────────


def test_route_root_is_empty_prefix() -> None:
    assert route(Identity.ROOT, _ctx()) == []


def test_route_operator_is_pipe_cmd() -> None:
    ctx = SetupContext(
        host_config=minimal_host_config("sb", MachinectlAuth.SUDO),
        operator="alice",
    )
    assert route(Identity.OPERATOR, ctx) == pipe_cmd("alice")


def test_route_sandbox_is_machinectl_cmd_sudo() -> None:
    ctx = SetupContext(
        host_config=minimal_host_config("sbuser", MachinectlAuth.SUDO),
        operator="alice",
    )
    assert route(Identity.SANDBOX, ctx) == machinectl_cmd("sbuser")


# ── run_plan_pass ────────────────────────────────────────────────────────────


def test_plan_pass_probes_only_no_mutation() -> None:
    acted: list[str] = []

    def _spy_act(_c: SetupContext) -> str:
        acted.append("ran")
        return "x"

    phases = [
        _phase("a", probe_result=PhaseResult.ALREADY_CORRECT),
        _phase("b", probe_result=PhaseResult.MISSING, depends_on=("a",), act=_spy_act),
        _phase("c", probe_result=PhaseResult.DRIFT, depends_on=("b",)),
    ]
    out = run_plan_pass(phases, _ctx())
    assert acted == []  # no act ever invoked in plan pass
    assert [o.phase_id for o in out] == ["a", "b", "c"]  # dependency order
    assert all(isinstance(o, PhasePlanOutcome) for o in out)
    assert out[0].result == PhaseResult.ALREADY_CORRECT
    assert out[1].result == PhaseResult.MISSING
    assert out[2].result == PhaseResult.DRIFT
    assert "probed" in out[0].detail


def _raising_probe_phase(
    pid: str, *, depends_on: tuple[str, ...] = ()
) -> Phase:
    """A phase whose probe raises (the unwrapped-probe B1 class)."""

    def _probe(_c: SetupContext) -> tuple[PhaseResult, str]:
        raise KeyError("getpwnam(): name not found: 'sandbox'")

    return Phase(
        id=pid,
        name=f"phase {pid}",
        identity=Identity.ROOT,
        probe=_probe,
        act=lambda _c: f"{pid} acted",
        reverify=lambda _c: True,
        depends_on=depends_on,
    )


def test_plan_pass_probe_raises_is_fail_not_propagated() -> None:
    """A raising probe must NOT crash run_plan_pass; record FAIL, continue.

    Regression for the B1 class (an unguarded ``pwd.getpwnam`` KeyError in a
    probe crashing ``sandbox setup --dry-run``).
    """
    phases = [
        _raising_probe_phase("a"),
        _phase("b", probe_result=PhaseResult.MISSING, depends_on=("a",)),
    ]
    out = run_plan_pass(phases, _ctx())
    assert [o.phase_id for o in out] == ["a", "b"]
    assert out[0].result == PhaseResult.FAIL
    assert "probe raised" in out[0].detail
    assert "name not found" in out[0].detail
    # The plan pass does not block dependents (it is probe-only); b still
    # probes normally — the point is the pass did not crash.
    assert out[1].result == PhaseResult.MISSING


# ── run_apply_pass ───────────────────────────────────────────────────────────


def test_apply_pass_already_correct_skips_act() -> None:
    def _boom(_c: SetupContext) -> str:
        raise AssertionError("act must not run for an already-correct phase")

    out = run_apply_pass(
        [_phase("a", probe_result=PhaseResult.ALREADY_CORRECT, act=_boom)], _ctx()
    )
    assert out[0].result == PhaseResult.ALREADY_CORRECT
    assert out[0].reverified is False
    assert isinstance(out[0], PhaseApplyOutcome)


def test_apply_pass_skipped_probe_result_is_passed_through() -> None:
    out = run_apply_pass([_phase("a", probe_result=PhaseResult.SKIPPED)], _ctx())
    assert out[0].result == PhaseResult.SKIPPED
    assert out[0].reverified is False


def test_apply_pass_drift_acts_and_reverifies() -> None:
    seq: list[str] = []

    def _act(_c: SetupContext) -> str:
        seq.append("act")
        return "did the thing"

    def _reverify(_c: SetupContext) -> bool:
        seq.append("reverify")
        return True

    out = run_apply_pass(
        [_phase("a", probe_result=PhaseResult.MISSING, act=_act, reverify=_reverify)],
        _ctx(),
    )
    assert seq == ["act", "reverify"]
    assert out[0].result == PhaseResult.ALREADY_CORRECT
    assert out[0].reverified is True
    assert out[0].detail == "did the thing"


def test_apply_pass_act_raises_is_fail_no_rollback() -> None:
    def _act(_c: SetupContext) -> str:
        raise RuntimeError("act blew up")

    out = run_apply_pass(
        [_phase("a", probe_result=PhaseResult.DRIFT, act=_act)], _ctx()
    )
    assert out[0].result == PhaseResult.FAIL
    assert "act blew up" in out[0].detail
    assert out[0].reverified is False


def test_apply_pass_reverify_false_is_fail() -> None:
    out = run_apply_pass(
        [
            _phase(
                "a",
                probe_result=PhaseResult.DRIFT,
                reverify=lambda _h: False,
            )
        ],
        _ctx(),
    )
    assert out[0].result == PhaseResult.FAIL
    assert "did not confirm convergence" in out[0].detail


def test_apply_pass_reverify_raises_is_fail() -> None:
    def _reverify(_c: SetupContext) -> bool:
        raise RuntimeError("reverify exploded")

    out = run_apply_pass(
        [_phase("a", probe_result=PhaseResult.DRIFT, reverify=_reverify)], _ctx()
    )
    assert out[0].result == PhaseResult.FAIL
    assert "reverify exploded" in out[0].detail


def test_apply_pass_rollback_fires_on_failure() -> None:
    rolled: list[str] = []

    def _act(_c: SetupContext) -> str:
        raise RuntimeError("L3a probe rejected")

    def _rollback(_c: SetupContext) -> None:
        rolled.append("rm drop-in")

    out = run_apply_pass(
        [
            _phase(
                "l3a",
                probe_result=PhaseResult.DRIFT,
                act=_act,
                rollback=_rollback,
            )
        ],
        _ctx(),
    )
    assert rolled == ["rm drop-in"]
    assert out[0].result == PhaseResult.FAIL
    assert "rolled back" in out[0].detail


def test_apply_pass_rollback_itself_failing_is_surfaced() -> None:
    def _act(_c: SetupContext) -> str:
        raise RuntimeError("primary failure")

    def _rollback(_c: SetupContext) -> None:
        raise RuntimeError("rollback broke too")

    out = run_apply_pass(
        [
            _phase(
                "l3a",
                probe_result=PhaseResult.DRIFT,
                act=_act,
                rollback=_rollback,
            )
        ],
        _ctx(),
    )
    assert out[0].result == PhaseResult.FAIL
    assert "rollback also failed: rollback broke too" in out[0].detail


def test_apply_pass_blocked_by_transitive_propagation() -> None:
    def _fail_act(_c: SetupContext) -> str:
        raise RuntimeError("a failed")

    a = _phase("a", probe_result=PhaseResult.MISSING, act=_fail_act)
    b = _phase("b", probe_result=PhaseResult.MISSING, depends_on=("a",))
    c = _phase("c", probe_result=PhaseResult.MISSING, depends_on=("b",))
    independent = _phase("z", probe_result=PhaseResult.MISSING)

    out = {o.phase_id: o for o in run_apply_pass([a, b, c, independent], _ctx())}
    assert out["a"].result == PhaseResult.FAIL
    assert out["b"].result == PhaseResult.BLOCKED_BY
    assert "blocked by failed phase 'a'" in out["b"].detail
    # Transitive: c depends on b (itself blocked) → also blocked.
    assert out["c"].result == PhaseResult.BLOCKED_BY
    assert "blocked by failed phase 'b'" in out["c"].detail
    # Independent phase still runs and succeeds despite a's failure.
    assert out["z"].result == PhaseResult.ALREADY_CORRECT
    assert out["z"].reverified is True


def test_apply_pass_conflict_is_refusal_never_acts() -> None:
    calls: list[str] = []

    def _act(_c: SetupContext) -> str:
        calls.append("act")
        return "should not happen"

    def _reverify(_c: SetupContext) -> bool:
        calls.append("reverify")
        return True

    out = run_apply_pass(
        [
            _phase(
                "a",
                probe_result=PhaseResult.CONFLICT,
                act=_act,
                reverify=_reverify,
            )
        ],
        _ctx(),
    )
    assert calls == []  # neither act nor reverify ever invoked on a refusal
    assert out[0].result == PhaseResult.CONFLICT
    assert out[0].reverified is False
    assert "probed conflict" in out[0].detail
    assert isinstance(out[0], PhaseApplyOutcome)


def test_apply_pass_conflict_blocks_dependents() -> None:
    a = _phase("a", probe_result=PhaseResult.CONFLICT)
    b = _phase("b", probe_result=PhaseResult.MISSING, depends_on=("a",))
    c = _phase("c", probe_result=PhaseResult.MISSING, depends_on=("b",))

    out = {o.phase_id: o for o in run_apply_pass([a, b, c], _ctx())}
    assert out["a"].result == PhaseResult.CONFLICT
    assert out["b"].result == PhaseResult.BLOCKED_BY
    assert "blocked by failed phase 'a'" in out["b"].detail
    # Transitive: c depends on b (itself blocked) → also blocked.
    assert out["c"].result == PhaseResult.BLOCKED_BY
    assert "blocked by failed phase 'b'" in out["c"].detail


def test_apply_pass_conflict_does_not_fire_rollback() -> None:
    rolled: list[str] = []

    def _rollback(_c: SetupContext) -> None:
        rolled.append("rolled")

    out = run_apply_pass(
        [
            _phase(
                "a",
                probe_result=PhaseResult.CONFLICT,
                rollback=_rollback,
            )
        ],
        _ctx(),
    )
    assert rolled == []  # a clean refusal mutated nothing; nothing to roll back
    assert out[0].result == PhaseResult.CONFLICT
    assert out[0].reverified is False


def test_apply_pass_probe_raises_is_fail_and_blocks_dependents() -> None:
    """A raising probe in the apply pass is a FAIL; dependents BLOCKED_BY.

    Regression for the B1 class: a raising ``phase.probe`` must not crash
    ``sandbox setup`` (apply); the runner records FAIL, the transitive
    dependents are BLOCKED_BY, and an independent phase still runs.
    """
    a = _raising_probe_phase("a")
    b = _phase("b", probe_result=PhaseResult.MISSING, depends_on=("a",))
    c = _phase("c", probe_result=PhaseResult.MISSING, depends_on=("b",))
    independent = _phase("z", probe_result=PhaseResult.MISSING)

    out = {
        o.phase_id: o
        for o in run_apply_pass([a, b, c, independent], _ctx())
    }
    assert out["a"].result == PhaseResult.FAIL
    assert "probe raised" in out["a"].detail
    assert "name not found" in out["a"].detail
    assert out["a"].reverified is False
    assert out["b"].result == PhaseResult.BLOCKED_BY
    assert "blocked by failed phase 'a'" in out["b"].detail
    assert out["c"].result == PhaseResult.BLOCKED_BY
    assert "blocked by failed phase 'b'" in out["c"].detail
    # Independent phase still runs despite a's probe raising.
    assert out["z"].result == PhaseResult.ALREADY_CORRECT
    assert out["z"].reverified is True


def test_apply_pass_probe_raises_does_not_fire_rollback() -> None:
    rolled: list[str] = []

    def _probe(_c: SetupContext) -> tuple[PhaseResult, str]:
        raise RuntimeError("probe blew up")

    def _rollback(_c: SetupContext) -> None:
        rolled.append("rolled")

    phase = Phase(
        id="l3a",
        name="phase l3a",
        identity=Identity.ROOT,
        probe=_probe,
        act=lambda _c: "acted",
        reverify=lambda _c: True,
        rollback=_rollback,
    )
    out = run_apply_pass([phase], _ctx())
    assert rolled == []  # a probe mutated nothing; nothing to roll back
    assert out[0].result == PhaseResult.FAIL
    assert "probe raised: probe blew up" in out[0].detail


# ── shared sandbox-user getpwnam guard (design D10 / B1-class) ───────────────


def _fake_pw(uid: int, home: str) -> pwd.struct_passwd:
    # A real struct_passwd so isinstance(..., pwd.struct_passwd) discrimination
    # (the documented call-site contract) is exercised faithfully.
    return pwd.struct_passwd(
        ("sandboxuser", "x", uid, uid, "", home, "/bin/bash")
    )


def test_resolve_sandbox_pw_returns_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pwd.getpwnam", lambda _n: _fake_pw(4242, "/home/sb"))
    pw = resolve_sandbox_pw(_ctx().host_config)
    assert pw.pw_uid == 4242
    assert pw.pw_dir == "/home/sb"


def test_resolve_sandbox_pw_raises_typed_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(_n: str) -> object:
        raise KeyError("getpwnam(): name not found: 'sandboxuser'")

    monkeypatch.setattr("pwd.getpwnam", _boom)
    with pytest.raises(SandboxUserNotYetCreated, match="does not exist yet"):
        resolve_sandbox_pw(_ctx().host_config)


def test_probe_sandbox_pw_or_missing_returns_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pwd.getpwnam", lambda _n: _fake_pw(7, "/home/x"))
    result = probe_sandbox_pw_or_missing(_ctx().host_config)
    assert isinstance(result, pwd.struct_passwd)
    assert result.pw_uid == 7


def test_probe_sandbox_pw_or_missing_returns_missing_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(_n: str) -> object:
        raise KeyError("name not found")

    monkeypatch.setattr("pwd.getpwnam", _boom)
    result = probe_sandbox_pw_or_missing(_ctx().host_config)
    assert not isinstance(result, pwd.struct_passwd)
    res, detail = result
    assert res == PhaseResult.MISSING
    assert "does not exist yet" in detail


def test_sandbox_user_not_yet_created_is_a_keyerror() -> None:
    # It must remain a KeyError subclass so an unguarded `except KeyError`
    # at a call site still catches it (defense in depth).
    assert issubclass(SandboxUserNotYetCreated, KeyError)


# ── wait_user_manager_ready (shared L5/L6 settle gate, F-014/E2a) ─────────────


def test_wait_user_manager_ready_polls_target_uid_root_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate queries ``user@<uid>.service`` root-side (no boundary cross)."""
    monkeypatch.setattr("pwd.getpwnam", lambda _n: _fake_pw(4242, "/home/sb"))
    calls: list[list[str]] = []

    def _run(
        _self: object, cmd: list[str], **_kw: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("core.executor.Executor.run", _run)
    wait_user_manager_ready("sandboxuser")
    assert len(calls) == 1
    inner = calls[0][-1]
    # Targets the resolved uid's manager unit, in a bounded retry loop, and is
    # a plain root-side systemctl query — NOT a machinectl/systemd-run crossing.
    assert "systemctl is-active user@4242.service" in inner
    assert "seq 1 30" in inner
    assert "machinectl" not in " ".join(calls[0])
    assert "systemd-run" not in " ".join(calls[0])


def test_wait_user_manager_ready_raises_when_never_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manager that never becomes active surfaces as a raised error.

    The Executor's default ``check=True`` raises on the loop's ``exit 1``; the
    gate must let that propagate (→ phase-runner FAIL) rather than swallow it.
    """
    monkeypatch.setattr("pwd.getpwnam", lambda _n: _fake_pw(4242, "/home/sb"))

    def _boom(
        _self: object, _cmd: list[str], **_kw: object
    ) -> subprocess.CompletedProcess[str]:
        raise SandboxExecutionError("user@4242.service did not become active")

    monkeypatch.setattr("core.executor.Executor.run", _boom)
    with pytest.raises(SandboxExecutionError, match="did not become active"):
        wait_user_manager_ready("sandboxuser")


def test_wait_user_manager_ready_attempts_is_parametrized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``attempts`` is a parameter (a test seam), not a hardcoded constant."""
    monkeypatch.setattr("pwd.getpwnam", lambda _n: _fake_pw(7, "/home/x"))
    captured: list[str] = []

    def _run(
        _self: object, cmd: list[str], **_kw: object
    ) -> subprocess.CompletedProcess[str]:
        captured.append(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("core.executor.Executor.run", _run)
    wait_user_manager_ready("x", attempts=5)
    assert "seq 1 5" in captured[0]


# ── assert_phase_content_aware conftest fixture (consumed, not imported) ──────


def test_content_aware_fixture_accepts_a_compliant_probe(
    assert_phase_content_aware: ContentAwareAssertion,
) -> None:
    state = {"stale": False}

    def _probe(_c: SetupContext) -> tuple[PhaseResult, str]:
        if state["stale"]:
            return PhaseResult.DRIFT, "source changed under us"
        return PhaseResult.ALREADY_CORRECT, "matches source"

    phase = Phase(
        id="cc",
        name="content-aware",
        identity=Identity.ROOT,
        probe=_probe,
        act=lambda _h: "acted",
        reverify=lambda _h: True,
    )
    assert_phase_content_aware(phase, _ctx(), lambda: state.__setitem__("stale", True))


def test_content_aware_fixture_rejects_a_file_exists_only_probe(
    assert_phase_content_aware: ContentAwareAssertion,
) -> None:
    # A naive probe that always reports ALREADY_CORRECT (file-exists-only)
    # must be caught by the fixture: it never reports DRIFT after make_stale.
    phase = _phase("naive", probe_result=PhaseResult.ALREADY_CORRECT)
    with pytest.raises(AssertionError, match="must report DRIFT"):
        assert_phase_content_aware(phase, _ctx(), lambda: None)


# ── applies_in mode-gating (Phase field + both-pass skip emission) ────────────


def test_applies_in_defaults_to_all_modes() -> None:
    """A phase built with no ``applies_in`` opts into every execution mode."""
    phase = _phase("a")
    assert phase.applies_in == frozenset(DockerExecutionMode)
    assert DockerExecutionMode.SEPARATE_USER in phase.applies_in
    assert DockerExecutionMode.OPERATOR_ROOTLESS in phase.applies_in


def test_default_phase_runs_in_both_modes() -> None:
    """A default (all-modes) phase is probed in separate-user AND rootless."""
    for mode in DockerExecutionMode:
        probed: list[str] = []

        def _probe(
            _c: SetupContext, _p: list[str] = probed
        ) -> tuple[PhaseResult, str]:
            _p.append("probed")
            return PhaseResult.ALREADY_CORRECT, "ok"

        phase = _phase("a", probe=_probe)
        out = run_plan_pass([phase], _ctx(mode=mode))
        assert probed == ["probed"]
        assert out[0].result == PhaseResult.ALREADY_CORRECT


def _explode(_c: SetupContext) -> NoReturn:
    raise AssertionError("callback must not run for a mode-skipped phase")


def _separate_user_only_phase(
    pid: str, *, depends_on: tuple[str, ...] = ()
) -> Phase:
    """A phase pinned to separate-user mode whose every callback explodes.

    If the runner runs ANY callback while in operator-rootless mode, the test
    fails loudly — proving the mode-skip path never invokes probe/act/reverify.
    """
    return Phase(
        id=pid,
        name=f"phase {pid}",
        identity=Identity.ROOT,
        probe=_explode,
        act=_explode,
        reverify=_explode,
        depends_on=depends_on,
        applies_in=frozenset({DockerExecutionMode.SEPARATE_USER}),
    )


def test_plan_pass_mode_skips_excluded_phase() -> None:
    """A separate-user-only phase is SKIPPED (probe never called) in rootless."""
    phase = _separate_user_only_phase("a")
    out = run_plan_pass([phase], _ctx(mode=DockerExecutionMode.OPERATOR_ROOTLESS))
    assert out[0].result == PhaseResult.SKIPPED
    assert out[0].detail == "skipped (operator-rootless)"
    assert isinstance(out[0], PhasePlanOutcome)


def test_apply_pass_mode_skips_excluded_phase() -> None:
    """Same phase SKIPPED in the apply pass — act/reverify never called."""
    phase = _separate_user_only_phase("a")
    out = run_apply_pass([phase], _ctx(mode=DockerExecutionMode.OPERATOR_ROOTLESS))
    assert out[0].result == PhaseResult.SKIPPED
    assert out[0].detail == "skipped (operator-rootless)"
    assert out[0].reverified is False
    assert isinstance(out[0], PhaseApplyOutcome)


def test_excluded_phase_runs_normally_in_its_mode() -> None:
    """The same separate-user-only phase runs (probe called) in separate-user."""
    probed: list[str] = []

    def _probe(_c: SetupContext) -> tuple[PhaseResult, str]:
        probed.append("probed")
        return PhaseResult.ALREADY_CORRECT, "ok"

    phase = Phase(
        id="a",
        name="phase a",
        identity=Identity.ROOT,
        probe=_probe,
        act=lambda _c: "acted",
        reverify=lambda _c: True,
        applies_in=frozenset({DockerExecutionMode.SEPARATE_USER}),
    )
    out = run_plan_pass([phase], _ctx(mode=DockerExecutionMode.SEPARATE_USER))
    assert probed == ["probed"]
    assert out[0].result == PhaseResult.ALREADY_CORRECT


def test_apply_pass_mode_skip_does_not_block_dependents() -> None:
    """A phase depending on a mode-skipped phase still runs (not BLOCKED_BY).

    Mirrors operator-rootless: l65 is mode-skipped, but a phase depending on
    it must still run — a mode-skip is dependency-satisfied, never a failure.
    """
    a = _separate_user_only_phase("a")
    b = _phase("b", probe_result=PhaseResult.MISSING, depends_on=("a",))

    out = {
        o.phase_id: o
        for o in run_apply_pass(
            [a, b], _ctx(mode=DockerExecutionMode.OPERATOR_ROOTLESS)
        )
    }
    assert out["a"].result == PhaseResult.SKIPPED
    # B is NOT blocked by the mode-skipped A — it acts and reverifies.
    assert out["b"].result == PhaseResult.ALREADY_CORRECT
    assert out["b"].reverified is True


# ── crossing-only phases skip in operator-rootless (M2 real-phase drive) ──────


# The four crossing-only phases mode-gated to separate-user (M2): no dispatcher
# binary, no sudoers AUTH GATE, no per-op probe, no fresh-session
# re-probe in operator-rootless. Driving the REAL discovered phase set proves
# the declarative ``applies_in`` skip reaches the runner end-to-end.
_CROSSING_ONLY_IDS = frozenset({"l65", "l3", "l3a", "l8"})


def _real_crossing_only_phases() -> list[Phase]:
    """The real discovered l65/l3/l3a/l8 phases (their inter-deps preserved).

    Filtered from ``discover_phases()`` so the assertions bind to the SHIPPED
    ``PHASE`` objects, not a synthetic stand-in. ``l3a``→``l3``→``l8`` edges are
    internal to this subset; ``l65``→``l6a`` and ``l3``→``l7`` are external and
    satisfied via ``allow_external_deps`` in the passes below.
    """
    return [p for p in discover_phases() if p.id in _CROSSING_ONLY_IDS]


def test_crossing_only_phases_are_separate_user_only() -> None:
    """Each shipped crossing-only phase opts out of operator-rootless."""
    phases = {p.id: p for p in _real_crossing_only_phases()}
    assert set(phases) == _CROSSING_ONLY_IDS
    for phase in phases.values():
        assert phase.applies_in == frozenset(
            {DockerExecutionMode.SEPARATE_USER}
        )


def test_real_crossing_only_phases_skip_in_operator_rootless_plan() -> None:
    """Plan pass over the real phases SKIPS l65/l3/l3a/l8 in operator-rootless.

    The mode-skip fires BEFORE any probe, so none of their host-touching probes
    (``resolve_machinectl_path``, manifest/source-bundle reads) run — proving the
    "no boundary rule, no dispatcher" end state declaratively.
    """
    phases = _real_crossing_only_phases()
    out = {
        o.phase_id: o
        for o in run_plan_pass(
            phases,
            _ctx(mode=DockerExecutionMode.OPERATOR_ROOTLESS),
            allow_external_deps=True,
        )
    }
    assert set(out) == _CROSSING_ONLY_IDS
    for pid in _CROSSING_ONLY_IDS:
        assert out[pid].result == PhaseResult.SKIPPED
        assert out[pid].detail == "skipped (operator-rootless)"


def test_real_crossing_only_phases_skip_in_operator_rootless_apply() -> None:
    """Apply pass SKIPS the same four — their acts (compile/install/probe) never
    run in operator-rootless, so no host mutation or crossing is attempted."""
    phases = _real_crossing_only_phases()
    out = {
        o.phase_id: o
        for o in run_apply_pass(
            phases,
            _ctx(mode=DockerExecutionMode.OPERATOR_ROOTLESS),
            allow_external_deps=True,
        )
    }
    assert set(out) == _CROSSING_ONLY_IDS
    for pid in _CROSSING_ONLY_IDS:
        assert out[pid].result == PhaseResult.SKIPPED
        assert out[pid].reverified is False


def test_real_crossing_only_phases_run_in_separate_user_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SAME four real phases are NOT skipped in separate-user mode.

    Their probes run (deterministically MISSING on a fresh tree: absent drop-in
    / absent dispatcher manifest / verification-only l3a/l8) — the point is that
    none come back SKIPPED, i.e. the mode gate is inactive in separate-user.
    ``resolve_systemd_run_path`` is pinned so l3's probe is host-independent.
    """
    monkeypatch.setattr(
        "core.setup.l3_sudoers.resolve_systemd_run_path",
        lambda _hc: "/usr/bin/systemd-run",
    )
    phases = _real_crossing_only_phases()
    out = {
        o.phase_id: o
        for o in run_plan_pass(
            phases,
            _ctx(mode=DockerExecutionMode.SEPARATE_USER),
            allow_external_deps=True,
        )
    }
    assert set(out) == _CROSSING_ONLY_IDS
    for pid in _CROSSING_ONLY_IDS:
        assert out[pid].result != PhaseResult.SKIPPED


# ── daemon_owner_user / daemon_owner_crossing (M4 shared contract) ────────────


def test_daemon_owner_user_separate_user_is_sandbox_user() -> None:
    ctx = _ctx(user="sbuser", mode=DockerExecutionMode.SEPARATE_USER)
    assert daemon_owner_user(ctx) == "sbuser"


def test_daemon_owner_user_operator_rootless_is_operator() -> None:
    ctx = _ctx(
        user="sbuser",
        mode=DockerExecutionMode.OPERATOR_ROOTLESS,
        operator="alice",
    )
    assert daemon_owner_user(ctx) == "alice"


def test_daemon_owner_crossing_separate_user_is_machinectl_cmd() -> None:
    """separate-user crossing equals machinectl_cmd(...) byte-for-byte."""
    ctx = _ctx(user="sbuser", auth=MachinectlAuth.SUDO)
    assert daemon_owner_crossing(ctx) == machinectl_cmd("sbuser")


def test_daemon_owner_crossing_operator_rootless_injects_session_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """operator-rootless crossing is a LOCAL ``env …`` prefix that re-injects the
    operator's user-session environment (no sudo drop) — finding 8.11.

    The sterile Executor scrubs everything but PATH, so rootless docker +
    ``systemctl --user`` need HOME / XDG_RUNTIME_DIR / DBUS_SESSION_BUS_ADDRESS /
    DOCKER_HOST injected explicitly (else ``dockerd-rootless-setuptool.sh`` fails on
    HOME and ``docker`` hits the rootful socket). There is NO ``sudo``/machinectl —
    setup already runs as the operator.
    """
    monkeypatch.setattr(
        "core.setup.phase_runner.pwd.getpwnam",
        lambda _n: _fake_pw(5000, "/home/alice"),
    )
    ctx = _ctx(
        user="sbuser",
        mode=DockerExecutionMode.OPERATOR_ROOTLESS,
        operator="alice",
    )
    assert daemon_owner_crossing(ctx) == [
        "env",
        "HOME=/home/alice",
        "XDG_RUNTIME_DIR=/run/user/5000",
        "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/5000/bus",
        "DOCKER_HOST=unix:///run/user/5000/docker.sock",
    ]
    # No privilege drop / no boundary crossing in op-rootless.
    assert "sudo" not in daemon_owner_crossing(ctx)
    assert "machinectl" not in daemon_owner_crossing(ctx)
