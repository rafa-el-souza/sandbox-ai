"""Empirical validation of the workspace-backup rsync recipe (D8).

Verifies that ``core.workspace_backups.create_backup`` strips runtime ACL/
group/setgid state (per `cli-workspace`'s "Workspace Backup Recipe") while
preserving the file tree structure and the executable bit.

Skips when ``rsync``, ``setfacl``, or ``getfacl`` are unavailable, or when
the test filesystem does not support POSIX ACLs.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _binary_available(name: str) -> bool:
    return shutil.which(name) is not None


def _fs_supports_acls(probe_dir: Path) -> bool:
    """True iff setfacl can apply a named ACL to a probe dir under ``probe_dir``."""
    target = probe_dir / "_acl-probe"
    target.mkdir()
    try:
        subprocess.run(
            ["setfacl", "-m", f"u:{os.getenv('USER', 'root')}:rwx", str(target)],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    finally:
        shutil.rmtree(target, ignore_errors=True)
    return True


@pytest.mark.skipif(not _binary_available("rsync"), reason="rsync not on PATH")
@pytest.mark.skipif(not _binary_available("setfacl"), reason="setfacl not on PATH")
def test_backup_strips_acl_setgid_and_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Source workspace at mode 2770 sb-ws + named ACL + user xattr →
    backup tree at mode 0700 dirs / 0600 files / no ACL / dev:dev ownership."""
    monkeypatch.setenv("SANDBOX_AI_HOME", str(tmp_path / ".sandbox-ai"))
    if not _fs_supports_acls(tmp_path):
        pytest.skip("filesystem does not support POSIX ACLs")

    src = tmp_path / "live-workspace"
    src.mkdir(mode=0o770)
    (src / "subdir").mkdir(mode=0o770)
    (src / "file.txt").write_text("payload")
    (src / "script.sh").write_text("#!/bin/sh\necho hi\n")
    os.chmod(src / "script.sh", 0o770)

    # Set setgid + named ACL (best-effort — caller must own the group).
    try:
        os.chmod(src, 0o2770)
        subprocess.run(
            ["setfacl", "-m", f"u:{os.getenv('USER', 'root')}:rwx", str(src)],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        pytest.skip("could not apply test ACL/setgid to source")

    from core.workspace_backups import create_backup

    info = create_backup(
        instance_name="testinst",
        workspace_name="main",
        source_path=str(src),
        source_bootstrap_mode="empty",
        dev_primary_gid=os.getgid(),
    )

    # Directories: mode 0700, no setgid.
    for dirpath, dirnames, _ in os.walk(info.path):
        if dirpath == str(info.path):
            continue
        for d in dirnames:
            full = os.path.join(dirpath, d)
            mode = stat.S_IMODE(os.lstat(full).st_mode)
            assert mode & 0o7777 == 0o700, f"{full} mode {mode:04o} != 0700"

    # Files: 0600 or 0700 (executable bit preserved on owner only).
    file_path = info.path / "file.txt"
    script_path = info.path / "script.sh"
    assert file_path.exists()
    assert script_path.exists()
    assert stat.S_IMODE(os.lstat(file_path).st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(script_path).st_mode) == 0o700

    # No POSIX ACL entries on the backup tree.
    if _binary_available("getfacl"):
        result = subprocess.run(
            ["getfacl", "--omit-header", "-c", str(info.path / "file.txt")],
            check=True,
            capture_output=True,
            text=True,
        )
        # Default ACL output for plain mode bits has only user::, group::, other::.
        lines = [line for line in result.stdout.strip().splitlines() if line]
        assert all(line.split(":")[0] in ("user", "group", "other") for line in lines)
        # Strict: no named-user entry (would have a non-empty middle field).
        for line in lines:
            if line.startswith("user:"):
                assert line.split(":")[1] == "", f"named user entry survived: {line!r}"

    # Ownership stripped to dev's primary group.
    assert os.lstat(info.path / "file.txt").st_gid == os.getgid()


@pytest.mark.skipif(not _binary_available("rsync"), reason="rsync not on PATH")
def test_backup_preserves_executable_bit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOX_AI_HOME", str(tmp_path / ".sandbox-ai"))
    src = tmp_path / "src"
    src.mkdir()
    exe = src / "run.sh"
    exe.write_text("#!/bin/sh\n")
    os.chmod(exe, 0o755)

    from core.workspace_backups import create_backup

    info = create_backup(
        instance_name="i",
        workspace_name="w",
        source_path=str(src),
        source_bootstrap_mode="empty",
        dev_primary_gid=os.getgid(),
    )
    assert stat.S_IMODE(os.lstat(info.path / "run.sh").st_mode) == 0o700
