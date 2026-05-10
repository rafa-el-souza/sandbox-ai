"""Tests for core.doctor.checks.workspace_bridge.

The exhaustive per-check coverage lives in tests/unit/core/test_doctor.py
during the in-flight refactor; this mirror file currently asserts module
surface and import wiring. Per-check test classes will be migrated here
in the §11 finalization step of the refactor.
"""

from __future__ import annotations


def test_module_exposes_eleven_check_functions() -> None:
    from core.doctor.checks import workspace_bridge

    expected = {
        "check_backups_disk_pressure",
        "check_backups_partial_dirs_present",
        "check_dev_in_workspace_bridge_group",
        "check_dev_umask_workspace_friendly",
        "check_helper_image_pulled",
        "check_pre_existing_instance_layout",
        "check_secrets_hydrated_restrictively",
        "check_subuid_resolver_works",
        "check_workspace_bridge_group_exists",
        "check_workspace_home_single_filesystem",
        "check_workspace_path_in_walker_boundary",
    }
    assert set(workspace_bridge.__all__) == expected
    # Per-instance scan helpers live alongside the checks (sole-caller locality).
    assert callable(workspace_bridge._scan_instance_dirs)
    assert callable(workspace_bridge._scan_instance_workspace_paths)
    assert callable(workspace_bridge._read_registry_raw)
    assert callable(workspace_bridge._default_uid_for_path)
    assert callable(workspace_bridge._load_host_settings_or_skip)


def test_public_re_exports_resolve_to_topic_module() -> None:
    import core.doctor as doctor_pkg
    from core.doctor.checks import workspace_bridge

    for name in workspace_bridge.__all__:
        assert getattr(doctor_pkg, name) is getattr(workspace_bridge, name)
    assert doctor_pkg._scan_instance_dirs is workspace_bridge._scan_instance_dirs
    assert doctor_pkg._scan_instance_workspace_paths is workspace_bridge._scan_instance_workspace_paths
    assert doctor_pkg._read_registry_raw is workspace_bridge._read_registry_raw
