# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for core.doctor.checks.binary_integrity_posture.

Always-PASS informational check; the four mechanism probes degrade gracefully
to NOT-INSTALLED / INACTIVE when their binary/file is absent.
"""

from __future__ import annotations

import subprocess
from typing import Any

_MOD = "core.doctor.checks.binary_integrity_posture"


def _cp(stdout: str = "", stderr: str = "", rc: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def test_module_exposes_single_check() -> None:
    from core.doctor.checks import binary_integrity_posture

    assert set(binary_integrity_posture.__all__) == {"check_binary_integrity_posture"}


class TestNoneActive:
    def test_none_of_the_four_active_reports_pass_with_hint(self, monkeypatch: Any) -> None:
        from core.doctor.checks.binary_integrity_posture import check_binary_integrity_posture

        monkeypatch.setattr(f"{_MOD}._read_text", lambda p: None)
        monkeypatch.setattr(f"{_MOD}.shutil.which", lambda b: None)
        monkeypatch.setattr(f"{_MOD}._run", lambda argv: _cp(stdout="inactive"))
        result = check_binary_integrity_posture("sandbox", None)
        assert result.status == "pass"
        assert "dm-verity=INACTIVE" in result.detail
        assert "IMA=NOT-APPRAISING" in result.detail
        assert "fapolicyd=NOT-RUNNING" in result.detail
        assert "AIDE=NOT-INSTALLED" in result.detail
        assert result.remediation is not None
        assert "production hosts" in result.remediation


class TestFullyHardened:
    def test_all_four_active_reports_pass_no_remediation(self, monkeypatch: Any) -> None:
        from core.doctor.checks.binary_integrity_posture import check_binary_integrity_posture

        def fake_read(path: Any) -> str:
            s = str(path)
            if "cmdline" in s:
                return "root=/dev/mapper/x dm-verity ro"
            if "ima" in s:
                return "appraise func=BPRM_CHECK"
            return ""

        def fake_which(binary: str) -> str | None:
            return f"/usr/bin/{binary}"

        def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
            if argv[:2] == ["dmsetup", "status"]:
                return _cp(stdout="root: 0 100 verity 1")
            if argv[:2] == ["systemctl", "is-active"]:
                return _cp(stdout="active\n")
            if argv[0] == "fapolicyd-cli":
                return _cp(stdout="Enforcing")
            return _cp()

        monkeypatch.setattr(f"{_MOD}._read_text", fake_read)
        monkeypatch.setattr(f"{_MOD}.shutil.which", fake_which)
        monkeypatch.setattr(f"{_MOD}._run", fake_run)

        def fake_exists(self: Any) -> bool:
            return "aide.db" in str(self)

        monkeypatch.setattr("pathlib.Path.exists", fake_exists)
        result = check_binary_integrity_posture("sandbox", None)
        assert result.status == "pass"
        assert "fully hardened" in result.detail
        assert result.remediation is None


class TestProbeBranches:
    def test_dm_verity_marker_without_dmsetup_is_inactive(self, monkeypatch: Any) -> None:
        from core.doctor.checks import binary_integrity_posture as m

        monkeypatch.setattr(f"{_MOD}._read_text", lambda p: "dm-verity")
        monkeypatch.setattr(f"{_MOD}.shutil.which", lambda b: None)
        assert m._probe_dm_verity() == "INACTIVE"

    def test_dm_verity_dmsetup_nonzero_is_inactive(self, monkeypatch: Any) -> None:
        from core.doctor.checks import binary_integrity_posture as m

        monkeypatch.setattr(f"{_MOD}._read_text", lambda p: "verity")
        monkeypatch.setattr(f"{_MOD}.shutil.which", lambda b: "/sbin/dmsetup")
        monkeypatch.setattr(f"{_MOD}._run", lambda argv: _cp(rc=1))
        assert m._probe_dm_verity() == "INACTIVE"

    def test_dm_verity_dmsetup_run_none_is_inactive(self, monkeypatch: Any) -> None:
        from core.doctor.checks import binary_integrity_posture as m

        monkeypatch.setattr(f"{_MOD}._read_text", lambda p: "verity")
        monkeypatch.setattr(f"{_MOD}.shutil.which", lambda b: "/sbin/dmsetup")
        monkeypatch.setattr(f"{_MOD}._run", lambda argv: None)
        assert m._probe_dm_verity() == "INACTIVE"

    def test_dm_verity_no_verity_target_is_inactive(self, monkeypatch: Any) -> None:
        from core.doctor.checks import binary_integrity_posture as m

        monkeypatch.setattr(f"{_MOD}._read_text", lambda p: "dm-verity")
        monkeypatch.setattr(f"{_MOD}.shutil.which", lambda b: "/sbin/dmsetup")
        monkeypatch.setattr(f"{_MOD}._run", lambda argv: _cp(stdout="root: 0 100 linear"))
        assert m._probe_dm_verity() == "INACTIVE"

    def test_ima_appraising(self, monkeypatch: Any) -> None:
        from core.doctor.checks import binary_integrity_posture as m

        monkeypatch.setattr(f"{_MOD}._read_text", lambda p: "measure\nappraise func=X")
        assert m._probe_ima() == "APPRAISING"

    def test_ima_policy_present_no_appraise(self, monkeypatch: Any) -> None:
        from core.doctor.checks import binary_integrity_posture as m

        monkeypatch.setattr(f"{_MOD}._read_text", lambda p: "measure func=X")
        assert m._probe_ima() == "NOT-APPRAISING"

    def test_fapolicyd_inactive_is_not_running(self, monkeypatch: Any) -> None:
        from core.doctor.checks import binary_integrity_posture as m

        monkeypatch.setattr(f"{_MOD}._run", lambda argv: _cp(stdout="inactive"))
        assert m._probe_fapolicyd() == "NOT-RUNNING"

    def test_fapolicyd_active_no_cli_is_not_running(self, monkeypatch: Any) -> None:
        from core.doctor.checks import binary_integrity_posture as m

        monkeypatch.setattr(f"{_MOD}._run", lambda argv: _cp(stdout="active"))
        monkeypatch.setattr(f"{_MOD}.shutil.which", lambda b: None)
        assert m._probe_fapolicyd() == "NOT-RUNNING"

    def test_fapolicyd_active_cli_none_is_not_running(self, monkeypatch: Any) -> None:
        from core.doctor.checks import binary_integrity_posture as m

        def run(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
            if argv[:2] == ["systemctl", "is-active"]:
                return _cp(stdout="active")
            return None

        monkeypatch.setattr(f"{_MOD}._run", run)
        monkeypatch.setattr(f"{_MOD}.shutil.which", lambda b: "/usr/sbin/fapolicyd-cli")
        assert m._probe_fapolicyd() == "NOT-RUNNING"

    def test_fapolicyd_permissive(self, monkeypatch: Any) -> None:
        from core.doctor.checks import binary_integrity_posture as m

        def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
            if argv[:2] == ["systemctl", "is-active"]:
                return _cp(stdout="active")
            return _cp(stdout="permissive mode")

        monkeypatch.setattr(f"{_MOD}._run", run)
        monkeypatch.setattr(f"{_MOD}.shutil.which", lambda b: "/usr/sbin/fapolicyd-cli")
        assert m._probe_fapolicyd() == "PERMISSIVE"

    def test_fapolicyd_status_unknown_defaults_enforcing(self, monkeypatch: Any) -> None:
        from core.doctor.checks import binary_integrity_posture as m

        def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
            if argv[:2] == ["systemctl", "is-active"]:
                return _cp(stdout="active")
            return _cp(stdout="something else")

        monkeypatch.setattr(f"{_MOD}._run", run)
        monkeypatch.setattr(f"{_MOD}.shutil.which", lambda b: "/usr/sbin/fapolicyd-cli")
        assert m._probe_fapolicyd() == "ENFORCING"

    def test_fapolicyd_active_run_none(self, monkeypatch: Any) -> None:
        from core.doctor.checks import binary_integrity_posture as m

        def run(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
            if argv[:2] == ["systemctl", "is-active"]:
                return None
            return _cp()

        monkeypatch.setattr(f"{_MOD}._run", run)
        assert m._probe_fapolicyd() == "NOT-RUNNING"

    def test_aide_not_installed(self, monkeypatch: Any) -> None:
        from core.doctor.checks import binary_integrity_posture as m

        monkeypatch.setattr(f"{_MOD}.shutil.which", lambda b: None)
        assert m._probe_aide() == "NOT-INSTALLED"

    def test_aide_installed_db_missing(self, monkeypatch: Any) -> None:
        from core.doctor.checks import binary_integrity_posture as m

        monkeypatch.setattr(f"{_MOD}.shutil.which", lambda b: "/usr/bin/aide")
        monkeypatch.setattr("pathlib.Path.exists", lambda self: False)
        assert m._probe_aide() == "INSTALLED-DB-MISSING"

    def test_aide_installed_db_present(self, monkeypatch: Any) -> None:
        from core.doctor.checks import binary_integrity_posture as m

        monkeypatch.setattr(f"{_MOD}.shutil.which", lambda b: "/usr/bin/aide")
        monkeypatch.setattr("pathlib.Path.exists", lambda self: True)
        assert m._probe_aide() == "INSTALLED-DB-PRESENT"

    def test_run_handles_oserror(self, monkeypatch: Any) -> None:
        from core.doctor.checks import binary_integrity_posture as m

        def boom(*a: Any, **k: Any) -> Any:
            raise OSError("no binary")

        monkeypatch.setattr(f"{_MOD}.subprocess.run", boom)
        assert m._run(["whatever"]) is None

    def test_read_text_handles_oserror(self, monkeypatch: Any) -> None:
        from core.doctor.checks import binary_integrity_posture as m

        def boom(self: Any) -> str:
            raise OSError("denied")

        monkeypatch.setattr("pathlib.Path.read_text", boom)
        assert m._read_text(m._CMDLINE) is None
