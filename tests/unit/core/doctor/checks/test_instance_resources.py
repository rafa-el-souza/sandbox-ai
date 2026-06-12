# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for core.doctor.checks.instance_resources.

Covers the two advisory (WARN-only) per-instance host-capacity checks plus the
``_load_service_limits`` compose-reading helper. Registered instances are seeded
by monkeypatching the shared ``_scan_instance_dirs`` registry scan (string form,
no suppression directives); host CPU/RAM detection is monkeypatched via the
``core.host_resources`` string targets so the checks see deterministic values.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_MODULE = "core.doctor.checks.instance_resources"


def _write_compose(instance_dir: Path, services: dict[str, dict[str, object]]) -> None:
    """Render a minimal ``<instance_dir>/docker/compose.yml`` with the given
    services map (each body a dict of e.g. ``{"cpus": "4.0"}`` / ``mem_limit``)."""
    from ruamel.yaml import YAML

    docker_dir = instance_dir / "docker"
    docker_dir.mkdir(parents=True, exist_ok=True)
    with (docker_dir / "compose.yml").open("w") as f:
        YAML(typ="safe").dump({"services": services}, f)


def test_module_exposes_two_check_functions() -> None:
    from core.doctor.checks import instance_resources

    expected = {"check_host_cpu_capacity", "check_instance_memory_overcommit"}
    assert set(instance_resources.__all__) == expected
    assert callable(instance_resources._load_service_limits)
    assert callable(instance_resources._cpu_offenders)
    assert callable(instance_resources._sum_mem_limits)


def test_public_re_exports_resolve_to_topic_module() -> None:
    import core.doctor as doctor_pkg
    from core.doctor.checks import instance_resources

    for name in instance_resources.__all__:
        assert getattr(doctor_pkg, name) is getattr(instance_resources, name)


def test_registered_in_both_modes() -> None:
    from core.doctor import build_check_registry
    from core.host_config import DockerExecutionMode

    for mode in (DockerExecutionMode.SEPARATE_USER, DockerExecutionMode.OPERATOR_ROOTLESS):
        ids = [c.id for c in build_check_registry(mode)]
        assert "host_cpu_capacity" in ids
        assert "instance_memory_overcommit" in ids


class TestLoadServiceLimits:
    def test_returns_services_map_when_present(self, tmp_path: Path) -> None:
        from core.doctor.checks.instance_resources import _load_service_limits

        inst = tmp_path / "inst"
        _write_compose(inst, {"core": {"cpus": "2.0", "mem_limit": "512m"}})
        result = _load_service_limits(str(inst))
        assert result == {"core": {"cpus": "2.0", "mem_limit": "512m"}}

    def test_skip_signal_when_compose_absent(self, tmp_path: Path) -> None:
        from core.doctor.checks.instance_resources import _ComposeAbsent, _load_service_limits

        inst = tmp_path / "inst"
        inst.mkdir()
        assert isinstance(_load_service_limits(str(inst)), _ComposeAbsent)

    def test_skip_signal_when_compose_unparseable(self, tmp_path: Path) -> None:
        from core.doctor.checks.instance_resources import _ComposeAbsent, _load_service_limits

        inst = tmp_path / "inst"
        (inst / "docker").mkdir(parents=True)
        (inst / "docker" / "compose.yml").write_text("services: [unclosed\n")
        assert isinstance(_load_service_limits(str(inst)), _ComposeAbsent)

    def test_skip_signal_when_top_level_not_mapping(self, tmp_path: Path) -> None:
        from core.doctor.checks.instance_resources import _ComposeAbsent, _load_service_limits

        inst = tmp_path / "inst"
        (inst / "docker").mkdir(parents=True)
        (inst / "docker" / "compose.yml").write_text("- a\n- b\n")
        assert isinstance(_load_service_limits(str(inst)), _ComposeAbsent)

    def test_skip_signal_when_services_not_mapping(self, tmp_path: Path) -> None:
        from core.doctor.checks.instance_resources import _ComposeAbsent, _load_service_limits

        inst = tmp_path / "inst"
        (inst / "docker").mkdir(parents=True)
        (inst / "docker" / "compose.yml").write_text("services: []\n")
        assert isinstance(_load_service_limits(str(inst)), _ComposeAbsent)

    def test_non_dict_service_bodies_dropped(self, tmp_path: Path) -> None:
        from core.doctor.checks.instance_resources import _load_service_limits

        inst = tmp_path / "inst"
        (inst / "docker").mkdir(parents=True)
        (inst / "docker" / "compose.yml").write_text("services:\n  good: {cpus: '1.0'}\n  bad: hello\n")
        result = _load_service_limits(str(inst))
        assert result == {"good": {"cpus": "1.0"}}


