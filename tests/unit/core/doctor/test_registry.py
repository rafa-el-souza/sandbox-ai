"""Tests for core.doctor.registry.

Covers ``build_check_registry``, ``topological_sort``, ``run_checks``, and
``run_check_subset`` — the runner surface that wires per-topic check
functions into a single ordered registry.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import patch

import pytest


def test_module_exposes_expected_runner_surface() -> None:
    from core.doctor import registry

    assert set(registry.__all__) == {
        "build_check_registry",
        "run_check_subset",
        "run_checks",
        "topological_sort",
    }


def test_public_re_exports_resolve_to_registry_module() -> None:
    import core.doctor as doctor_pkg
    from core.doctor import registry

    for name in registry.__all__:
        assert getattr(doctor_pkg, name) is getattr(registry, name)


class TestCheckRunner:
    def test_build_check_registry_returns_all_checks(self) -> None:
        from core.doctor import build_check_registry

        checks = build_check_registry()
        assert len(checks) == 38
        ids = [c.id for c in checks]
        assert "sudo" in ids
        assert "tlog" in ids
        assert "machinectl_reachable" in ids
        assert "docker_rootless" in ids
        assert "runsc_runtimeargs" in ids
        assert "compose_project_name_collision" in ids
        assert "legacy_registry_shape" in ids

    def test_topological_sort_respects_dependencies(self) -> None:
        from core.doctor import Check, CheckResult, topological_sort

        def noop(u: str, d: str | None) -> CheckResult:
            return CheckResult(status="pass", name="n", detail="")

        checks = [
            Check(id="c", name="C", category="t", depends_on=["b"], run=noop, remediation=""),
            Check(id="a", name="A", category="t", depends_on=[], run=noop, remediation=""),
            Check(id="b", name="B", category="t", depends_on=["a"], run=noop, remediation=""),
        ]
        sorted_checks = topological_sort(checks)
        ids = [c.id for c in sorted_checks]
        assert ids.index("a") < ids.index("b")
        assert ids.index("b") < ids.index("c")

    def test_runner_cascading_skip(self) -> None:
        from core.doctor import Check, CheckResult, run_checks

        def fail_run(u: str, d: str | None) -> CheckResult:
            return CheckResult(status="fail", name="root", detail="broken")

        def pass_run(u: str, d: str | None) -> CheckResult:
            return CheckResult(status="pass", name="dep", detail="ok")

        checks = [
            Check(id="root", name="Root", category="t", depends_on=[], run=fail_run, remediation="fix root"),
            Check(id="dep1", name="Dep1", category="t", depends_on=["root"], run=pass_run, remediation=""),
            Check(id="dep2", name="Dep2", category="t", depends_on=["dep1"], run=pass_run, remediation=""),
        ]
        results = run_checks(checks, "sandbox", None)
        assert results[0].status == "fail"
        assert results[1].status == "skip"
        assert results[2].status == "skip"

    def test_runner_independent_chain_isolation(self) -> None:
        from core.doctor import Check, CheckResult, run_checks

        def fail_run(u: str, d: str | None) -> CheckResult:
            return CheckResult(status="fail", name="Chain1", detail="broken")

        def pass_run(u: str, d: str | None) -> CheckResult:
            return CheckResult(status="pass", name="Chain2", detail="ok")

        checks = [
            Check(id="c1", name="Chain1", category="a", depends_on=[], run=fail_run, remediation=""),
            Check(id="c1d", name="Chain1Dep", category="a", depends_on=["c1"], run=pass_run, remediation=""),
            Check(id="c2", name="Chain2", category="b", depends_on=[], run=pass_run, remediation=""),
        ]
        results = run_checks(checks, "sandbox", None)
        by_name = {r.name: r for r in results}
        assert by_name["Chain1"].status == "fail"
        assert by_name["Chain1Dep"].status == "skip"
        assert by_name["Chain2"].status == "pass"


class TestRunCheckSubset:
    def test_category_filtering_returns_only_matching_checks(self) -> None:
        from core.doctor import run_check_subset

        results = run_check_subset(["Filesystem"], "sandbox", None)
        assert len(results) == 3
        names = {r.name for r in results}
        assert "setfacl" in names or "setfacl binary" in names

    def test_multiple_categories_returns_union(self) -> None:
        from core.doctor import run_check_subset

        results = run_check_subset(["Filesystem", "Repo Integrity"], "sandbox", None)
        assert len(results) == 5
        names = {r.name for r in results}
        assert "tooling plane" in names or "state dir writable" in names

    def test_cascading_skip_within_subset(self) -> None:
        from core.doctor import CheckResult, run_check_subset

        def fake_setfacl(user: str, distro: str | None) -> CheckResult:
            return CheckResult(status="fail", name="setfacl", detail="not found", category="Filesystem")

        with patch("core.doctor.registry.check_setfacl", fake_setfacl):
            results = run_check_subset(["Filesystem"], "sandbox", None)
            assert len(results) == 3
            statuses = {r.name: r.status for r in results}
            assert statuses["setfacl"] == "fail" or statuses["setfacl binary"] == "fail"
            acl_result = next(r for r in results if "ACL" in r.name)
            assert acl_result.status == "skip"

    def test_empty_category_list_returns_empty(self) -> None:
        from core.doctor import run_check_subset

        results = run_check_subset([], "sandbox", None)
        assert results == []

    def test_exclude_ids_removes_checks(self) -> None:
        from core.doctor import run_check_subset

        results = run_check_subset(
            ["Filesystem"],
            "sandbox",
            None,
            exclude_ids={"ancestor_traverse"},
        )
        names = {r.name for r in results}
        assert "ancestor traverse" not in names
        assert "setfacl binary" in names or "ACL support" in names

    def test_cross_chain_dependency_raises_valueerror(self) -> None:
        from core.doctor import Check, CheckResult

        def noop(u: str, d: str | None) -> CheckResult:
            return CheckResult(status="pass", name="n", detail="")

        fake_checks = [
            Check(
                id="setfacl",
                name="setfacl",
                category="Filesystem",
                depends_on=["sudo"],
                run=noop,
                remediation="",
            ),
            Check(
                id="sudo",
                name="sudo",
                category="Privilege Boundary",
                depends_on=[],
                run=noop,
                remediation="",
            ),
        ]

        with patch("core.doctor.registry.build_check_registry", return_value=fake_checks):
            from core.doctor import run_check_subset

            with pytest.raises(ValueError, match="outside the subset"):
                run_check_subset(["Filesystem"], "sandbox", None)


class TestAclSupportSkipCascade:
    def test_acl_support_skip_cascades(self) -> None:
        from core.doctor import CheckResult, run_check_subset

        def fake_acl_support(user: str, distro: str | None) -> CheckResult:
            return CheckResult(status="fail", name="ACL support", detail="no ACL", category="Filesystem")

        with (
            patch("core.doctor.registry.check_acl_support", fake_acl_support),
            patch(
                "core.doctor.registry.check_setfacl",
                return_value=CheckResult(status="pass", name="setfacl binary", detail="ok", category="Filesystem"),
            ),
        ):
            results = run_check_subset(["Filesystem"], "sandbox", None)
            by_name = {r.name: r for r in results}
            assert by_name["ACL support"].status == "fail"
            assert by_name["ancestor traverse"].status == "skip"


class TestWarnStatus:
    def test_warn_does_not_cascade_skip(self) -> None:
        from core.doctor import Check, CheckResult, run_checks

        def warn_run(u: str, d: str | None) -> CheckResult:
            return CheckResult(status="warn", name="root", detail="advisory")

        def pass_run(u: str, d: str | None) -> CheckResult:
            return CheckResult(status="pass", name="dep", detail="ok")

        checks = [
            Check(id="root", name="Root", category="t", depends_on=[], run=warn_run, remediation=""),
            Check(id="dep1", name="Dep1", category="t", depends_on=["root"], run=pass_run, remediation=""),
        ]
        results = run_checks(checks, "sandbox", None)
        assert results[0].status == "warn"
        assert results[1].status == "pass"

    def test_runsc_runtimeargs_skipped_when_runsc_fails(self) -> None:
        from core.doctor import Check, CheckResult, run_checks

        def fail_runsc(u: str, d: str | None) -> CheckResult:
            return CheckResult(status="fail", name="gVisor runsc", detail="not found")

        def pass_runtimeargs(u: str, d: str | None) -> CheckResult:
            return CheckResult(status="pass", name="runsc runtimeArgs", detail="ok")

        checks = [
            Check(
                id="runsc",
                name="gVisor runsc",
                category="Privilege Boundary",
                depends_on=[],
                run=fail_runsc,
                remediation="",
            ),
            Check(
                id="runsc_runtimeargs",
                name="runsc runtimeArgs",
                category="Privilege Boundary",
                depends_on=["runsc"],
                run=pass_runtimeargs,
                remediation="",
            ),
        ]
        results = run_checks(checks, "sandbox", None)
        assert results[0].status == "fail"
        assert results[1].status == "skip"

    def test_check_host_uds_skipped_when_runsc_failed(self) -> None:
        from core.doctor import Check, CheckResult, run_checks

        def fail_runsc(u: str, d: str | None) -> CheckResult:
            return CheckResult(status="fail", name="gVisor runsc", detail="not found")

        def pass_host_uds(u: str, d: str | None) -> CheckResult:
            return CheckResult(status="pass", name="--host-uds=none", detail="ok")

        checks = [
            Check(
                id="runsc",
                name="gVisor runsc",
                category="Privilege Boundary",
                depends_on=[],
                run=fail_runsc,
                remediation="",
            ),
            Check(
                id="host_uds",
                name="--host-uds=none",
                category="Privilege Boundary",
                depends_on=["runsc"],
                run=pass_host_uds,
                remediation="",
            ),
        ]
        results = run_checks(checks, "sandbox", None)
        assert results[0].status == "fail"
        assert results[1].status == "skip"


class TestPolkitRegistry:
    def test_sudo_check_omitted_in_polkit_mode(self) -> None:
        from core.doctor import build_check_registry
        from core.host_config import MachinectlAuth

        checks = build_check_registry(MachinectlAuth.POLKIT)
        ids = [c.id for c in checks]
        assert "sudo" not in ids
        assert len(checks) == 37

    def test_sudo_check_present_in_sudo_mode(self) -> None:
        from core.doctor import build_check_registry
        from core.host_config import MachinectlAuth

        checks = build_check_registry(MachinectlAuth.SUDO)
        ids = [c.id for c in checks]
        assert "sudo" in ids
        assert len(checks) == 38

    def test_machinectl_reachable_dependency_omits_sudo_in_polkit(self) -> None:
        from core.doctor import build_check_registry
        from core.host_config import MachinectlAuth

        checks = build_check_registry(MachinectlAuth.POLKIT)
        reach = next(c for c in checks if c.id == "machinectl_reachable")
        assert "sudo" not in reach.depends_on
        assert set(reach.depends_on) == {"machinectl", "user_exists", "systemd_machined"}

    def test_machinectl_reachable_dependency_includes_sudo_in_sudo(self) -> None:
        from core.doctor import build_check_registry
        from core.host_config import MachinectlAuth

        checks = build_check_registry(MachinectlAuth.SUDO)
        reach = next(c for c in checks if c.id == "machinectl_reachable")
        assert "sudo" in reach.depends_on

    def test_default_auth_mode_is_sudo(self) -> None:
        from core.doctor import build_check_registry
        from core.host_config import MachinectlAuth

        default_ids = [c.id for c in build_check_registry()]
        sudo_ids = [c.id for c in build_check_registry(MachinectlAuth.SUDO)]
        assert default_ids == sudo_ids

    def test_run_check_subset_forwards_auth_mode(self) -> None:
        from core.doctor import run_check_subset
        from core.host_config import MachinectlAuth

        with patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ):
            results = run_check_subset(
                ["Privilege Boundary"],
                "sandbox",
                None,
                auth_mode=MachinectlAuth.POLKIT,
            )

        names = {r.name for r in results}
        assert "sudo" not in names

    def test_registry_includes_host_uds(self) -> None:
        from core.doctor import build_check_registry

        checks = build_check_registry()
        ids = [c.id for c in checks]
        assert "host_uds" in ids

    def test_privilege_boundary_subset_count(self) -> None:
        from core.doctor import build_check_registry

        checks = build_check_registry()
        pb_checks = [c for c in checks if c.category == "Privilege Boundary"]
        assert len(pb_checks) == 12

    def test_image_digests_registered_in_supply_chain(self) -> None:
        from core.doctor import build_check_registry

        checks = build_check_registry()
        image_check = next((c for c in checks if c.id == "image_digests"), None)
        assert image_check is not None
        assert image_check.category == "Supply Chain"
        assert "docker_available" in image_check.depends_on


# ── C-005 1.4: execution-mode-aware doctor ───────────────────────────────────

_CROSSING_ONLY = {"machinectl_reachable", "systemd_machined", "user_exists", "dispatcher_sha_drift"}


class TestExecutionModeGating:
    """The five crossing-only checks carry ``applies_in=separate-user``; every
    other check stays both-mode (design D2)."""

    def test_crossing_only_checks_gated_to_separate_user(self) -> None:
        from core.doctor import build_check_registry
        from core.host_config import DockerExecutionMode

        checks = {c.id: c for c in build_check_registry()}
        for cid in _CROSSING_ONLY:
            assert checks[cid].applies_in == frozenset({DockerExecutionMode.SEPARATE_USER}), cid

    def test_non_crossing_checks_apply_in_both_modes(self) -> None:
        from core.doctor import build_check_registry
        from core.host_config import DockerExecutionMode

        checks = build_check_registry()
        both = frozenset(DockerExecutionMode)
        for c in checks:
            if c.id not in _CROSSING_ONLY:
                assert c.applies_in == both, c.id

    def test_setup_invariants_stays_both_mode(self) -> None:
        from core.doctor import build_check_registry
        from core.host_config import DockerExecutionMode

        checks = {c.id: c for c in build_check_registry()}
        # setup_invariants branches internally; it is NOT mode-gated out.
        assert checks["setup_invariants"].applies_in == frozenset(DockerExecutionMode)

    def test_runner_mode_skips_excluded_check_not_pass(self) -> None:
        from core.doctor import Check, CheckResult, run_checks
        from core.host_config import DockerExecutionMode

        def should_not_run(u: str, d: str | None) -> CheckResult:
            raise AssertionError("mode-skipped check must not be run")

        checks = [
            Check(
                id="crossing",
                name="Crossing",
                category="t",
                depends_on=[],
                run=should_not_run,
                remediation="",
                applies_in=frozenset({DockerExecutionMode.SEPARATE_USER}),
            ),
        ]
        results = run_checks(checks, "sandbox", None, DockerExecutionMode.OPERATOR_ROOTLESS)
        # A mode-skip is an explicit skip — never a false green.
        assert results[0].status == "skip"
        assert results[0].detail == "skipped (operator-rootless)"

    def test_mode_skip_does_not_cascade_to_dependents(self) -> None:
        """A mode-skipped dep is "not applicable" — it must NOT block its
        dependents (mirrors Phase.applies_in). The docker checks depend on the
        crossing checks in separate-user, but must still RUN in op-rootless."""
        from core.doctor import Check, CheckResult, run_checks
        from core.host_config import DockerExecutionMode

        ran: list[str] = []

        def crossing_run(u: str, d: str | None) -> CheckResult:
            raise AssertionError("crossing check must be mode-skipped")

        def dependent_run(u: str, d: str | None) -> CheckResult:
            ran.append("dependent")
            return CheckResult(status="pass", name="Dependent", detail="ran locally")

        checks = [
            Check(
                id="crossing",
                name="Crossing",
                category="t",
                depends_on=[],
                run=crossing_run,
                remediation="",
                applies_in=frozenset({DockerExecutionMode.SEPARATE_USER}),
            ),
            Check(
                id="dependent",
                name="Dependent",
                category="t",
                depends_on=["crossing"],
                run=dependent_run,
                remediation="",
            ),
        ]
        results = run_checks(checks, "sandbox", None, DockerExecutionMode.OPERATOR_ROOTLESS)
        by_name = {r.name: r for r in results}
        assert by_name["Crossing"].status == "skip"
        assert by_name["Dependent"].status == "pass"
        assert ran == ["dependent"]

    def test_genuine_dependency_failure_still_cascades(self) -> None:
        """A real fail/dependency-skip dep STILL cascade-skips (the pre-existing
        behavior is preserved, distinct from a mode-skip)."""
        from core.doctor import Check, CheckResult, run_checks
        from core.host_config import DockerExecutionMode

        def fail_run(u: str, d: str | None) -> CheckResult:
            return CheckResult(status="fail", name="Root", detail="broken")

        def dep_run(u: str, d: str | None) -> CheckResult:
            return CheckResult(status="pass", name="Dep", detail="ok")

        checks = [
            Check(id="root", name="Root", category="t", depends_on=[], run=fail_run, remediation=""),
            Check(id="dep", name="Dep", category="t", depends_on=["root"], run=dep_run, remediation=""),
        ]
        results = run_checks(checks, "sandbox", None, DockerExecutionMode.SEPARATE_USER)
        by_name = {r.name: r for r in results}
        assert by_name["Root"].status == "fail"
        assert by_name["Dep"].status == "skip"
        assert "requires" in by_name["Dep"].detail

    def test_separate_user_runs_all_crossing_checks(self) -> None:
        """Regression guard: in separate-user no check is mode-skipped."""
        from core.doctor import Check, CheckResult, run_checks
        from core.host_config import DockerExecutionMode

        ran: list[str] = []

        def run_fn(u: str, d: str | None) -> CheckResult:
            ran.append("crossing")
            return CheckResult(status="pass", name="Crossing", detail="ok")

        checks = [
            Check(
                id="crossing",
                name="Crossing",
                category="t",
                depends_on=[],
                run=run_fn,
                remediation="",
                applies_in=frozenset({DockerExecutionMode.SEPARATE_USER}),
            ),
        ]
        results = run_checks(checks, "sandbox", None, DockerExecutionMode.SEPARATE_USER)
        assert results[0].status == "pass"
        assert ran == ["crossing"]

    def test_run_checks_defaults_to_separate_user(self) -> None:
        from core.doctor import Check, CheckResult, run_checks
        from core.host_config import DockerExecutionMode

        def run_fn(u: str, d: str | None) -> CheckResult:
            return CheckResult(status="pass", name="C", detail="ok")

        checks = [
            Check(
                id="c",
                name="C",
                category="t",
                depends_on=[],
                run=run_fn,
                remediation="",
                applies_in=frozenset({DockerExecutionMode.SEPARATE_USER}),
            ),
        ]
        # No mode arg → SEPARATE_USER default → the separate-user check runs.
        results = run_checks(checks, "sandbox", None)
        assert results[0].status == "pass"


class TestRegistryThreadsMode:
    """``build_check_registry(auth, mode)`` partial-binds ``mode`` into each
    check that builds ``minimal_host_config(...)`` so its probe routes locally
    in operator-rootless (the host_config it builds carries the mode)."""

    def test_docker_available_host_config_carries_operator_rootless(self, monkeypatch: Any) -> None:
        import subprocess

        from core.doctor import build_check_registry
        from core.host_config import DockerExecutionMode

        captured: dict[str, Any] = {}

        def capture(op: str, args: Any, host_config: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            captured["host_config"] = host_config
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="24.0.7\n", stderr="")

        monkeypatch.setattr("core.dispatch.invoke", capture)
        checks = {c.id: c for c in build_check_registry(mode=DockerExecutionMode.OPERATOR_ROOTLESS)}
        result = checks["docker_available"].run("sandbox", None)
        assert result.status == "pass"
        assert captured["host_config"].host.docker_execution_mode is DockerExecutionMode.OPERATOR_ROOTLESS

    def test_default_mode_is_separate_user(self, monkeypatch: Any) -> None:
        import subprocess

        from core.doctor import build_check_registry
        from core.host_config import DockerExecutionMode

        captured: dict[str, Any] = {}

        def capture(op: str, args: Any, host_config: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            captured["host_config"] = host_config
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="24.0.7\n", stderr="")

        monkeypatch.setattr("core.dispatch.invoke", capture)
        checks = {c.id: c for c in build_check_registry()}
        checks["docker_available"].run("sandbox", None)
        assert captured["host_config"].host.docker_execution_mode is DockerExecutionMode.SEPARATE_USER


class TestRunCheckSubsetThreadsMode:
    def test_subset_mode_skips_crossing_checks_in_operator_rootless(self) -> None:
        from core.doctor import run_check_subset
        from core.host_config import DockerExecutionMode

        results = run_check_subset(
            ["Privilege Boundary"],
            "sandbox",
            None,
            mode=DockerExecutionMode.OPERATOR_ROOTLESS,
        )
        by_name = {r.name: r for r in results}
        # The crossing-only checks are mode-skipped, not run / not PASS.
        assert by_name["machinectl reachable"].status == "skip"
        assert by_name["machinectl reachable"].detail == "skipped (operator-rootless)"
        assert by_name["unprivileged user"].status == "skip"
        assert by_name["systemd-machined"].status == "skip"

    def test_subset_defaults_to_separate_user(self) -> None:
        from core.doctor import run_check_subset

        # In separate-user the crossing-only user_exists check RUNS (id probe),
        # so it is never mode-skipped. Patch the id subprocess so the result
        # does not depend on a real host user.
        with patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="uid=0\n", stderr=""),
        ):
            results = run_check_subset(["Privilege Boundary"], "sandbox", None)
        # No result carries a mode-skip detail when the default (separate-user)
        # is active — every crossing-only check runs.
        details = [r.detail for r in results]
        assert "skipped (operator-rootless)" not in details
        assert "skipped (separate-user)" not in details
        # The user_exists check actually ran (its result name is "user exists").
        assert any(r.name == "user exists" for r in results)


