# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for core.doctor.checks.per_user_tree.

Covers the per-user-tree existence/mode checks plus three legacy-state
detection checks (sandboxes/, [instance].user_project_root, path-keyed
instances.json).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def test_module_exposes_seven_check_functions() -> None:
    from core.doctor.checks import per_user_tree

    expected = {
        "check_legacy_cwd_files",
        "check_legacy_registry_shape",
        "check_legacy_sandboxes_dir_detected",
        "check_legacy_workspace_in_user_project_root",
        "check_obsolete_host_toml",
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
    _STALE = "Per-host config now lives at"

    def test_pass_when_no_legacy(self, tmp_path: Path, monkeypatch: Any) -> None:
        from core.doctor import check_legacy_cwd_files

        monkeypatch.chdir(tmp_path)
        result = check_legacy_cwd_files("u", None)
        assert result.status == "pass"

    def test_warn_on_legacy_toml_only(self, tmp_path: Path, monkeypatch: Any) -> None:
        from core.doctor import check_legacy_cwd_files

        (tmp_path / "sandbox-ai.toml").write_text("")
        monkeypatch.chdir(tmp_path)
        result = check_legacy_cwd_files("u", None)
        assert result.status == "warn"
        assert "Found legacy" in result.detail
        assert "sandbox-ai.toml" in result.detail
        assert "Per-host config is now setup-determined" in result.detail
        assert "sudo sandbox setup" in result.detail
        # State message absent when only the toml exists.
        assert "Orchestrator state now lives at" not in result.detail
        # The stale "new toml location" wording must be gone.
        assert self._STALE not in result.detail

    def test_warn_on_legacy_state_only(self, tmp_path: Path, monkeypatch: Any) -> None:
        from core.doctor import check_legacy_cwd_files

        (tmp_path / ".state").mkdir()
        monkeypatch.chdir(tmp_path)
        result = check_legacy_cwd_files("u", None)
        assert result.status == "warn"
        assert ".state" in result.detail
        assert "Orchestrator state now lives at" in result.detail
        assert "delete the legacy directory" in result.detail
        assert "Found legacy" in result.detail
        assert self._STALE not in result.detail

    def test_warn_on_legacy_toml_and_state(self, tmp_path: Path, monkeypatch: Any) -> None:
        from core.doctor import check_legacy_cwd_files

        (tmp_path / "sandbox-ai.toml").write_text("")
        (tmp_path / ".state").mkdir()
        monkeypatch.chdir(tmp_path)
        result = check_legacy_cwd_files("u", None)
        assert result.status == "warn"
        # Both per-file messages present.
        assert "sandbox-ai.toml" in result.detail
        assert ".state" in result.detail
        assert "Per-host config is now setup-determined" in result.detail
        assert "Orchestrator state now lives at" in result.detail
        assert self._STALE not in result.detail


class TestCheckObsoleteHostToml:
    def test_warn_when_present(self, isolated_sandbox_ai_home: Path) -> None:
        from core.doctor import check_obsolete_host_toml

        config = isolated_sandbox_ai_home / "config"
        config.mkdir(parents=True)
        leftover = config / "sandbox-ai.toml"
        leftover.write_text("")
        result = check_obsolete_host_toml("u", None)
        assert result.status == "warn"
        assert result.detail == (
            f"Found an obsolete {leftover}. Host config is now setup-determined — "
            "delete this file and run `sudo sandbox setup`."
        )
        # Must not reference the internal marker / setup-state.json.
        assert "marker" not in result.detail
        assert "setup-state.json" not in result.detail

    def test_pass_when_absent(self, isolated_sandbox_ai_home: Path) -> None:
        from core.doctor import check_obsolete_host_toml

        result = check_obsolete_host_toml("u", None)
        assert result.status == "pass"


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
