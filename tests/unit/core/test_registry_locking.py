"""Tests for `core.registry` lock topology — regression guards for the lock layout.

Guards two invariants:

1. ``InstanceRegistry.remove`` (and ``register``) does NOT self-deadlock
   against the per-user ``state.lock`` — the registry mutation lock is a
   distinct file (``instances.json.lock``) so a caller may legitimately
   hold ``state.lock`` while invoking registry mutation paths
   (e.g. ``sandbox destroy``).
2. ``state_lock_path()`` and ``registry_lock_path()`` are path-disjoint —
   regression guard against accidental re-unification onto a single file.

The deadlock-regression test uses a SIGALRM watchdog because pre-fix
behavior is "hang forever" (registry's ``_open_lock`` issues a blocking
``fcntl.flock(LOCK_EX)`` without ``LOCK_NB``); a watchdog converts the
hang into a fast-failing assertion rather than letting the test run
exhaust the CI budget.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
from pathlib import Path
from types import FrameType

import pytest
from core.host_config import registry_lock_path, state_lock_path
from core.registry import InstanceRegistry


class _WatchdogTimeout(Exception):
    """Raised by the SIGALRM watchdog when a test would otherwise hang."""


def _arm_watchdog(seconds: int) -> None:
    def _handler(_signum: int, _frame: FrameType | None) -> None:
        raise _WatchdogTimeout(f"watchdog fired after {seconds}s — likely self-deadlock")

    signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)


def _disarm_watchdog() -> None:
    signal.alarm(0)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)


def test_remove_succeeds_while_outer_state_lock_held(isolated_sandbox_ai_home: Path) -> None:
    """`InstanceRegistry.remove` MUST NOT hang when the caller already holds `state.lock`.

    Pre-fix: registry's ``_open_lock`` opens ``state.lock`` on a second FD
    and ``fcntl.flock(LOCK_EX)`` blocks indefinitely. A 2-second SIGALRM
    watchdog converts that hang into a ``_WatchdogTimeout`` so the test
    fails fast rather than stalling the suite.
    """
    state_lock = state_lock_path()
    os.makedirs(state_lock.parent, exist_ok=True)

    # Seed the registry with the entry to remove so the call has work to do.
    registry_path = isolated_sandbox_ai_home / "state" / "instances.json"
    registry_path.write_text(
        json.dumps(
            {
                "test-inst-under-outer-state-lock": {
                    "instance_dir": str(isolated_sandbox_ai_home / "instances" / "test-inst"),
                    "created_at": "2026-05-07T00:00:00Z",
                }
            }
        )
    )

    outer_fd = os.open(str(state_lock), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(outer_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        _arm_watchdog(2)
        try:
            InstanceRegistry().remove("test-inst-under-outer-state-lock")
        finally:
            _disarm_watchdog()

        # Verify the mutation took effect — and that the registry lock file
        # was created lazily and is distinct from state.lock.
        assert json.loads(registry_path.read_text()) == {}
        assert registry_lock_path().exists()
    finally:
        fcntl.flock(outer_fd, fcntl.LOCK_UN)
        os.close(outer_fd)


def test_state_and_registry_lock_paths_are_disjoint(isolated_sandbox_ai_home: Path) -> None:
    """The two lock files MUST resolve to distinct paths."""
    assert state_lock_path() != registry_lock_path()
    assert state_lock_path().name == "state.lock"
    assert registry_lock_path().name == "instances.json.lock"


def test_register_succeeds_while_outer_state_lock_held(isolated_sandbox_ai_home: Path) -> None:
    """`InstanceRegistry.register` MUST NOT hang when the caller already holds `state.lock`.

    Symmetric companion to the ``remove`` regression: both mutators route
    through the same ``_open_lock`` and so share the deadlock surface.
    """
    state_lock = state_lock_path()
    os.makedirs(state_lock.parent, exist_ok=True)

    outer_fd = os.open(str(state_lock), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(outer_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        _arm_watchdog(2)
        try:
            entry = InstanceRegistry().register(
                "fresh-under-outer-state-lock",
                str(isolated_sandbox_ai_home / "instances" / "fresh"),
            )
        finally:
            _disarm_watchdog()

        assert entry.instance_dir.endswith("instances/fresh")
        assert registry_lock_path().exists()
    finally:
        fcntl.flock(outer_fd, fcntl.LOCK_UN)
        os.close(outer_fd)


def test_watchdog_assertion_marker(isolated_sandbox_ai_home: Path) -> None:
    """Sanity guard: the watchdog actually fires when a hang is simulated.

    Confirms the deadlock-regression test would observe a watchdog timeout
    if the underlying bug regressed (rather than silently passing because
    the watchdog is broken). Uses ``signal.pause()`` to simulate a hang.
    """
    _arm_watchdog(1)
    try:
        with pytest.raises(_WatchdogTimeout):
            signal.pause()
    finally:
        _disarm_watchdog()