class TestHostCpuCapacityCheck:
    def test_warn_when_service_cpus_exceeds_host(self, tmp_path: Path, monkeypatch: Any) -> None:
        from core.doctor.checks.instance_resources import check_host_cpu_capacity

        inst = tmp_path / "myproj"
        _write_compose(inst, {"core": {"cpus": "4.0"}, "dnsdist": {"cpus": "0.5"}})
        monkeypatch.setattr(f"{_MODULE}._scan_instance_dirs", lambda: [str(inst)])
        monkeypatch.setattr(f"{_MODULE}.host_cpu_count", lambda: 2)

        result = check_host_cpu_capacity("sandbox", None)
        assert result.status == "warn"
        assert "myproj/core" in result.detail
        assert "4.0" in result.detail
        assert "2" in result.detail
        # The non-offending service is not named.
        assert "dnsdist" not in result.detail

    def test_pass_when_all_within_host(self, tmp_path: Path, monkeypatch: Any) -> None:
        from core.doctor.checks.instance_resources import check_host_cpu_capacity

        inst = tmp_path / "myproj"
        _write_compose(inst, {"core": {"cpus": "2.0"}, "dnsdist": {"cpus": "0.5"}})
        monkeypatch.setattr(f"{_MODULE}._scan_instance_dirs", lambda: [str(inst)])
        monkeypatch.setattr(f"{_MODULE}.host_cpu_count", lambda: 4)

        result = check_host_cpu_capacity("sandbox", None)
        assert result.status == "pass"
        assert "4" in result.detail

    def test_skips_instance_without_rendered_compose(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        from core.doctor.checks.instance_resources import check_host_cpu_capacity

        # Registered but un-started instance: dir exists, no docker/compose.yml.
        inst = tmp_path / "fresh"
        inst.mkdir()
        monkeypatch.setattr(f"{_MODULE}._scan_instance_dirs", lambda: [str(inst)])
        monkeypatch.setattr(f"{_MODULE}.host_cpu_count", lambda: 1)

        result = check_host_cpu_capacity("sandbox", None)
        assert result.status == "pass"

    def test_unparseable_or_absent_cpus_not_flagged(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        from core.doctor.checks.instance_resources import check_host_cpu_capacity

        inst = tmp_path / "myproj"
        # `nolimit` has no cpus; `weird` has a non-numeric cpus; neither is flagged.
        _write_compose(inst, {"nolimit": {"mem_limit": "1g"}, "weird": {"cpus": "lots"}})
        monkeypatch.setattr(f"{_MODULE}._scan_instance_dirs", lambda: [str(inst)])
        monkeypatch.setattr(f"{_MODULE}.host_cpu_count", lambda: 1)

        result = check_host_cpu_capacity("sandbox", None)
        assert result.status == "pass"

    def test_boolean_cpus_value_not_flagged(self, tmp_path: Path, monkeypatch: Any) -> None:
        from core.doctor.checks.instance_resources import check_host_cpu_capacity

        inst = tmp_path / "myproj"
        # A YAML bool would float() to 1.0 but is not a real cpus request — guard it.
        _write_compose(inst, {"core": {"cpus": True}})
        monkeypatch.setattr(f"{_MODULE}._scan_instance_dirs", lambda: [str(inst)])
        monkeypatch.setattr(f"{_MODULE}.host_cpu_count", lambda: 1)

        result = check_host_cpu_capacity("sandbox", None)
        assert result.status == "pass"

    def test_numeric_cpus_value_flagged(self, tmp_path: Path, monkeypatch: Any) -> None:
        from core.doctor.checks.instance_resources import check_host_cpu_capacity

        inst = tmp_path / "myproj"
        # A bare float (not a quoted string) is still a valid cpus request.
        _write_compose(inst, {"core": {"cpus": 8}})
        monkeypatch.setattr(f"{_MODULE}._scan_instance_dirs", lambda: [str(inst)])
        monkeypatch.setattr(f"{_MODULE}.host_cpu_count", lambda: 2)

        result = check_host_cpu_capacity("sandbox", None)
        assert result.status == "warn"
        assert "myproj/core" in result.detail


class TestInstanceMemoryOvercommitCheck:
    def test_warn_when_summed_mem_exceeds_host(self, tmp_path: Path, monkeypatch: Any) -> None:
        from core.doctor.checks.instance_resources import check_instance_memory_overcommit

        inst = tmp_path / "myproj"
        # 8gb + 8gb = 16gb summed; host RAM 4gb → over-commit.
        _write_compose(inst, {"core": {"mem_limit": "8gb"}, "admin": {"mem_limit": "8gb"}})
        monkeypatch.setattr(f"{_MODULE}._scan_instance_dirs", lambda: [str(inst)])
        monkeypatch.setattr(f"{_MODULE}.host_ram_bytes", lambda: 4 * 1024**3)

        result = check_instance_memory_overcommit("sandbox", None)
        assert result.status == "warn"
        assert "myproj" in result.detail
        assert str(16 * 1024**3) in result.detail
        assert str(4 * 1024**3) in result.detail
        assert "OOM" in result.detail

    def test_pass_when_within_host_ram(self, tmp_path: Path, monkeypatch: Any) -> None:
        from core.doctor.checks.instance_resources import check_instance_memory_overcommit

        inst = tmp_path / "myproj"
        _write_compose(inst, {"core": {"mem_limit": "1gb"}, "admin": {"mem_limit": "512m"}})
        monkeypatch.setattr(f"{_MODULE}._scan_instance_dirs", lambda: [str(inst)])
        monkeypatch.setattr(f"{_MODULE}.host_ram_bytes", lambda: 4 * 1024**3)

        result = check_instance_memory_overcommit("sandbox", None)
        assert result.status == "pass"

    def test_skips_instance_without_rendered_compose(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        from core.doctor.checks.instance_resources import check_instance_memory_overcommit

        inst = tmp_path / "fresh"
        inst.mkdir()
        monkeypatch.setattr(f"{_MODULE}._scan_instance_dirs", lambda: [str(inst)])
        monkeypatch.setattr(f"{_MODULE}.host_ram_bytes", lambda: 1)

        result = check_instance_memory_overcommit("sandbox", None)
        assert result.status == "pass"

    def test_unparseable_or_absent_mem_limit_not_summed(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        from core.doctor.checks.instance_resources import check_instance_memory_overcommit

        inst = tmp_path / "myproj"
        # `nolimit` has no mem_limit; `weird` is non-Docker-size; `flag` is bool —
        # none contribute, so the sum stays 0 ≤ host RAM.
        _write_compose(
            inst,
            {"nolimit": {"cpus": "1.0"}, "weird": {"mem_limit": "huge"}, "flag": {"mem_limit": True}},
        )
        monkeypatch.setattr(f"{_MODULE}._scan_instance_dirs", lambda: [str(inst)])
        monkeypatch.setattr(f"{_MODULE}.host_ram_bytes", lambda: 1)

        result = check_instance_memory_overcommit("sandbox", None)
        assert result.status == "pass"

    def test_integer_mem_limit_bytes_summed(self, tmp_path: Path, monkeypatch: Any) -> None:
        from core.doctor.checks.instance_resources import check_instance_memory_overcommit

        inst = tmp_path / "myproj"
        # A bare int mem_limit is already bytes (parse_docker_size returns it as-is).
        _write_compose(inst, {"core": {"mem_limit": 2048}})
        monkeypatch.setattr(f"{_MODULE}._scan_instance_dirs", lambda: [str(inst)])
        monkeypatch.setattr(f"{_MODULE}.host_ram_bytes", lambda: 1024)

        result = check_instance_memory_overcommit("sandbox", None)
        assert result.status == "warn"
        assert "2048" in result.detail


class TestAdvisoryDoesNotFlipExitContract:
    """The doctor exit contract is ``any(r.status == "fail")`` (cli.main.doctor);
    a WARN from either advisory check must NOT contribute a failure."""

    def test_cpu_warn_is_not_a_failure(self, tmp_path: Path, monkeypatch: Any) -> None:
        from core.doctor.checks.instance_resources import check_host_cpu_capacity

        inst = tmp_path / "myproj"
        _write_compose(inst, {"core": {"cpus": "8.0"}})
        monkeypatch.setattr(f"{_MODULE}._scan_instance_dirs", lambda: [str(inst)])
        monkeypatch.setattr(f"{_MODULE}.host_cpu_count", lambda: 1)

        result = check_host_cpu_capacity("sandbox", None)
        assert result.status == "warn"
        # Mirror the cli exit-contract computation: a WARN yields no failure.
        assert not any(r.status == "fail" for r in [result])

    def test_memory_warn_is_not_a_failure(self, tmp_path: Path, monkeypatch: Any) -> None:
        from core.doctor.checks.instance_resources import check_instance_memory_overcommit

        inst = tmp_path / "myproj"
        _write_compose(inst, {"core": {"mem_limit": "16gb"}})
        monkeypatch.setattr(f"{_MODULE}._scan_instance_dirs", lambda: [str(inst)])
        monkeypatch.setattr(f"{_MODULE}.host_ram_bytes", lambda: 1)

        result = check_instance_memory_overcommit("sandbox", None)
        assert result.status == "warn"
        assert not any(r.status == "fail" for r in [result])
