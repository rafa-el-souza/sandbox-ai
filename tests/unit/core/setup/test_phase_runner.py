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

import sys
from types import ModuleType
from typing import TYPE_CHECKING

import pytest
from core.host_config import MachinectlAuth, machinectl_cmd, minimal_host_config, pipe_cmd
from core.setup.phase_runner import (
    Identity,
    Phase,
    PhaseApplyOutcome,
    PhaseDependencyError,
    PhaseDiscoveryError,
    PhasePlanOutcome,
    PhaseResult,
    _is_phase_module_name,
    discover_phases,
    order_phases,
    route,
    run_apply_pass,
    run_plan_pass,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from core.host_config import HostConfig

    SyntheticPkgFactory = Callable[[dict[str, str]], ModuleType]
    ContentAwareAssertion = Callable[
        [Phase, HostConfig, Callable[[], None]], None
    ]


# ── helpers ──────────────────────────────────────────────────────────────────


def _hc() -> HostConfig:
    return minimal_host_config("sandboxuser", MachinectlAuth.SUDO)


def _phase(
    pid: str,
    *,
    identity: Identity = Identity.ROOT,
    probe_result: PhaseResult = PhaseResult.ALREADY_CORRECT,
    depends_on: tuple[str, ...] = (),
    act: Callable[[HostConfig], str] | None = None,
    reverify: Callable[[HostConfig], bool] | None = None,
    rollback: Callable[[HostConfig], None] | None = None,
) -> Phase:
    def _probe(_hcfg: HostConfig) -> tuple[PhaseResult, str]:
        return probe_result, f"{pid} probed {probe_result}"

    return Phase(
        id=pid,
        name=f"phase {pid}",
        identity=identity,
        probe=_probe,
        act=act if act is not None else (lambda _h: f"{pid} acted"),
        reverify=reverify if reverify is not None else (lambda _h: True),
        depends_on=depends_on,
        rollback=rollback,
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
    # Production default: the real core.setup package. No lN_* phase modules
    # exist yet (this milestone is "complete but unwired"), so the result is
    # empty — but the call must succeed against the real package object.
    assert discover_phases() == []


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


# ── route ────────────────────────────────────────────────────────────────────


def test_route_root_is_empty_prefix() -> None:
    assert route(Identity.ROOT, _hc(), "op", "sb") == []


def test_route_operator_is_pipe_cmd() -> None:
    assert route(Identity.OPERATOR, _hc(), "alice", "sb") == pipe_cmd("alice")


def test_route_sandbox_is_machinectl_cmd_sudo() -> None:
    hc = minimal_host_config("sb", MachinectlAuth.SUDO)
    assert route(Identity.SANDBOX, hc, "alice", "sbuser") == machinectl_cmd(
        "sbuser", MachinectlAuth.SUDO
    )


def test_route_sandbox_is_machinectl_cmd_polkit() -> None:
    hc = minimal_host_config("sb", MachinectlAuth.POLKIT)
    assert route(Identity.SANDBOX, hc, "alice", "sbuser") == machinectl_cmd(
        "sbuser", MachinectlAuth.POLKIT
    )


# ── run_plan_pass ────────────────────────────────────────────────────────────


def test_plan_pass_probes_only_no_mutation() -> None:
    acted: list[str] = []

    def _spy_act(_h: HostConfig) -> str:
        acted.append("ran")
        return "x"

    phases = [
        _phase("a", probe_result=PhaseResult.ALREADY_CORRECT),
        _phase("b", probe_result=PhaseResult.MISSING, depends_on=("a",), act=_spy_act),
        _phase("c", probe_result=PhaseResult.DRIFT, depends_on=("b",)),
    ]
    out = run_plan_pass(phases, _hc())
    assert acted == []  # no act ever invoked in plan pass
    assert [o.phase_id for o in out] == ["a", "b", "c"]  # dependency order
    assert all(isinstance(o, PhasePlanOutcome) for o in out)
    assert out[0].result == PhaseResult.ALREADY_CORRECT
    assert out[1].result == PhaseResult.MISSING
    assert out[2].result == PhaseResult.DRIFT
    assert "probed" in out[0].detail


# ── run_apply_pass ───────────────────────────────────────────────────────────


def test_apply_pass_already_correct_skips_act() -> None:
    def _boom(_h: HostConfig) -> str:
        raise AssertionError("act must not run for an already-correct phase")

    out = run_apply_pass(
        [_phase("a", probe_result=PhaseResult.ALREADY_CORRECT, act=_boom)], _hc()
    )
    assert out[0].result == PhaseResult.ALREADY_CORRECT
    assert out[0].reverified is False
    assert isinstance(out[0], PhaseApplyOutcome)


def test_apply_pass_skipped_probe_result_is_passed_through() -> None:
    out = run_apply_pass([_phase("a", probe_result=PhaseResult.SKIPPED)], _hc())
    assert out[0].result == PhaseResult.SKIPPED
    assert out[0].reverified is False


def test_apply_pass_drift_acts_and_reverifies() -> None:
    seq: list[str] = []

    def _act(_h: HostConfig) -> str:
        seq.append("act")
        return "did the thing"

    def _reverify(_h: HostConfig) -> bool:
        seq.append("reverify")
        return True

    out = run_apply_pass(
        [_phase("a", probe_result=PhaseResult.MISSING, act=_act, reverify=_reverify)],
        _hc(),
    )
    assert seq == ["act", "reverify"]
    assert out[0].result == PhaseResult.ALREADY_CORRECT
    assert out[0].reverified is True
    assert out[0].detail == "did the thing"


def test_apply_pass_act_raises_is_fail_no_rollback() -> None:
    def _act(_h: HostConfig) -> str:
        raise RuntimeError("act blew up")

    out = run_apply_pass(
        [_phase("a", probe_result=PhaseResult.DRIFT, act=_act)], _hc()
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
        _hc(),
    )
    assert out[0].result == PhaseResult.FAIL
    assert "did not confirm convergence" in out[0].detail


def test_apply_pass_reverify_raises_is_fail() -> None:
    def _reverify(_h: HostConfig) -> bool:
        raise RuntimeError("reverify exploded")

    out = run_apply_pass(
        [_phase("a", probe_result=PhaseResult.DRIFT, reverify=_reverify)], _hc()
    )
    assert out[0].result == PhaseResult.FAIL
    assert "reverify exploded" in out[0].detail


def test_apply_pass_rollback_fires_on_failure() -> None:
    rolled: list[str] = []

    def _act(_h: HostConfig) -> str:
        raise RuntimeError("L3a probe rejected")

    def _rollback(_h: HostConfig) -> None:
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
        _hc(),
    )
    assert rolled == ["rm drop-in"]
    assert out[0].result == PhaseResult.FAIL
    assert "rolled back" in out[0].detail


def test_apply_pass_rollback_itself_failing_is_surfaced() -> None:
    def _act(_h: HostConfig) -> str:
        raise RuntimeError("primary failure")

    def _rollback(_h: HostConfig) -> None:
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
        _hc(),
    )
    assert out[0].result == PhaseResult.FAIL
    assert "rollback also failed: rollback broke too" in out[0].detail


