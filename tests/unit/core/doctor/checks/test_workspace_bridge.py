# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for core.doctor.checks.workspace_bridge.

Covers the 11 workspace-bridge checks plus the per-instance scan helpers
(``scan_instance_dirs``, ``_scan_instance_workspace_paths``) that live in
this module per sole-caller locality.
"""

from __future__ import annotations

import os
from typing import Any


def _stub_marker(
    monkeypatch: Any,
    *,
    user: str = "claude-sandbox",
    mode: Any = None,
    workspace_bridge_group: str = "sb-ws",
) -> None:
    """Stub ``HostConfig.from_marker`` so the workspace-bridge checks read a
    setup-determined host config (the runtime path post-D-B) instead of toml.

    The marker already carries the execution mode and per-operator bridge name,
    so the check functions consume it directly with no mode overlay.
    """
    from core.host_config import (
        DEFAULT_PROVISIONING_MODE,
        HostConfig,
        HostSettings,
    )

    resolved_mode = DEFAULT_PROVISIONING_MODE if mode is None else mode
    cfg = HostConfig(
        host=HostSettings(
            docker_unprivileged_user=user,
            docker_execution_mode=resolved_mode,
            workspace_bridge_group=workspace_bridge_group,
        )
    )
    monkeypatch.setattr(
        "core.doctor.checks.workspace_bridge.HostConfig.from_marker",
        classmethod(lambda cls, operator: cfg),
    )


def test_module_exposes_ten_check_functions() -> None:
    from core.doctor.checks import workspace_bridge

    expected = {
        "check_backups_disk_pressure",
        "check_backups_partial_dirs_present",
        "check_dev_in_workspace_bridge_group",
        "check_dev_umask_workspace_friendly",
        "check_pre_existing_instance_layout",
        "check_secrets_hydrated_restrictively",
        "check_subuid_resolver_works",
        "check_workspace_bridge_group_exists",
        "check_workspace_home_single_filesystem",
        "check_workspace_path_in_walker_boundary",
    }
    assert set(workspace_bridge.__all__) == expected
    assert callable(workspace_bridge.scan_instance_dirs)
    assert callable(workspace_bridge._scan_instance_workspace_paths)
    assert callable(workspace_bridge.read_registry_raw)
    assert callable(workspace_bridge._default_uid_for_path)
    assert callable(workspace_bridge._load_host_settings_or_skip)


def test_public_re_exports_resolve_to_topic_module() -> None:
    import core.doctor as doctor_pkg
    from core.doctor.checks import workspace_bridge

    for name in workspace_bridge.__all__:
        assert getattr(doctor_pkg, name) is getattr(workspace_bridge, name)


class TestScanInstanceDirs:
    def test_returns_empty_when_state_missing(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor.checks.workspace_bridge import scan_instance_dirs

        assert scan_instance_dirs() == []

    def test_returns_empty_on_corrupt_json(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor.checks.workspace_bridge import scan_instance_dirs

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "instances.json").write_text("{not json")
        assert scan_instance_dirs() == []

    def test_returns_empty_when_instances_field_wrong_shape(self, isolated_sandbox_ai_home: Any) -> None:
        import json as _json

        from core.doctor.checks.workspace_bridge import scan_instance_dirs

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "instances.json").write_text(_json.dumps({"instances": []}))
        assert scan_instance_dirs() == []

    def test_returns_registered_instance_dirs(self, isolated_sandbox_ai_home: Any, tmp_path: Any) -> None:
        """Iterates the name-keyed registry; yields each entry's instance_dir
        when it exists on disk. Drops non-dict entries and missing dirs."""
        import json as _json

        present = tmp_path / "instances" / "myproj"
        present.mkdir(parents=True)
        missing = tmp_path / "instances" / "missing"

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "instances.json").write_text(
            _json.dumps(
                {
                    "myproj": {"instance_dir": str(present), "created_at": "2026-05-07T00:00:00Z"},
                    "gone": {"instance_dir": str(missing), "created_at": "2026-05-07T00:00:00Z"},
                    "garbage": "not-a-dict",
                }
            )
        )
        from core.doctor.checks.workspace_bridge import scan_instance_dirs

        assert scan_instance_dirs() == [str(present)]

    def test_returns_empty_when_top_level_not_dict(self, isolated_sandbox_ai_home: Any) -> None:
        import json as _json

        from core.doctor.checks.workspace_bridge import scan_instance_dirs

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "instances.json").write_text(_json.dumps([1, 2, 3]))
        assert scan_instance_dirs() == []


class TestScanInstanceWorkspacePaths:
    def test_skips_missing_sandbox_toml(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor.checks.workspace_bridge import _scan_instance_workspace_paths

        monkeypatch.setattr("core.doctor.checks.workspace_bridge.scan_instance_dirs", lambda: ["/no/such/dir"])
        assert _scan_instance_workspace_paths() == []

    def test_skips_unparseable_toml(self, isolated_sandbox_ai_home: Any, monkeypatch: Any, tmp_path: Any) -> None:
        from core.doctor.checks.workspace_bridge import _scan_instance_workspace_paths

        inst = tmp_path / "inst"
        inst.mkdir()
        (inst / "sandbox.toml").write_text("garbage = =")
        monkeypatch.setattr("core.doctor.checks.workspace_bridge.scan_instance_dirs", lambda: [str(inst)])
        assert _scan_instance_workspace_paths() == []

    def test_skips_non_dict_workspaces_block(
        self, isolated_sandbox_ai_home: Any, monkeypatch: Any, tmp_path: Any
    ) -> None:
        from core.doctor.checks.workspace_bridge import _scan_instance_workspace_paths

        inst = tmp_path / "inst"
        inst.mkdir()
        (inst / "sandbox.toml").write_text("workspaces = []\n")
        monkeypatch.setattr("core.doctor.checks.workspace_bridge.scan_instance_dirs", lambda: [str(inst)])
        assert _scan_instance_workspace_paths() == []

    def test_yields_each_workspace(self, isolated_sandbox_ai_home: Any, monkeypatch: Any, tmp_path: Any) -> None:
        from core.doctor.checks.workspace_bridge import _scan_instance_workspace_paths

        inst = tmp_path / "myinst"
        inst.mkdir()
        (inst / "sandbox.toml").write_text(
            '[workspaces.main]\nbootstrap_mode = "empty"\npath = "/p1"\n'
            '[workspaces.scratch]\nbootstrap_mode = "empty"\npath = "/p2"\n'
        )
        monkeypatch.setattr("core.doctor.checks.workspace_bridge.scan_instance_dirs", lambda: [str(inst)])
        result = sorted(_scan_instance_workspace_paths())
        assert result == [(str(inst), "main", "/p1"), (str(inst), "scratch", "/p2")]

    def test_skips_scalar_workspace_body_keeps_iterating(
        self, isolated_sandbox_ai_home: Any, monkeypatch: Any, tmp_path: Any
    ) -> None:
        # `workspaces` is a dict, but one entry's body is a scalar rather than a
        # table — the `if isinstance(body, dict)` false branch must skip it and
        # the loop must continue to yield the well-formed entry (377->376).
        from core.doctor.checks.workspace_bridge import _scan_instance_workspace_paths

        inst = tmp_path / "inst"
        inst.mkdir()
        (inst / "sandbox.toml").write_text(
            "[workspaces]\nscalar_ws = \"not-a-table\"\n"
            '[workspaces.main]\nbootstrap_mode = "empty"\npath = "/p1"\n'
        )
        monkeypatch.setattr("core.doctor.checks.workspace_bridge.scan_instance_dirs", lambda: [str(inst)])
        assert _scan_instance_workspace_paths() == [(str(inst), "main", "/p1")]

    def test_skips_workspace_table_without_string_path(
        self, isolated_sandbox_ai_home: Any, monkeypatch: Any, tmp_path: Any
    ) -> None:
        # A dict body whose `path` is absent (and one whose `path` is non-str)
        # must be dropped via the `if isinstance(path, str)` false branch
        # (379->376), while a sibling with a real string path still yields.
        from core.doctor.checks.workspace_bridge import _scan_instance_workspace_paths

        inst = tmp_path / "inst"
        inst.mkdir()
        (inst / "sandbox.toml").write_text(
            '[workspaces.nopath]\nbootstrap_mode = "empty"\n'
            "[workspaces.intpath]\npath = 123\n"
            '[workspaces.main]\nbootstrap_mode = "empty"\npath = "/p1"\n'
        )
        monkeypatch.setattr("core.doctor.checks.workspace_bridge.scan_instance_dirs", lambda: [str(inst)])
        assert _scan_instance_workspace_paths() == [(str(inst), "main", "/p1")]


class TestCheckWorkspaceBridgeGroupExists:
    def test_skip_when_no_host_config(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import check_workspace_bridge_group_exists

        result = check_workspace_bridge_group_exists("u", None)
        assert result.status == "skip"

    def test_pass_when_group_resolves(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_workspace_bridge_group_exists

        _stub_marker(monkeypatch, user="claude-sandbox")
        monkeypatch.setattr("core.doctor.checks.workspace_bridge.workspace_bridge_gid", lambda h: 200500)
        result = check_workspace_bridge_group_exists("claude-sandbox", None)
        assert result.status == "pass"
        assert "200500" in result.detail

    def test_mode_comes_from_marker_for_owner_resolution(
        self, isolated_sandbox_ai_home: Any, monkeypatch: Any
    ) -> None:
        # Regression F-069: the host config is now read from the per-operator
        # setup-state marker (`from_marker`), which already carries the
        # setup-determined execution mode. No mode overlay is applied — the mode
        # the marker records reaches `host` directly, so the daemon owner resolves
        # to the configured docker_unprivileged_user, not the invoking operator
        # (the false-failure on fedora). The check no longer takes a `mode` param.
        from core.doctor.checks.workspace_bridge import check_workspace_bridge_group_exists
        from core.host_config import DockerExecutionMode, resolve_daemon_owner_settings

        # The marker records SEPARATE_USER, which is the sole source of the mode
        # (the F-069 overlay is gone — the check has no `mode` param to contradict).
        _stub_marker(monkeypatch, user="sandbox", mode=DockerExecutionMode.SEPARATE_USER)

        captured: dict[str, Any] = {}

        def _capture(host: Any) -> int:
            captured["host"] = host
            return 200500

        monkeypatch.setattr("core.doctor.checks.workspace_bridge.workspace_bridge_gid", _capture)
        result = check_workspace_bridge_group_exists("sandbox", None)
        assert result.status == "pass"
        assert captured["host"].docker_execution_mode is DockerExecutionMode.SEPARATE_USER
        # The owner resolves to the configured user, NOT the invoking operator.
        assert resolve_daemon_owner_settings(captured["host"]) == "sandbox"

    def test_fail_when_group_missing_with_recommendation(
        self, isolated_sandbox_ai_home: Any, monkeypatch: Any
    ) -> None:
        from core.doctor import check_workspace_bridge_group_exists
        from core.host_config import WorkspaceBridgeGroupMissingError

        _stub_marker(monkeypatch, user="claude-sandbox", workspace_bridge_group="sb-ws")

        def _raise(host: Any) -> int:
            raise WorkspaceBridgeGroupMissingError("group missing")

        monkeypatch.setattr("core.doctor.checks.workspace_bridge.workspace_bridge_gid", _raise)
        monkeypatch.setattr(
            "core.doctor.checks.workspace_bridge.autodetect_workspace_bridge_gid_recommendation",
            lambda host_user, in_container_min=1000: 200999,
        )
        result = check_workspace_bridge_group_exists("claude-sandbox", None)
        assert result.status == "fail"
        assert "sb-ws" in (result.remediation or "")
        assert "200999" in (result.remediation or "")

    def test_fail_when_group_missing_and_no_recommendation(
        self, isolated_sandbox_ai_home: Any, monkeypatch: Any
    ) -> None:
        from core.doctor import check_workspace_bridge_group_exists
        from core.host_config import NoSubgidRangeError, WorkspaceBridgeGroupMissingError

        _stub_marker(monkeypatch, user="claude-sandbox")

        def _raise(host: Any) -> int:
            raise WorkspaceBridgeGroupMissingError("group missing")

        def _raise_no_range(host_user: str, in_container_min: int = 1000) -> int:
            raise NoSubgidRangeError("no subgid")

        monkeypatch.setattr("core.doctor.checks.workspace_bridge.workspace_bridge_gid", _raise)
        monkeypatch.setattr(
            "core.doctor.checks.workspace_bridge.autodetect_workspace_bridge_gid_recommendation",
            _raise_no_range,
        )
        result = check_workspace_bridge_group_exists("claude-sandbox", None)
        assert result.status == "fail"
        assert "<pick-a-gid" in (result.remediation or "")

    def test_fail_when_recommendation_finds_no_free_gid(
        self, isolated_sandbox_ai_home: Any, monkeypatch: Any
    ) -> None:
        from core.doctor import check_workspace_bridge_group_exists
        from core.host_config import NoFreeGidInSubgidRangeError, WorkspaceBridgeGroupMissingError

        _stub_marker(monkeypatch, user="claude-sandbox")

        def _raise(host: Any) -> int:
            raise WorkspaceBridgeGroupMissingError("group missing")

        def _raise_no_free(host_user: str, in_container_min: int = 1000) -> int:
            raise NoFreeGidInSubgidRangeError("range exhausted")

        monkeypatch.setattr("core.doctor.checks.workspace_bridge.workspace_bridge_gid", _raise)
        monkeypatch.setattr(
            "core.doctor.checks.workspace_bridge.autodetect_workspace_bridge_gid_recommendation",
            _raise_no_free,
        )
        result = check_workspace_bridge_group_exists("claude-sandbox", None)
        assert result.status == "fail"
        assert "<pick-a-gid" in (result.remediation or "")

    def test_fail_when_gid_out_of_range(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_workspace_bridge_group_exists
        from core.host_config import SubgidOutOfRangeError

        _stub_marker(monkeypatch, user="claude-sandbox")

        def _raise(host: Any) -> int:
            raise SubgidOutOfRangeError("gid 99 not in any range")

        monkeypatch.setattr("core.doctor.checks.workspace_bridge.workspace_bridge_gid", _raise)
        result = check_workspace_bridge_group_exists("claude-sandbox", None)
        assert result.status == "fail"
        assert "Recreate the bridge group" in (result.remediation or "")


class TestCheckDevInWorkspaceBridgeGroup:
    def test_skip_when_no_host_config(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import check_dev_in_workspace_bridge_group

        result = check_dev_in_workspace_bridge_group("u", None)
        assert result.status == "skip"

    def test_pass_when_in_supplementary_groups(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_dev_in_workspace_bridge_group

        _stub_marker(monkeypatch, user="claude-sandbox")
        monkeypatch.setattr("core.doctor.checks.workspace_bridge.workspace_bridge_gid", lambda h: 200500)
        monkeypatch.setattr("core.doctor.checks.workspace_bridge.os.getgroups", lambda: [200500, 1000])
        result = check_dev_in_workspace_bridge_group("u", None)
        assert result.status == "pass"

    def test_fail_relogin_path(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_dev_in_workspace_bridge_group

        _stub_marker(monkeypatch, user="claude-sandbox")
        monkeypatch.setattr("core.doctor.checks.workspace_bridge.workspace_bridge_gid", lambda h: 200500)
        monkeypatch.setattr("core.doctor.checks.workspace_bridge.os.getgroups", lambda: [1000])
        monkeypatch.setattr("core.doctor.checks.workspace_bridge.os.getuid", lambda: 1000)

        class _Pw:
            pw_name = "dev"

        class _Gr:
            gr_gid = 200500

            @property
            def gr_mem(self) -> list[str]:
                return ["dev"]

        import grp
        import pwd

        monkeypatch.setattr(pwd, "getpwuid", lambda uid: _Pw())
        monkeypatch.setattr(grp, "getgrall", lambda: [_Gr()])
        result = check_dev_in_workspace_bridge_group("u", None)
        assert result.status == "fail"
        assert "Log out" in (result.remediation or "")

    def test_fail_usermod_path(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_dev_in_workspace_bridge_group

        _stub_marker(monkeypatch, user="claude-sandbox")
        monkeypatch.setattr("core.doctor.checks.workspace_bridge.workspace_bridge_gid", lambda h: 200500)
        monkeypatch.setattr("core.doctor.checks.workspace_bridge.os.getgroups", lambda: [1000])
        monkeypatch.setattr("core.doctor.checks.workspace_bridge.os.getuid", lambda: 1000)

        class _Pw:
            pw_name = "dev"

        import grp
        import pwd

        monkeypatch.setattr(pwd, "getpwuid", lambda uid: _Pw())
        monkeypatch.setattr(grp, "getgrall", lambda: [])
        result = check_dev_in_workspace_bridge_group("u", None)
        assert result.status == "fail"
        assert "usermod -aG" in (result.remediation or "")

    def test_fail_when_bridge_lookup_raises(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_dev_in_workspace_bridge_group
        from core.host_config import WorkspaceBridgeGroupMissingError

        _stub_marker(monkeypatch, user="claude-sandbox")

        def _raise(host: Any) -> int:
            raise WorkspaceBridgeGroupMissingError("group missing")

        monkeypatch.setattr("core.doctor.checks.workspace_bridge.workspace_bridge_gid", _raise)
        result = check_dev_in_workspace_bridge_group("u", None)
        assert result.status == "fail"


class TestCheckSubuidResolverWorks:
    def test_pass(self, monkeypatch: Any) -> None:
        from core.doctor import check_subuid_resolver_works

        monkeypatch.setattr("core.doctor.checks.workspace_bridge.host_id_for_in_container", lambda n, u: 100999)
        result = check_subuid_resolver_works("claude-sandbox", None)
        assert result.status == "pass"
        assert "100999" in result.detail

    def test_fail_no_subuid(self, monkeypatch: Any) -> None:
        from core.doctor import check_subuid_resolver_works
        from core.host_config import NoSubuidRangeError

        def _raise(n: int, u: str) -> int:
            raise NoSubuidRangeError("no subuid")

        monkeypatch.setattr("core.doctor.checks.workspace_bridge.host_id_for_in_container", _raise)
        result = check_subuid_resolver_works("claude-sandbox", None)
        assert result.status == "fail"
        assert "rootless" in (result.remediation or "")


class TestCheckSecretsHydratedRestrictively:
    def test_pass_when_no_instances(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_secrets_hydrated_restrictively

        monkeypatch.setattr("core.doctor.checks.workspace_bridge.scan_instance_dirs", lambda: [])
        result = check_secrets_hydrated_restrictively("u", None)
        assert result.status == "pass"

    def test_warn_on_world_readable_secret(self, tmp_path: Any, monkeypatch: Any) -> None:
        from core.doctor import check_secrets_hydrated_restrictively

        inst = tmp_path / "inst"
        secrets = inst / "secrets"
        secrets.mkdir(parents=True)
        leak = secrets / "ipc_host_key"
        leak.write_text("k")
        os.chmod(leak, 0o644)

        monkeypatch.setattr("core.doctor.checks.workspace_bridge.scan_instance_dirs", lambda: [str(inst)])
        result = check_secrets_hydrated_restrictively("u", None)
        assert result.status == "warn"
        assert "ipc_host_key" in result.detail


class TestCheckSecretsHydratedRestrictivelyEdges:
    def test_skip_when_secrets_dir_absent(self, tmp_path: Any, monkeypatch: Any) -> None:
        from core.doctor import check_secrets_hydrated_restrictively

        inst = tmp_path / "inst"
        inst.mkdir()
        monkeypatch.setattr("core.doctor.checks.workspace_bridge.scan_instance_dirs", lambda: [str(inst)])
        result = check_secrets_hydrated_restrictively("u", None)
        assert result.status == "pass"

    def test_restrictive_secret_not_flagged_alongside_leak(self, tmp_path: Any, monkeypatch: Any) -> None:
        # A restrictively-permissioned secret (mode & 0o004 == 0) must take the
        # `if mode & 0o004` false branch (221->215) and be skipped, while a
        # world-readable sibling in the same dir is still flagged.
        from core.doctor import check_secrets_hydrated_restrictively

        inst = tmp_path / "inst"
        secrets = inst / "secrets"
        secrets.mkdir(parents=True)
        safe = secrets / "locked_key"
        safe.write_text("k")
        os.chmod(safe, 0o600)
        leak = secrets / "ipc_host_key"
        leak.write_text("k")
        os.chmod(leak, 0o644)

        monkeypatch.setattr("core.doctor.checks.workspace_bridge.scan_instance_dirs", lambda: [str(inst)])
        result = check_secrets_hydrated_restrictively("u", None)
        assert result.status == "warn"
        assert "ipc_host_key" in result.detail
        # The restrictive file is not reported.
        assert "locked_key" not in result.detail
        assert "1 secret(s) world-readable" in result.detail

    def test_all_restrictive_secrets_pass(self, tmp_path: Any, monkeypatch: Any) -> None:
        # When every secret is restrictive, the loop exhausts via the false
        # branch and the check passes.
        from core.doctor import check_secrets_hydrated_restrictively

        inst = tmp_path / "inst"
        secrets = inst / "secrets"
        secrets.mkdir(parents=True)
        for name in ("a", "b"):
            f = secrets / name
            f.write_text("k")
            os.chmod(f, 0o600)

        monkeypatch.setattr("core.doctor.checks.workspace_bridge.scan_instance_dirs", lambda: [str(inst)])
        result = check_secrets_hydrated_restrictively("u", None)
        assert result.status == "pass"

    def test_unstattable_file_skipped(self, tmp_path: Any, monkeypatch: Any) -> None:
        from core.doctor import check_secrets_hydrated_restrictively

        inst = tmp_path / "inst"
        secrets = inst / "secrets"
        secrets.mkdir(parents=True)
        (secrets / "x").write_text("k")

        real_stat = os.stat

        def _raise_on_x(path: str, **kw: Any) -> os.stat_result:
            if path.endswith("/x"):
                raise PermissionError("denied")
            return real_stat(path, **kw)

        monkeypatch.setattr("core.doctor.checks.workspace_bridge.os.stat", _raise_on_x)
        monkeypatch.setattr("core.doctor.checks.workspace_bridge.scan_instance_dirs", lambda: [str(inst)])
        result = check_secrets_hydrated_restrictively("u", None)
        assert result.status == "pass"


class TestCheckPreExistingInstanceLayout:
    def test_just_initd_instance_passes_silently(self, tmp_path: Any, monkeypatch: Any) -> None:
        from core.doctor import check_pre_existing_instance_layout

        inst = tmp_path / "inst"
        inst.mkdir(parents=True)
        monkeypatch.setattr("core.doctor.checks.workspace_bridge.host_id_for_in_container", lambda n, u: 999999)
        monkeypatch.setattr("core.doctor.checks.workspace_bridge.scan_instance_dirs", lambda: [str(inst)])
        result = check_pre_existing_instance_layout("u", None)
        assert result.status == "pass"
        assert result.detail == "no stale cache/log leaf ownership detected"

    def test_pass_when_chowned(self, tmp_path: Any, monkeypatch: Any) -> None:
        from core.doctor import check_pre_existing_instance_layout

        inst = tmp_path / "inst"
        for leaf in ("cache/core/.claude", "log/core"):
            (inst / leaf).mkdir(parents=True)
        target_uid = os.stat(inst / "cache/core/.claude").st_uid
        monkeypatch.setattr("core.doctor.checks.workspace_bridge.host_id_for_in_container", lambda n, u: target_uid)
        monkeypatch.setattr("core.doctor.checks.workspace_bridge.scan_instance_dirs", lambda: [str(inst)])
        result = check_pre_existing_instance_layout("u", None)
        assert result.status == "pass"

    def test_warn_when_dev_owned_includes_per_leaf_rm_rf(self, tmp_path: Any, monkeypatch: Any) -> None:
        from core.doctor import check_pre_existing_instance_layout

        inst = tmp_path / "inst"
        leaves = (
            "cache/core/.claude",
            "log/core",
        )
        for leaf in leaves:
            (inst / leaf).mkdir(parents=True)

        monkeypatch.setattr("core.doctor.checks.workspace_bridge.host_id_for_in_container", lambda n, u: 999999)
        monkeypatch.setattr("core.doctor.checks.workspace_bridge.scan_instance_dirs", lambda: [str(inst)])
        result = check_pre_existing_instance_layout("u", None)
        assert result.status == "warn"
        assert "2 cache/log leaf(s)" in result.detail
        remediation = result.remediation or ""
        for leaf in leaves:
            assert f"rm -rf {inst}/{leaf}" in remediation
        assert "destroy" not in remediation

    def test_mixed_state_reports_only_stale_leaves(self, tmp_path: Any, monkeypatch: Any) -> None:
        from core.doctor import check_pre_existing_instance_layout

        inst = tmp_path / "inst"
        consumer_leaf = inst / "cache/core/.claude"
        legacy_leaf = inst / "log/core"
        consumer_leaf.mkdir(parents=True)
        legacy_leaf.mkdir(parents=True)

        consumer_subuid = 777777
        legacy_uid = 1000

        def resolver(path: str) -> int:
            if not os.path.exists(path):
                raise FileNotFoundError(path)
            if path == str(consumer_leaf):
                return consumer_subuid
            return legacy_uid

        monkeypatch.setattr(
            "core.doctor.checks.workspace_bridge.host_id_for_in_container",
            lambda n, u: consumer_subuid,
        )
        monkeypatch.setattr("core.doctor.checks.workspace_bridge.scan_instance_dirs", lambda: [str(inst)])
        result = check_pre_existing_instance_layout("u", None, uid_for_path=resolver)
        assert result.status == "warn"
        assert "1 cache/log leaf(s)" in result.detail
        assert str(legacy_leaf) in result.detail
        assert str(consumer_leaf) not in result.detail
        remediation = result.remediation or ""
        assert f"rm -rf {legacy_leaf}" in remediation
        assert str(consumer_leaf) not in remediation

    def test_warn_aggregates_across_multiple_instances(self, tmp_path: Any, monkeypatch: Any) -> None:
        from core.doctor import check_pre_existing_instance_layout

        inst_a = tmp_path / "a"
        inst_b = tmp_path / "b"
        (inst_a / "log/core").mkdir(parents=True)
        (inst_b / "cache/core/.claude").mkdir(parents=True)

        monkeypatch.setattr("core.doctor.checks.workspace_bridge.host_id_for_in_container", lambda n, u: 999999)
        monkeypatch.setattr(
            "core.doctor.checks.workspace_bridge.scan_instance_dirs",
            lambda: [str(inst_a), str(inst_b)],
        )
        result = check_pre_existing_instance_layout("u", None)
        assert result.status == "warn"
        assert "2 cache/log leaf(s)" in result.detail
        remediation = result.remediation or ""
        assert f"rm -rf {inst_a}/log/core" in remediation
        assert f"rm -rf {inst_b}/cache/core/.claude" in remediation

    def test_pass_when_partial_layout_resolves_correctly(self, tmp_path: Any, monkeypatch: Any) -> None:
        from core.doctor import check_pre_existing_instance_layout

        inst = tmp_path / "inst"
        (inst / "log/core").mkdir(parents=True)
        target_uid = (inst / "log/core").stat().st_uid

        monkeypatch.setattr("core.doctor.checks.workspace_bridge.host_id_for_in_container", lambda n, u: target_uid)
        monkeypatch.setattr("core.doctor.checks.workspace_bridge.scan_instance_dirs", lambda: [str(inst)])
        result = check_pre_existing_instance_layout("u", None)
        assert result.status == "pass"

    def test_skip_when_no_subuid(self, monkeypatch: Any) -> None:
        from core.doctor import check_pre_existing_instance_layout
        from core.host_config import NoSubuidRangeError

        def _raise(n: int, u: str) -> int:
            raise NoSubuidRangeError("none")

        monkeypatch.setattr("core.doctor.checks.workspace_bridge.host_id_for_in_container", _raise)
        result = check_pre_existing_instance_layout("u", None)
        assert result.status == "skip"


class TestCheckBackupsDiskPressure:
    def test_pass_when_no_backups_dir(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import check_backups_disk_pressure

        result = check_backups_disk_pressure("u", None)
        assert result.status == "pass"
        assert "no backups directory" in result.detail

    def test_pass_under_threshold(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import check_backups_disk_pressure

        backups = isolated_sandbox_ai_home / "workspaces" / "_backups" / "i" / "w" / "2026-05-07-00-00-00"
        backups.mkdir(parents=True)
        (backups / "data").write_text("hi")
        result = check_backups_disk_pressure("u", None)
        assert result.status == "pass"

    def test_warn_when_too_many_entries(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import check_backups_disk_pressure

        ws_dir = isolated_sandbox_ai_home / "workspaces" / "_backups" / "i" / "w"
        ws_dir.mkdir(parents=True)
        for n in range(51):
            (ws_dir / f"2026-05-07-00-00-{n:02d}").mkdir()
        result = check_backups_disk_pressure("u", None)
        assert result.status == "warn"
        assert "51 entries" in result.detail

    def test_warn_when_size_exceeds(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_backups_disk_pressure

        backup = isolated_sandbox_ai_home / "workspaces" / "_backups" / "i" / "w" / "2026-05-07-00-00-00"
        backup.mkdir(parents=True)
        (backup / "data").write_text("x")

        real_lstat = os.lstat

        def fat_stat(path: str) -> os.stat_result:
            st = real_lstat(path)
            if path.endswith("/data"):
                return os.stat_result(
                    (
                        st.st_mode,
                        st.st_ino,
                        st.st_dev,
                        st.st_nlink,
                        st.st_uid,
                        st.st_gid,
                        6 * 1024**3 + 1,
                        st.st_atime,
                        st.st_mtime,
                        st.st_ctime,
                    )
                )
            return st

        monkeypatch.setattr("core.doctor.checks.workspace_bridge.os.lstat", fat_stat)
        result = check_backups_disk_pressure("u", None)
        assert result.status == "warn"

    def test_unstattable_file_skipped(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_backups_disk_pressure

        backup = isolated_sandbox_ai_home / "workspaces" / "_backups" / "i" / "w" / "2026-05-07-00-00-00"
        backup.mkdir(parents=True)
        (backup / "data").write_text("x")

        real_lstat = os.lstat

        def boom(path: str) -> os.stat_result:
            if path.endswith("/data"):
                raise OSError("denied")
            return real_lstat(path)

        monkeypatch.setattr("core.doctor.checks.workspace_bridge.os.lstat", boom)
        result = check_backups_disk_pressure("u", None)
        assert result.status == "pass"

    def test_stray_files_in_backup_tree_skipped(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import check_backups_disk_pressure

        backups = isolated_sandbox_ai_home / "workspaces" / "_backups"
        backups.mkdir(parents=True)
        (backups / "README").write_text("not an instance")
        (backups / "inst").mkdir()
        (backups / "inst" / "stray-file").write_text("not a workspace")
        (backups / "inst" / "ws").mkdir()
        (backups / "inst" / "ws" / "another-stray").write_text("not a timestamp")
        result = check_backups_disk_pressure("u", None)
        assert result.status == "pass"


class TestCheckBackupsPartialDirsPresent:
    def test_pass_when_no_backups_dir(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import check_backups_partial_dirs_present

        result = check_backups_partial_dirs_present("u", None)
        assert result.status == "pass"

    def test_pass_when_partial_is_fresh(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import check_backups_partial_dirs_present

        partial = isolated_sandbox_ai_home / "workspaces" / "_backups" / "i" / "w" / "2026-05-07-00-00-00.partial"
        partial.mkdir(parents=True)
        result = check_backups_partial_dirs_present("u", None)
        assert result.status == "pass"

    def test_warn_when_partial_is_stale(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import check_backups_partial_dirs_present

        partial = isolated_sandbox_ai_home / "workspaces" / "_backups" / "i" / "w" / "2026-05-07-00-00-00.partial"
        partial.mkdir(parents=True)

        import time

        old = time.time() - 7200
        os.utime(partial, (old, old))

        result = check_backups_partial_dirs_present("u", None)
        assert result.status == "warn"
        assert ".partial" in result.detail

    def test_unstattable_partial_skipped(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_backups_partial_dirs_present

        partial = isolated_sandbox_ai_home / "workspaces" / "_backups" / "i" / "w" / "2026-05-07-00-00-00.partial"
        partial.mkdir(parents=True)

        real_lstat = os.lstat

        def boom(path: str) -> os.stat_result:
            if path.endswith(".partial"):
                raise OSError("denied")
            return real_lstat(path)

        monkeypatch.setattr("core.doctor.checks.workspace_bridge.os.lstat", boom)
        result = check_backups_partial_dirs_present("u", None)
        assert result.status == "pass"


class TestCheckDevUmaskWorkspaceFriendly:
    def test_skip_when_no_workspaces(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_dev_umask_workspace_friendly

        monkeypatch.setattr("core.doctor.checks.workspace_bridge._scan_instance_workspace_paths", lambda: [])
        result = check_dev_umask_workspace_friendly("u", None)
        assert result.status == "skip"

    def test_warn_on_022_umask(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_dev_umask_workspace_friendly

        monkeypatch.setattr(
            "core.doctor.checks.workspace_bridge._scan_instance_workspace_paths",
            lambda: [("/i", "main", "/p")],
        )
        monkeypatch.setattr("core.doctor.checks.workspace_bridge.os.umask", lambda mask: 0o022 if mask == 0 else 0)
        result = check_dev_umask_workspace_friendly("u", None)
        assert result.status == "warn"
        assert "0022" in result.detail

    def test_pass_on_002_umask(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_dev_umask_workspace_friendly

        monkeypatch.setattr(
            "core.doctor.checks.workspace_bridge._scan_instance_workspace_paths",
            lambda: [("/i", "main", "/p")],
        )
        monkeypatch.setattr("core.doctor.checks.workspace_bridge.os.umask", lambda mask: 0o002 if mask == 0 else 0)
        result = check_dev_umask_workspace_friendly("u", None)
        assert result.status == "pass"


class TestCheckWorkspacePathInWalkerBoundary:
    def test_pass_when_no_workspaces(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_workspace_path_in_walker_boundary

        monkeypatch.setattr("core.doctor.checks.workspace_bridge._scan_instance_workspace_paths", lambda: [])
        result = check_workspace_path_in_walker_boundary("u", None)
        assert result.status == "pass"

    def test_fail_when_workspace_at_boundary(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_workspace_path_in_walker_boundary

        monkeypatch.setattr(
            "core.doctor.checks.workspace_bridge._scan_instance_workspace_paths",
            lambda: [("/i", "main", "/etc")],
        )
        result = check_workspace_path_in_walker_boundary("u", None)
        assert result.status == "fail"
        assert "/etc" in result.detail

    def test_safe_path_not_flagged_alongside_boundary(
        self, isolated_sandbox_ai_home: Any, monkeypatch: Any
    ) -> None:
        # A non-boundary workspace must take the `if real in BOUNDARY_PATHS`
        # false branch (519->514) and be skipped, while a boundary sibling is
        # still reported — proving the loop continues past the safe entry.
        from core.doctor import check_workspace_path_in_walker_boundary

        monkeypatch.setattr(
            "core.doctor.checks.workspace_bridge._scan_instance_workspace_paths",
            lambda: [("/i", "safe", "/home/dev/projects/myws"), ("/i", "main", "/etc")],
        )
        result = check_workspace_path_in_walker_boundary("u", None)
        assert result.status == "fail"
        assert "/etc" in result.detail
        assert "1 workspace(s) at boundary" in result.detail
        assert "myws" not in result.detail

    def test_pass_when_all_paths_safe(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        # Every workspace is outside the boundary set — the loop exhausts via the
        # false branch and the check passes.
        from core.doctor import check_workspace_path_in_walker_boundary

        monkeypatch.setattr(
            "core.doctor.checks.workspace_bridge._scan_instance_workspace_paths",
            lambda: [("/i", "a", "/home/dev/projects/x"), ("/i", "b", "/home/dev/projects/y")],
        )
        result = check_workspace_path_in_walker_boundary("u", None)
        assert result.status == "pass"

    def test_realpath_oserror_skipped(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_workspace_path_in_walker_boundary

        monkeypatch.setattr(
            "core.doctor.checks.workspace_bridge._scan_instance_workspace_paths",
            lambda: [("/i", "main", "/some/path")],
        )

        def boom(_: str) -> str:
            raise OSError("denied")

        monkeypatch.setattr("core.doctor.checks.workspace_bridge.os.path.realpath", boom)
        result = check_workspace_path_in_walker_boundary("u", None)
        assert result.status == "pass"


class TestCheckWorkspaceHomeSingleFilesystem:
    def test_pass_when_workspaces_dir_absent(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import check_workspace_home_single_filesystem

        isolated_sandbox_ai_home.mkdir(parents=True)
        result = check_workspace_home_single_filesystem("u", None)
        assert result.status == "pass"
        assert "absent" in result.detail

    def test_pass_on_same_fs(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import check_workspace_home_single_filesystem

        (isolated_sandbox_ai_home / "workspaces").mkdir(parents=True)
        result = check_workspace_home_single_filesystem("u", None)
        assert result.status == "pass"

    def test_warn_on_cross_fs(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_workspace_home_single_filesystem

        (isolated_sandbox_ai_home / "workspaces").mkdir(parents=True)

        real_stat = os.stat

        def differ(path: Any) -> Any:
            st = real_stat(path)
            if str(path).endswith("/workspaces"):
                return os.stat_result(
                    (
                        st.st_mode,
                        st.st_ino,
                        st.st_dev + 1,
                        st.st_nlink,
                        st.st_uid,
                        st.st_gid,
                        st.st_size,
                        st.st_atime,
                        st.st_mtime,
                        st.st_ctime,
                    )
                )
            return st

        monkeypatch.setattr("core.doctor.checks.workspace_bridge.os.stat", differ)
        result = check_workspace_home_single_filesystem("u", None)
        assert result.status == "warn"
        assert "different filesystems" in result.detail

    def test_skip_on_stat_error(self, isolated_sandbox_ai_home: Any, monkeypatch: Any) -> None:
        from core.doctor import check_workspace_home_single_filesystem

        (isolated_sandbox_ai_home / "workspaces").mkdir(parents=True)

        def boom(path: Any) -> Any:
            raise PermissionError("denied")

        monkeypatch.setattr("core.doctor.checks.workspace_bridge.os.stat", boom)
        result = check_workspace_home_single_filesystem("u", None)
        assert result.status == "skip"


