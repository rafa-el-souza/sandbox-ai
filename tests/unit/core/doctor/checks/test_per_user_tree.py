# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for core.doctor.checks.per_user_tree.

Covers the per-user-tree existence/mode checks plus three legacy-state
detection checks (sandboxes/, [instance].user_project_root, path-keyed
instances.json).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def test_module_exposes_six_check_functions() -> None:
    from core.doctor.checks import per_user_tree

    expected = {
        "check_legacy_cwd_files",
        "check_legacy_registry_shape",
        "check_legacy_sandboxes_dir_detected",
        "check_legacy_workspace_in_user_project_root",
        "check_per_user_tree_exists",
        "check_per_user_tree_mode",
    }
    assert set(per_user_tree.__all__) == expected


def test_public_re_exports_resolve_to_topic_module() -> None:
    import core.doctor as doctor_pkg
    from core.doctor.checks import per_user_tree

    for name in per_user_tree.__all__:
        assert getattr(doctor_pkg, name) is getattr(per_user_tree, name)


class TestCheckPerUserTreeExists:
    def test_pass_when_tree_present(self, isolated_sandbox_ai_home: Path) -> None:
        from core.doctor import check_per_user_tree_exists
        from core.host_config import ensure_per_user_state

        ensure_per_user_state(isolated_sandbox_ai_home)
        result = check_per_user_tree_exists("u", None)
        assert result.status == "pass"

    def test_fail_when_home_missing(self, isolated_sandbox_ai_home: Path) -> None:
        from core.doctor import check_per_user_tree_exists

        result = check_per_user_tree_exists("u", None)
        assert result.status == "fail"
        assert "missing" in result.detail.lower()
        assert result.remediation is not None
        assert "sandbox init" in result.remediation

    def test_fail_when_state_subdir_missing(self, isolated_sandbox_ai_home: Path) -> None:
        from core.doctor import check_per_user_tree_exists

        # Create only home and config — state is missing.
        (isolated_sandbox_ai_home / "config").mkdir(parents=True)
        result = check_per_user_tree_exists("u", None)
        assert result.status == "fail"
        assert "state" in result.detail


class TestCheckPerUserTreeMode:
    def test_pass_when_all_0700(self, isolated_sandbox_ai_home: Path) -> None:
        from core.doctor import check_per_user_tree_mode
        from core.host_config import ensure_per_user_state

        ensure_per_user_state(isolated_sandbox_ai_home)
        result = check_per_user_tree_mode("u", None)
        assert result.status == "pass"

    def test_warn_on_mode_drift(self, isolated_sandbox_ai_home: Path) -> None:
        import os

        from core.doctor import check_per_user_tree_mode
        from core.host_config import ensure_per_user_state

        ensure_per_user_state(isolated_sandbox_ai_home)
        os.chmod(isolated_sandbox_ai_home / "state", 0o755)
        result = check_per_user_tree_mode("u", None)
        assert result.status == "warn"
        assert "0o755" in result.detail
        assert result.remediation is not None
        assert "chmod 0700" in result.remediation

    def test_skip_when_tree_missing(self, isolated_sandbox_ai_home: Path) -> None:
        from core.doctor import check_per_user_tree_mode

        result = check_per_user_tree_mode("u", None)
        assert result.status == "skip"


class TestCheckLegacyCwdFiles:
    def test_pass_when_no_legacy(self, tmp_path: Path, monkeypatch: Any) -> None:
        from core.doctor import check_legacy_cwd_files

        monkeypatch.chdir(tmp_path)
        result = check_legacy_cwd_files("u", None)
        assert result.status == "pass"

    def test_warn_on_legacy_toml_and_state(self, tmp_path: Path, monkeypatch: Any) -> None:
        from core.doctor import check_legacy_cwd_files

        (tmp_path / "sandbox-ai.toml").write_text("")
        (tmp_path / ".state").mkdir()
        monkeypatch.chdir(tmp_path)
        result = check_legacy_cwd_files("u", None)
        assert result.status == "warn"
        assert "sandbox-ai.toml" in result.detail
        assert ".state" in result.detail


class TestCheckLegacySandboxesDirDetected:
    def test_pass_when_absent(self, tmp_path: Any, monkeypatch: Any) -> None:
        from core.doctor import check_legacy_sandboxes_dir_detected

        monkeypatch.chdir(tmp_path)
        result = check_legacy_sandboxes_dir_detected("u", None)
        assert result.status == "pass"

    def test_warn_when_present(self, tmp_path: Any, monkeypatch: Any) -> None:
        from core.doctor import check_legacy_sandboxes_dir_detected

        (tmp_path / "sandboxes").mkdir()
        monkeypatch.chdir(tmp_path)
        result = check_legacy_sandboxes_dir_detected("u", None)
        assert result.status == "warn"
        assert "sandboxes" in result.detail


class TestCheckLegacyWorkspaceInUserProjectRoot:
    def test_pass_when_no_legacy_field(self, tmp_path: Any, monkeypatch: Any) -> None:
        from core.doctor import check_legacy_workspace_in_user_project_root

        inst = tmp_path / "inst"
        inst.mkdir()
        (inst / "sandbox.toml").write_text('[instance]\nname = "x"\n')
        monkeypatch.setattr("core.doctor.checks.per_user_tree.scan_instance_dirs", lambda: [str(inst)])
        result = check_legacy_workspace_in_user_project_root("u", None)
        assert result.status == "pass"

    def test_warn_when_legacy_field_present(self, tmp_path: Any, monkeypatch: Any) -> None:
        from core.doctor import check_legacy_workspace_in_user_project_root

        inst = tmp_path / "myinst"
        inst.mkdir()
        (inst / "sandbox.toml").write_text(
            '[instance]\nname = "myinst"\nuser_project_root = "/old/path"\n'
        )
        monkeypatch.setattr("core.doctor.checks.per_user_tree.scan_instance_dirs", lambda: [str(inst)])
        result = check_legacy_workspace_in_user_project_root("u", None)
        assert result.status == "warn"
        assert "myinst" in result.detail

    def test_unparseable_toml_skipped(self, tmp_path: Any, monkeypatch: Any) -> None:
        from core.doctor import check_legacy_workspace_in_user_project_root

        inst = tmp_path / "inst"
        inst.mkdir()
        (inst / "sandbox.toml").write_text("not = valid = toml = !!")
        monkeypatch.setattr("core.doctor.checks.per_user_tree.scan_instance_dirs", lambda: [str(inst)])
        result = check_legacy_workspace_in_user_project_root("u", None)
        # Skipped silently → pass (no legacy detected because we couldn't read).
        assert result.status == "pass"


class TestCheckLegacyRegistryShape:
    def test_pass_on_name_keyed(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import check_legacy_registry_shape

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True)
        (state / "instances.json").write_text(json.dumps({"foo": {"instance_dir": "/x"}}))
        result = check_legacy_registry_shape("u", None)
        assert result.status == "pass"

    def test_warn_on_path_keyed(self, isolated_sandbox_ai_home: Any) -> None:
        from core.doctor import check_legacy_registry_shape

        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True)
        (state / "instances.json").write_text(json.dumps({"/home/user/foo": {"x": 1}}))
        result = check_legacy_registry_shape("u", None)
        assert result.status == "warn"
