# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for `core.ipam` lock topology — regression guards for the lock layout.

Guards two invariants:

1. ``IPAMLedger.allocate`` does NOT self-deadlock against the per-user
   ``state.lock`` — the IPAM mutation lock is a distinct file
   (``ipam.json.lock``) so a caller may legitimately hold ``state.lock``
   while invoking IPAM mutation paths.
2. ``state_lock_path()`` and ``ipam_lock_path()`` are path-disjoint —
   regression guard against accidental re-unification onto a single file.
"""

import fcntl
import os
from pathlib import Path

from core.host_config import ipam_lock_path, state_lock_path
from core.ipam import IPAMLedger


def test_allocate_succeeds_while_outer_state_lock_held(isolated_sandbox_ai_home: Path) -> None:
    """`IPAMLedger.allocate` MUST NOT raise when the caller already holds `state.lock`."""
    state_lock = state_lock_path()
    os.makedirs(state_lock.parent, exist_ok=True)
    outer_fd = os.open(str(state_lock), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(outer_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        base_index = IPAMLedger().allocate("inst-under-outer-lock")
        assert isinstance(base_index, int)
        assert base_index == 0
    finally:
        fcntl.flock(outer_fd, fcntl.LOCK_UN)
        os.close(outer_fd)


def test_state_and_ipam_lock_paths_are_disjoint(isolated_sandbox_ai_home: Path) -> None:
    """The two lock files MUST resolve to distinct paths."""
    assert state_lock_path() != ipam_lock_path()
    assert state_lock_path().name == "state.lock"
    assert ipam_lock_path().name == "ipam.json.lock"
