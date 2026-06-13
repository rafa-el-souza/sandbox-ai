# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Empirical validation of the rsync ``-X`` xattrs runtime probe.

`core.workspace_backups.rsync_supports_xattrs()` parses ``rsync --version``
output for the ``xattrs`` capability tag and caches the result. The flag
inclusion (or omission) in the recipe must match what the host's rsync
actually supports.

These tests run against the real rsync on PATH:

1. The probe's verdict matches what ``rsync --version`` actually says about
   xattrs support.
2. ``create_backup`` includes ``-X`` in its rsync invocation iff the probe
   reports True.
3. With xattrs supported (typical on Linux + ext4/btrfs/xfs), a user xattr
   on the source file survives into the backup tree.

Skips when ``rsync`` is unavailable or the test filesystem does not support
user xattrs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _binary_available(name: str) -> bool:
    return shutil.which(name) is not None


def _fs_supports_user_xattrs(probe_dir: Path) -> bool:
    """True iff a user.* xattr can be set and read back on a probe file."""
    f = probe_dir / "_xattr-probe"
    f.write_text("x")
    try:
        os.setxattr(str(f), b"user.test", b"v")
        return os.getxattr(str(f), b"user.test") == b"v"
    except OSError:
        return False
    finally:
        f.unlink(missing_ok=True)


@pytest.mark.skipif(not _binary_available("rsync"), reason="rsync not on PATH")
def test_probe_matches_rsync_version_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cached probe verdict matches what `rsync --version` actually reports."""
    from core import workspace_backups as wb

    wb.reset_rsync_caches()
    monkeypatch.delenv("SANDBOX_AI_HOME", raising=False)  # probe doesn't need home

    raw = subprocess.run(
        ["rsync", "--version"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    expected = " xattrs" in raw and "no xattrs" not in raw
    assert wb.rsync_supports_xattrs() is expected


@pytest.mark.skipif(not _binary_available("rsync"), reason="rsync not on PATH")
def test_recipe_includes_x_flag_iff_probe_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rsync command emitted by `_build_rsync_cmd` matches the probe."""
    from core import workspace_backups as wb

    wb.reset_rsync_caches()
    use_xattrs = wb.rsync_supports_xattrs()
    cmd = wb._build_rsync_cmd(
        "/src",
        "/dest.partial",
        excludes=(),
        extra_excludes=(),
        safe_links=False,
        use_xattrs=use_xattrs,
    )
    if use_xattrs:
        assert "-X" in cmd, "probe says xattrs supported but recipe omits -X"
    else:
        assert "-X" not in cmd, "probe says xattrs unsupported but recipe includes -X"


@pytest.mark.skipif(not _binary_available("rsync"), reason="rsync not on PATH")
def test_user_xattr_round_trips_when_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a user.* xattr on the source survives into the backup tree
    when the host rsync supports xattrs and the destination filesystem
    preserves them. Skipped otherwise."""
    from core import workspace_backups as wb

    wb.reset_rsync_caches()
    if not wb.rsync_supports_xattrs():
        pytest.skip("rsync built without xattrs support")
    if not _fs_supports_user_xattrs(tmp_path):
        pytest.skip("filesystem does not support user xattrs")

    monkeypatch.setenv("SANDBOX_AI_HOME", str(tmp_path / ".sandbox-ai"))

    src = tmp_path / "src"
    src.mkdir()
    payload = src / "tagged.txt"
    payload.write_text("data")
    os.setxattr(str(payload), b"user.kind", b"important")

    info = wb.create_backup(
        instance_name="i",
        workspace_name="w",
        source_path=str(src),
        source_bootstrap_mode="empty",
        dev_primary_gid=os.getgid(),
    )
    restored_xattr = os.getxattr(str(info.path / "tagged.txt"), b"user.kind")
    assert restored_xattr == b"important"