def test_apply_pass_blocked_by_transitive_propagation() -> None:
    def _fail_act(_h: HostConfig) -> str:
        raise RuntimeError("a failed")

    a = _phase("a", probe_result=PhaseResult.MISSING, act=_fail_act)
    b = _phase("b", probe_result=PhaseResult.MISSING, depends_on=("a",))
    c = _phase("c", probe_result=PhaseResult.MISSING, depends_on=("b",))
    independent = _phase("z", probe_result=PhaseResult.MISSING)

    out = {o.phase_id: o for o in run_apply_pass([a, b, c, independent], _hc())}
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

    def _act(_h: HostConfig) -> str:
        calls.append("act")
        return "should not happen"

    def _reverify(_h: HostConfig) -> bool:
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
        _hc(),
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

    out = {o.phase_id: o for o in run_apply_pass([a, b, c], _hc())}
    assert out["a"].result == PhaseResult.CONFLICT
    assert out["b"].result == PhaseResult.BLOCKED_BY
    assert "blocked by failed phase 'a'" in out["b"].detail
    # Transitive: c depends on b (itself blocked) → also blocked.
    assert out["c"].result == PhaseResult.BLOCKED_BY
    assert "blocked by failed phase 'b'" in out["c"].detail


def test_apply_pass_conflict_does_not_fire_rollback() -> None:
    rolled: list[str] = []

    def _rollback(_h: HostConfig) -> None:
        rolled.append("rolled")

    out = run_apply_pass(
        [
            _phase(
                "a",
                probe_result=PhaseResult.CONFLICT,
                rollback=_rollback,
            )
        ],
        _hc(),
    )
    assert rolled == []  # a clean refusal mutated nothing; nothing to roll back
    assert out[0].result == PhaseResult.CONFLICT
    assert out[0].reverified is False


# ── assert_phase_content_aware conftest fixture (consumed, not imported) ──────


def test_content_aware_fixture_accepts_a_compliant_probe(
    assert_phase_content_aware: ContentAwareAssertion,
) -> None:
    state = {"stale": False}

    def _probe(_h: HostConfig) -> tuple[PhaseResult, str]:
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
    assert_phase_content_aware(phase, _hc(), lambda: state.__setitem__("stale", True))


def test_content_aware_fixture_rejects_a_file_exists_only_probe(
    assert_phase_content_aware: ContentAwareAssertion,
) -> None:
    # A naive probe that always reports ALREADY_CORRECT (file-exists-only)
    # must be caught by the fixture: it never reports DRIFT after make_stale.
    phase = _phase("naive", probe_result=PhaseResult.ALREADY_CORRECT)
    with pytest.raises(AssertionError, match="must report DRIFT"):
        assert_phase_content_aware(phase, _hc(), lambda: None)
