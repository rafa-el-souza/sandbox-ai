# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""Empirical validation of G13 — chmod-after-setfacl mask collapse.

Locks the rule: ``setfacl`` must be applied AFTER ``chmod``. Otherwise the
chmod resets the ACL mask to the chmod's group bits, which collapses any
previously-granted named-user ACL entry's effective permissions.

Concretely:

* ``mkdir 0700 dir; setfacl -m u:claude:--x dir`` → succeeds; the mask is
  auto-set to ``--x`` so the named entry is effective.
* ``setfacl -m u:claude:--x dir; chmod 0700 dir`` → the chmod collapses the
  mask to ``---``; the named entry is masked out and the daemon cannot
  traverse.

Skips when ``setfacl``/``getfacl`` are unavailable or the test filesystem
does not support POSIX ACLs.
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


def _mask_line(facl_output: str) -> str | None:
    """Return the ``mask::<bits>`` line from a getfacl output (without the
    header) or None if absent."""
    for line in facl_output.splitlines():
        if line.startswith("mask::"):
            return line
    return None


@pytest.mark.skipif(not _binary_available("setfacl"), reason="setfacl not on PATH")
@pytest.mark.skipif(not _binary_available("getfacl"), reason="getfacl not on PATH")
def test_setfacl_after_chmod_yields_effective_mask(tmp_path: Path) -> None:
    """Order: chmod first, setfacl second → mask matches the named entry's
    granted bits → effective permissions == granted permissions."""
    if not _fs_supports_acls(tmp_path):
        pytest.skip("filesystem does not support POSIX ACLs")

    user = os.getenv("USER", "root")
    target = tmp_path / "chmod-then-setfacl"
    target.mkdir(mode=0o700)

    subprocess.run(
        ["setfacl", "-m", f"u:{user}:--x", str(target)],
        check=True,
        capture_output=True,
    )
    facl = subprocess.run(
        ["getfacl", "--omit-header", str(target)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    mask = _mask_line(facl)
    assert mask is not None, f"no mask line in:\n{facl}"
    # Mask covers the named entry's --x grant.
    assert "x" in mask.split("::", 1)[1], f"mask collapsed unexpectedly: {mask!r}"
    # The named entry has no `#effective:` annotation (no mask collapse).
    user_line = next(line for line in facl.splitlines() if line.startswith(f"user:{user}:"))
    assert "#effective" not in user_line, f"named entry was masked: {user_line!r}"


@pytest.mark.skipif(not _binary_available("setfacl"), reason="setfacl not on PATH")
@pytest.mark.skipif(not _binary_available("getfacl"), reason="getfacl not on PATH")
def test_chmod_after_setfacl_collapses_mask(tmp_path: Path) -> None:
    """Order: setfacl first, chmod second → chmod resets the mask to the
    group bits (---), masking out the named entry's effective permissions."""
    if not _fs_supports_acls(tmp_path):
        pytest.skip("filesystem does not support POSIX ACLs")

    user = os.getenv("USER", "root")
    target = tmp_path / "setfacl-then-chmod"
    target.mkdir(mode=0o700)

    subprocess.run(
        ["setfacl", "-m", f"u:{user}:--x", str(target)],
        check=True,
        capture_output=True,
    )
    # The chmod-induced collapse: 0o700 has no group bits → mask becomes ---.
    os.chmod(target, 0o700)

    facl = subprocess.run(
        ["getfacl", "--omit-header", str(target)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    mask = _mask_line(facl)
    assert mask is not None, f"no mask line in:\n{facl}"
    # Mask collapsed to no permissions (or annotated as such).
    mask_bits = mask.split("::", 1)[1]
    assert "x" not in mask_bits, f"expected mask to lose execute, got {mask!r}"
    # The named entry now carries the `#effective:---` annotation.
    user_line = next(line for line in facl.splitlines() if line.startswith(f"user:{user}:"))
    assert "#effective" in user_line, (
        f"expected mask collapse to annotate named entry, got {user_line!r}"
    )
