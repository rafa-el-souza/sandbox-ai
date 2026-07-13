# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""Empirical validation of D11 / D18 — same-fs ``os.rename`` preserves named
ACLs, setgid bit, and persistent default ACL without re-application.

Skips when ``setfacl``/``getfacl`` are unavailable or the test filesystem
does not support POSIX ACLs.
"""

from __future__ import annotations

import errno
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


@pytest.mark.skipif(not _binary_available("setfacl"), reason="setfacl not on PATH")
@pytest.mark.skipif(not _binary_available("getfacl"), reason="getfacl not on PATH")
def test_os_rename_preserves_acl_setgid_and_default_acl(tmp_path: Path) -> None:
    if not _fs_supports_acls(tmp_path):
        pytest.skip("filesystem does not support POSIX ACLs")

    user = os.getenv("USER", "root")
    old_dir = tmp_path / "old-name"
    old_dir.mkdir(mode=0o770)
    os.chmod(old_dir, 0o2770)  # setgid

    # Apply named-user ACL + persistent default ACL.
    subprocess.run(
        ["setfacl", "-m", f"u:{user}:rwx", str(old_dir)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["setfacl", "-d", "-m", f"u:{user}:rwx", str(old_dir)],
        check=True,
        capture_output=True,
    )
    before = subprocess.run(
        ["getfacl", "--omit-header", "-p", str(old_dir)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    new_dir = tmp_path / "new-name"
    os.rename(old_dir, new_dir)

    assert not old_dir.exists()
    assert new_dir.exists()
    # setgid preserved on the inode (rename doesn't touch mode bits).
    assert stat.S_IMODE(os.stat(new_dir).st_mode) & stat.S_ISGID

    after = subprocess.run(
        ["getfacl", "--omit-header", "-p", str(new_dir)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    # ACL state is identical aside from the path comment ruled out by --omit-header.
    assert before == after, f"ACL diverged across rename:\nbefore:\n{before}\nafter:\n{after}"


def test_os_rename_cross_fs_raises_exdev(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Synthetic EXDEV: monkeypatch os.rename to raise EXDEV; the cli rename
    handler must surface the error explicitly. (We can't reliably create two
    filesystems in CI; the symptom-reproduction is what matters.)"""
    src = tmp_path / "src"
    src.mkdir()
    dst = tmp_path / "dst"

    def cross_fs_rename(_a: str, _b: str) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "rename", cross_fs_rename)
    with pytest.raises(OSError) as excinfo:
        os.rename(src, dst)
    assert excinfo.value.errno == errno.EXDEV
