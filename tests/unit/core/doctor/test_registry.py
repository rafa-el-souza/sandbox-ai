"""Tests for core.doctor.registry.

Covers ``build_check_registry``, ``topological_sort``, ``run_checks``, and
``run_check_subset`` — the runner surface that wires per-topic check
functions into a single ordered registry.
"""

from __future__ import annotations

import subprocess
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
        assert len(checks) == 34
        ids = [c.id for c in checks]
        assert "sudo" in ids
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
        assert len(checks) == 33

    def test_sudo_check_present_in_sudo_mode(self) -> None:
        from core.doctor import build_check_registry
        from core.host_config import MachinectlAuth

        checks = build_check_registry(MachinectlAuth.SUDO)
        ids = [c.id for c in checks]
        assert "sudo" in ids
        assert len(checks) == 34

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
        assert len(pb_checks) == 11

    def test_image_digests_registered_in_supply_chain(self) -> None:
        from core.doctor import build_check_registry

        checks = build_check_registry()
        image_check = next((c for c in checks if c.id == "image_digests"), None)
        assert image_check is not None
        assert image_check.category == "Supply Chain"
        assert "docker_available" in image_check.depends_on


