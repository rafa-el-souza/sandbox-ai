"""Tests for core.doctor.checks.filesystem.

Covers `check_setfacl`, `check_acl_support`, `check_ancestor_traverse`,
plus the private `_has_acl_exec` getfacl probe.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch


def test_module_exposes_three_check_functions_and_private_helpers() -> None:
    from core.doctor.checks import filesystem

    assert set(filesystem.__all__) == {
        "check_acl_support",
        "check_ancestor_traverse",
        "check_setfacl",
    }
    assert callable(filesystem._has_acl_exec)
    assert isinstance(filesystem._ACL_PROBE_FAILURES, tuple)


def test_public_re_exports_resolve_to_topic_module() -> None:
    import core.doctor as doctor_pkg
    from core.doctor.checks import filesystem

    for name in filesystem.__all__:
        assert getattr(doctor_pkg, name) is getattr(filesystem, name)


def _make_stat(uid: int = 0, gid: int = 0, mode: int = 0o755) -> MagicMock:
    """Create a synthetic os.stat_result with controlled uid/gid/mode."""
    st = MagicMock(spec=os.stat_result)
    st.st_uid = uid
    st.st_gid = gid
    st.st_mode = mode
    return st


def _mock_pwd(user: str = "sandbox", uid: int = 2000, gid: int = 2000) -> MagicMock:
    """Create a synthetic pwd entry."""
    pw = MagicMock()
    pw.pw_uid = uid
    pw.pw_gid = gid
    pw.pw_name = user
    return pw


class TestSetfaclBinary:
    def test_check_setfacl_present(self) -> None:
        from core.doctor import check_setfacl

        with patch("shutil.which", return_value="/usr/bin/setfacl"):
            result = check_setfacl("sandbox", None)
            assert result.status == "pass"

    def test_check_setfacl_absent(self) -> None:
        from core.doctor import check_setfacl

        with patch("shutil.which", return_value=None):
            result = check_setfacl("sandbox", "fedora")
            assert result.status == "fail"
            assert "dnf" in (result.remediation or "")


class TestAclSupport:
    def test_acl_support_pass(self) -> None:
        from core.doctor import check_acl_support

        with (
            patch("subprocess.run") as mock_run,
            patch("tempfile.NamedTemporaryFile") as mock_tmp,
        ):
            mock_tmp.return_value.__enter__ = lambda s: MagicMock(name="/tmp/test")
            mock_tmp.return_value.__exit__ = lambda s, *a: None
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            result = check_acl_support("sandbox", None)
            assert result.status == "pass"

    def test_acl_support_fail(self) -> None:
        from core.doctor import check_acl_support

        with (
            patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "setfacl")),
            patch("tempfile.NamedTemporaryFile") as mock_tmp,
        ):
            mock_tmp.return_value.__enter__ = lambda s: MagicMock(name="/tmp/test")
            mock_tmp.return_value.__exit__ = lambda s, *a: None
            result = check_acl_support("sandbox", None)
            assert result.status == "fail"


class TestCheckAncestorTraverse:
    def test_pass_all_traversable(self) -> None:
        from core.doctor import check_ancestor_traverse

        traversable = _make_stat(uid=0, gid=0, mode=0o755)

        with (
            patch("core.doctor.checks.filesystem.sandbox_ai_home", return_value=Path("/synthetic/project")),
            patch("pwd.getpwnam", return_value=_mock_pwd("sandbox")),
            patch("os.stat", return_value=traversable),
        ):
            result = check_ancestor_traverse("sandbox", None)
            assert result.status == "pass"
            assert "traversable" in result.detail

    def test_fail_user_not_found(self) -> None:
        from core.doctor import check_ancestor_traverse

        with (
            patch("core.doctor.checks.filesystem.sandbox_ai_home", return_value=Path("/synthetic/project")),
            patch("pwd.getpwnam", side_effect=KeyError("nonexistent")),
        ):
            result = check_ancestor_traverse("nonexistent_user_xyz", None)
            assert result.status == "fail"
            assert "does not exist" in result.detail

    def test_fail_when_blocked_and_a_sandbox_is_running(self) -> None:
        # Blocked ancestor WHILE a sandbox is running → real FAIL (it cannot
        # reach its workspace). _no_sandbox_running → False.
        from core.doctor import check_ancestor_traverse

        blocked = _make_stat(uid=0, gid=0, mode=0o700)
        traversable = _make_stat(uid=0, gid=0, mode=0o755)

        def controlled_stat(path: str) -> MagicMock:
            if path == "/synthetic":
                return blocked
            return traversable

        with (
            patch("core.doctor.checks.filesystem.sandbox_ai_home", return_value=Path("/synthetic/project")),
            patch("pwd.getpwnam", return_value=_mock_pwd("sandbox")),
            patch("os.stat", side_effect=controlled_stat),
            patch("core.doctor.checks.filesystem._no_sandbox_running", return_value=False),
        ):
            result = check_ancestor_traverse("sandbox", None)
            assert result.status == "fail"
            assert "lacks execute" in result.detail
            assert "setfacl" in (result.remediation or "")

    def test_skip_when_blocked_and_no_sandbox_running(self) -> None:
        # Blocked ancestor but NO sandbox running → the traverse ACL is a
        # first-`sandbox start` grant; report SKIP (not FAIL), not yet applicable.
        from core.doctor import check_ancestor_traverse

        blocked = _make_stat(uid=0, gid=0, mode=0o700)
        traversable = _make_stat(uid=0, gid=0, mode=0o755)

        def controlled_stat(path: str) -> MagicMock:
            return blocked if path == "/synthetic" else traversable

        with (
            patch("core.doctor.checks.filesystem.sandbox_ai_home", return_value=Path("/synthetic/project")),
            patch("pwd.getpwnam", return_value=_mock_pwd("sandbox")),
            patch("os.stat", side_effect=controlled_stat),
            patch("core.doctor.checks.filesystem._no_sandbox_running", return_value=True),
        ):
            result = check_ancestor_traverse("sandbox", None)
            assert result.status == "skip"
            assert "first 'sandbox start'" in result.detail


class TestAncestorTraverseEdgeCases:
    def test_symlink_divergence_warns(self) -> None:
        from core.doctor import check_ancestor_traverse

        traversable = _make_stat(uid=0, gid=0, mode=0o755)

        with (
            patch("core.doctor.checks.filesystem.sandbox_ai_home", return_value=Path("/synthetic/project")),
            patch("pwd.getpwnam", return_value=_mock_pwd("sandbox")),
            patch("os.stat", return_value=traversable),
            patch("os.path.realpath", return_value="/other/real/path"),
        ):
            result = check_ancestor_traverse("sandbox", None)
            assert result.status == "warn"
            assert "Symlink divergence" in result.detail

    def test_oserror_on_stat_returns_fail(self) -> None:
        from core.doctor import check_ancestor_traverse

        traversable = _make_stat(uid=0, gid=0, mode=0o755)

        def controlled_stat(path: str) -> MagicMock:
            if path == "/synthetic":
                raise OSError("permission denied")
            return traversable

        with (
            patch("core.doctor.checks.filesystem.sandbox_ai_home", return_value=Path("/synthetic/project")),
            patch("pwd.getpwnam", return_value=_mock_pwd("sandbox")),
            patch("os.stat", side_effect=controlled_stat),
        ):
            result = check_ancestor_traverse("sandbox", None)
            assert result.status == "fail"
            assert "Cannot stat" in result.detail

    def test_group_exec_branch(self) -> None:
        from core.doctor import check_ancestor_traverse

        group_match = _make_stat(uid=9999, gid=2000, mode=0o750)
        traversable = _make_stat(uid=0, gid=0, mode=0o755)

        def controlled_stat(path: str) -> MagicMock:
            if path == "/synthetic":
                return group_match
            return traversable

        with (
            patch("core.doctor.checks.filesystem.sandbox_ai_home", return_value=Path("/synthetic/project")),
            patch("pwd.getpwnam", return_value=_mock_pwd("sandbox", uid=2000, gid=2000)),
            patch("os.stat", side_effect=controlled_stat),
        ):
            result = check_ancestor_traverse("sandbox", None)
            assert result.status == "pass"

    def test_user_owner_exec_branch(self) -> None:
        from core.doctor import check_ancestor_traverse

        user_owned = _make_stat(uid=2000, gid=2000, mode=0o700)
        traversable = _make_stat(uid=0, gid=0, mode=0o755)

        def controlled_stat(path: str) -> MagicMock:
            if path == "/synthetic":
                return user_owned
            return traversable

        with (
            patch("core.doctor.checks.filesystem.sandbox_ai_home", return_value=Path("/synthetic/project")),
            patch("pwd.getpwnam", return_value=_mock_pwd("sandbox", uid=2000, gid=2000)),
            patch("os.stat", side_effect=controlled_stat),
        ):
            result = check_ancestor_traverse("sandbox", None)
            assert result.status == "pass"


class TestHasAclExec:
    def test_acl_exec_found(self) -> None:
        from core.doctor.checks.filesystem import _has_acl_exec

        getfacl_output = (
            "# file: /home/dev\n"
            "# owner: dev\n"
            "# group: dev\n"
            "user::rwx\n"
            "user:sandbox:--x\n"
            "group::r-x\n"
            "mask::r-x\n"
            "other::---\n"
        )
        mock_result = subprocess.CompletedProcess([], 0, stdout=getfacl_output, stderr="")
        with patch("subprocess.run", return_value=mock_result):
            assert _has_acl_exec("/home/dev", "sandbox") is True

    def test_acl_no_exec(self) -> None:
        from core.doctor.checks.filesystem import _has_acl_exec

        getfacl_output = "user::rwx\nuser:sandbox:r--\nother::---\n"
        mock_result = subprocess.CompletedProcess([], 0, stdout=getfacl_output, stderr="")
        with patch("subprocess.run", return_value=mock_result):
            assert _has_acl_exec("/home/dev", "sandbox") is False

    def test_acl_user_not_present(self) -> None:
        from core.doctor.checks.filesystem import _has_acl_exec

        getfacl_output = "user::rwx\ngroup::r-x\nother::---\n"
        mock_result = subprocess.CompletedProcess([], 0, stdout=getfacl_output, stderr="")
        with patch("subprocess.run", return_value=mock_result):
            assert _has_acl_exec("/home/dev", "sandbox") is False

    def test_getfacl_nonzero_returns_false(self) -> None:
        from core.doctor.checks.filesystem import _has_acl_exec

        mock_result = subprocess.CompletedProcess([], 1, stdout="", stderr="error")
        with patch("subprocess.run", return_value=mock_result):
            assert _has_acl_exec("/home/dev", "sandbox") is False

    def test_getfacl_timeout_returns_false(self) -> None:
        from core.doctor.checks.filesystem import _has_acl_exec

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("getfacl", 5)):
            assert _has_acl_exec("/home/dev", "sandbox") is False

    def test_getfacl_oserror_returns_false(self) -> None:
        from core.doctor.checks.filesystem import _has_acl_exec

        with patch("subprocess.run", side_effect=OSError("not found")):
            assert _has_acl_exec("/home/dev", "sandbox") is False


class TestAncestorTraverseWithAclFallback:
    def test_mode_deny_acl_grants_passes(self) -> None:
        from core.doctor import check_ancestor_traverse

        blocked = _make_stat(uid=0, gid=0, mode=0o700)
        traversable = _make_stat(uid=0, gid=0, mode=0o755)

        def controlled_stat(path: str) -> MagicMock:
            if path == "/synthetic":
                return blocked
            return traversable

        getfacl_output = "user::rwx\nuser:sandbox:--x\nother::---\n"
        mock_getfacl = subprocess.CompletedProcess([], 0, stdout=getfacl_output, stderr="")

        def controlled_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return mock_getfacl

        with (
            patch("core.doctor.checks.filesystem.sandbox_ai_home", return_value=Path("/synthetic/project")),
            patch("pwd.getpwnam", return_value=_mock_pwd("sandbox")),
            patch("os.stat", side_effect=controlled_stat),
            patch("subprocess.run", side_effect=controlled_run),
        ):
            result = check_ancestor_traverse("sandbox", None)
            assert result.status == "pass"

    def test_mode_deny_acl_deny_fails(self) -> None:
        from core.doctor import check_ancestor_traverse

        blocked = _make_stat(uid=0, gid=0, mode=0o700)
        traversable = _make_stat(uid=0, gid=0, mode=0o755)

        def controlled_stat(path: str) -> MagicMock:
            if path == "/synthetic":
                return blocked
            return traversable

        getfacl_output = "user::rwx\nother::---\n"
        mock_getfacl = subprocess.CompletedProcess([], 0, stdout=getfacl_output, stderr="")

        with (
            patch("core.doctor.checks.filesystem.sandbox_ai_home", return_value=Path("/synthetic/project")),
            patch("pwd.getpwnam", return_value=_mock_pwd("sandbox")),
            patch("os.stat", side_effect=controlled_stat),
            patch("subprocess.run", return_value=mock_getfacl),
            patch("core.doctor.checks.filesystem._no_sandbox_running", return_value=False),
        ):
            result = check_ancestor_traverse("sandbox", None)
            assert result.status == "fail"
            assert "lacks execute" in result.detail


class TestNoSandboxRunning:
    def test_true_when_zero_projects(self, monkeypatch: Any) -> None:
        from core.dispatch import ProbeOutcome
        from core.doctor.checks.filesystem import _no_sandbox_running
        from core.host_config import DockerExecutionMode, MachinectlAuth

        monkeypatch.setattr(
            "core.dispatch.probe",
            lambda *a, **k: ProbeOutcome(ok=True, timed_out=False, stdout="[]", message=""),
        )
        assert _no_sandbox_running("sandbox", MachinectlAuth.SUDO, DockerExecutionMode.SEPARATE_USER) is True

    def test_false_when_projects_present(self, monkeypatch: Any) -> None:
        from core.dispatch import ProbeOutcome
        from core.doctor.checks.filesystem import _no_sandbox_running
        from core.host_config import DockerExecutionMode, MachinectlAuth

        monkeypatch.setattr(
            "core.dispatch.probe",
            lambda *a, **k: ProbeOutcome(ok=True, timed_out=False, stdout='[{"Name": "sandbox-x"}]', message=""),
        )
        assert _no_sandbox_running("sandbox", MachinectlAuth.SUDO, DockerExecutionMode.SEPARATE_USER) is False

    def test_false_when_probe_not_ok(self, monkeypatch: Any) -> None:
        from core.dispatch import ProbeOutcome
        from core.doctor.checks.filesystem import _no_sandbox_running
        from core.host_config import DockerExecutionMode, MachinectlAuth

        monkeypatch.setattr(
            "core.dispatch.probe",
            lambda *a, **k: ProbeOutcome(ok=False, timed_out=False, stdout="", message="docker unreachable"),
        )
        assert _no_sandbox_running("sandbox", MachinectlAuth.SUDO, DockerExecutionMode.SEPARATE_USER) is False

    def test_false_when_unparseable(self, monkeypatch: Any) -> None:
        from core.dispatch import ProbeOutcome
        from core.doctor.checks.filesystem import _no_sandbox_running
        from core.host_config import DockerExecutionMode, MachinectlAuth

        monkeypatch.setattr(
            "core.dispatch.probe",
            lambda *a, **k: ProbeOutcome(ok=True, timed_out=False, stdout="not json{{{", message=""),
        )
        assert _no_sandbox_running("sandbox", MachinectlAuth.SUDO, DockerExecutionMode.SEPARATE_USER) is False

    def test_false_when_json_not_a_list(self, monkeypatch: Any) -> None:
        # Valid JSON that decodes to a non-list (e.g. a daemon that emits an
        # object on error). The ``isinstance(…, list)`` guard must fail-safe to
        # False (report the real traverse gap, not hide it behind a SKIP).
        from core.dispatch import ProbeOutcome
        from core.doctor.checks.filesystem import _no_sandbox_running
        from core.host_config import DockerExecutionMode, MachinectlAuth

        monkeypatch.setattr(
            "core.dispatch.probe",
            lambda *a, **k: ProbeOutcome(ok=True, timed_out=False, stdout="{}", message=""),
        )
        assert _no_sandbox_running("sandbox", MachinectlAuth.SUDO, DockerExecutionMode.SEPARATE_USER) is False
