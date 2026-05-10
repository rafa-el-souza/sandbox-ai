"""Tests for core.doctor.checks.repo_integrity.

Covers `check_tooling_plane` (template inventory) and `check_state_dir_writable`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


def test_module_exposes_two_check_functions_and_unconditional_files() -> None:
    from core.doctor.checks import repo_integrity

    assert set(repo_integrity.__all__) == {
        "check_state_dir_writable",
        "check_tooling_plane",
    }
    assert isinstance(repo_integrity._UNCONDITIONAL_FILES, list)
    assert len(repo_integrity._UNCONDITIONAL_FILES) == 17
    assert callable(repo_integrity._resource_files)


def test_public_re_exports_resolve_to_topic_module() -> None:
    import core.doctor as doctor_pkg
    from core.doctor.checks import repo_integrity

    for name in repo_integrity.__all__:
        assert getattr(doctor_pkg, name) is getattr(repo_integrity, name)


class TestCheckToolingPlane:
    def test_tooling_plane_intact(self) -> None:
        from core.doctor import check_tooling_plane

        result = check_tooling_plane("sandbox", None)
        assert result.status == "pass"
        assert "17" in result.detail

    def test_tooling_plane_missing_files(self, tmp_path: Path) -> None:
        from core.doctor import check_tooling_plane

        # Build a templates root missing compose.yml (and other entries)
        (tmp_path / "docker").mkdir()
        with patch("core.doctor.checks.repo_integrity._resource_files", return_value=tmp_path):
            result = check_tooling_plane("sandbox", None)
            assert result.status == "fail"
            assert "compose.yml" in result.detail


class TestCheckStateDirWritable:
    def test_state_dir_writable(self, isolated_sandbox_ai_home: Path) -> None:
        from core.doctor import check_state_dir_writable

        (isolated_sandbox_ai_home / "state").mkdir(parents=True)
        result = check_state_dir_writable("sandbox", None)
        assert result.status == "pass"

    def test_state_dir_not_writable(self, isolated_sandbox_ai_home: Path) -> None:
        from core.doctor import check_state_dir_writable

        (isolated_sandbox_ai_home / "state").mkdir(parents=True)
        with patch("tempfile.NamedTemporaryFile", side_effect=OSError("perm denied")):
            result = check_state_dir_writable("sandbox", None)
            assert result.status == "fail"
