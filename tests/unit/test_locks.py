"""Tests for core.locks — per-instance backup lock acquisition + stale detection."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import multiprocessing as mp
import os
from pathlib import Path

import pytest
from core.locks import (
    STALE_GRACE_SECONDS,
    BackupLockHeldError,
    acquire_backup_lock,
    backup_lock_path,
    is_lock_stale,
)


def _hold_lock(path: str, ready: object, release: object) -> None:
    """Worker: open the lock, hold LOCK_EX, signal ready, wait for release."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    started = dt.datetime.now(tz=dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    os.write(fd, json.dumps({"pid": os.getpid(), "started_at_utc": started}).encode())
    ready.set()  # type: ignore[attr-defined]
    release.wait()  # type: ignore[attr-defined]
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


class TestBackupLockPath:
    def test_path_under_state_dir(self, isolated_sandbox_ai_home: Path) -> None:
        path = backup_lock_path("myinst")
        assert path == isolated_sandbox_ai_home / "state" / "myinst.backup.lock"


class TestAcquireBackupLock:
    def test_acquires_and_writes_metadata(self, isolated_sandbox_ai_home: Path) -> None:
        del isolated_sandbox_ai_home
        with acquire_backup_lock("myinst") as lock_path:
            content = json.loads(lock_path.read_bytes())
            assert content["pid"] == os.getpid()
            assert "started_at_utc" in content

    def test_clears_metadata_on_exit(self, isolated_sandbox_ai_home: Path) -> None:
        del isolated_sandbox_ai_home
        with acquire_backup_lock("myinst") as lock_path:
            assert lock_path.read_bytes()
        assert lock_path.read_bytes() == b""

    def test_raises_when_held_by_another_process(self, isolated_sandbox_ai_home: Path) -> None:
        path = backup_lock_path("contended")
        os.makedirs(path.parent, exist_ok=True)
        ctx = mp.get_context("fork")
        ready = ctx.Event()
        release = ctx.Event()
        proc = ctx.Process(target=_hold_lock, args=(str(path), ready, release))
        proc.start()
        try:
            assert ready.wait(timeout=5)
            with pytest.raises(BackupLockHeldError), acquire_backup_lock("contended"):
                pass
        finally:
            release.set()
            proc.join(timeout=5)

    def test_stale_lock_is_taken_over(self, isolated_sandbox_ai_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = backup_lock_path("stale")
        os.makedirs(path.parent, exist_ok=True)
        # Seed an old metadata record without holding flock — simulate a
        # crashed prior holder. Since flock is process-scoped we can simply
        # write metadata; flock will succeed and the in-walk staleness check
        # is bypassed (lock not held).
        old_ts = (dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=STALE_GRACE_SECONDS + 30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        path.write_text(json.dumps({"pid": 99999, "started_at_utc": old_ts}))
        with acquire_backup_lock("stale") as p:
            content = json.loads(p.read_bytes())
            assert content["pid"] == os.getpid()

    def test_stale_takeover_when_holder_present(
        self, isolated_sandbox_ai_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the lock is held but the metadata is older than the grace, take over."""
        path = backup_lock_path("contended-stale")
        os.makedirs(path.parent, exist_ok=True)
        ctx = mp.get_context("fork")
        ready = ctx.Event()
        release = ctx.Event()
        proc = ctx.Process(target=_hold_lock, args=(str(path), ready, release))
        proc.start()
        try:
            assert ready.wait(timeout=5)
            # Override is_lock_stale to report stale, exercising the takeover branch.
            monkeypatch.setattr("core.locks.is_lock_stale", lambda *_a, **_kw: True)
            # The fcntl lock is still held by the child, so LOCK_EX|LOCK_NB
            # will keep failing. The takeover branch retries once and then
            # surfaces an OSError when the fcntl call still fails — assert
            # we don't silently succeed against a held lock.
            with pytest.raises(OSError), acquire_backup_lock("contended-stale"):
                pass
        finally:
            release.set()
            proc.join(timeout=5)

    def test_oserror_other_than_eagain_propagates(
        self, isolated_sandbox_ai_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        del isolated_sandbox_ai_home

        def fake_flock(_fd: int, _flags: int) -> None:
            raise OSError(13, "permission denied")

        monkeypatch.setattr("core.locks.fcntl.flock", fake_flock)
        with pytest.raises(OSError), acquire_backup_lock("perm"):
            pass


class TestIsLockStale:
    def test_missing_file_is_stale(self, tmp_path: Path) -> None:
        assert is_lock_stale(tmp_path / "missing.lock")

    def test_empty_file_is_stale(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.lock"
        path.write_text("")
        assert is_lock_stale(path)

    def test_malformed_json_is_stale(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.lock"
        path.write_text("{not json")
        assert is_lock_stale(path)

    def test_non_dict_top_level_is_stale(self, tmp_path: Path) -> None:
        path = tmp_path / "list.lock"
        path.write_text(json.dumps([1, 2]))
        assert is_lock_stale(path)

    def test_missing_started_at_is_stale(self, tmp_path: Path) -> None:
        path = tmp_path / "nots.lock"
        path.write_text(json.dumps({"pid": 1}))
        assert is_lock_stale(path)

    def test_non_string_started_at_is_stale(self, tmp_path: Path) -> None:
        path = tmp_path / "intts.lock"
        path.write_text(json.dumps({"pid": 1, "started_at_utc": 12345}))
        assert is_lock_stale(path)

    def test_invalid_iso_string_is_stale(self, tmp_path: Path) -> None:
        path = tmp_path / "badiso.lock"
        path.write_text(json.dumps({"pid": 1, "started_at_utc": "not-a-date"}))
        assert is_lock_stale(path)

    def test_recent_record_not_stale(self, tmp_path: Path) -> None:
        path = tmp_path / "fresh.lock"
        now_iso = dt.datetime.now(tz=dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        path.write_text(json.dumps({"pid": 1, "started_at_utc": now_iso}))
        assert not is_lock_stale(path)

    def test_old_record_is_stale(self, tmp_path: Path) -> None:
        path = tmp_path / "old.lock"
        old = (dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=STALE_GRACE_SECONDS + 5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        path.write_text(json.dumps({"pid": 1, "started_at_utc": old}))
        assert is_lock_stale(path)
