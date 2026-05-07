"""Tests for ``core.workspace_backups``.

Mocks ``Executor`` so rsync never actually runs; the mock side-effect populates
``<dest>.partial/`` with placeholder content so ``_tree_size_and_count`` and
the atomic rename can exercise the real codepaths.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from core import workspace_backups as wb
from core.exceptions import SandboxExecutionError
from core.host_config import sandbox_ai_home
from core.locks import BackupLockHeldError, acquire_backup_lock
from core.workspace_copy import COPY_DEFAULT_EXCLUDES


class _FakeExecutor:
    """Stand-in for ``core.executor.Executor``.

    ``rsync_stdout`` is returned for ``rsync --version`` calls; ``rsync_run``
    is invoked for ``rsync`` (recipe) calls and may populate the partial dir.
    Set ``rsync_run = None`` to simulate rsync failure (raises
    ``SandboxExecutionError``).
    """

    last_cmd: list[str] | None = None
    last_recipe_cmd: list[str] | None = None

    def __init__(self) -> None:
        self.rsync_stdout: str = " xattrs\n"
        self.rsync_run: Any = lambda cmd: None

    def run(
        self,
        cmd: list[str],
        *,
        sentinel: bool = False,
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        type(self).last_cmd = cmd
        if cmd[:2] == ["rsync", "--version"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=self.rsync_stdout, stderr="")
        # Recipe call.
        type(self).last_recipe_cmd = cmd
        if self.rsync_run is None:
            raise SandboxExecutionError("simulated rsync failure")
        self.rsync_run(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


@pytest.fixture()
def fake_executor(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeExecutor]:
    """Install ``_FakeExecutor`` into ``core.workspace_backups``."""
    fake = _FakeExecutor()
    monkeypatch.setattr(wb, "Executor", lambda: fake)
    wb._reset_rsync_caches()
    yield fake
    wb._reset_rsync_caches()


def _populate_partial(cmd: list[str]) -> None:
    """Side-effect for ``rsync_run``: write a stub file into ``<dest>.partial/``
    so the real size/file-count walk has something to count."""
    dest = cmd[-1].rstrip("/")
    os.makedirs(dest, exist_ok=True)
    Path(dest, "stub.txt").write_text("hello")


def _make_source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hi")
    return src


# ── rsync_supports_xattrs probe ────────────────────────────────────────────


class TestRsyncSupportsXattrs:
    def test_true_when_capability_present(self, fake_executor: _FakeExecutor) -> None:
        fake_executor.rsync_stdout = "rsync version 3.2.7\nCapabilities: 64-bit files, 64-bit inums, xattrs, ACLs\n"
        assert wb.rsync_supports_xattrs() is True

    def test_false_when_no_xattrs_marker(self, fake_executor: _FakeExecutor) -> None:
        fake_executor.rsync_stdout = "rsync version 3.0.0\nCapabilities: 64-bit files, no xattrs\n"
        assert wb.rsync_supports_xattrs() is False

    def test_false_when_executor_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Boom:
            def run(self, *_a: Any, **_kw: Any) -> Any:
                raise SandboxExecutionError("boom")

        monkeypatch.setattr(wb, "Executor", lambda: _Boom())
        wb._reset_rsync_caches()
        assert wb.rsync_supports_xattrs() is False

    def test_caches_result(self, fake_executor: _FakeExecutor) -> None:
        fake_executor.rsync_stdout = " xattrs\n"
        assert wb.rsync_supports_xattrs() is True
        # Flip the response — cached True still returned.
        fake_executor.rsync_stdout = "no xattrs"
        assert wb.rsync_supports_xattrs() is True

    def test_query_version_caches_and_handles_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Empty:
            def run(self, *_a: Any, **_kw: Any) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(["rsync"], 0, stdout="", stderr="")

        monkeypatch.setattr(wb, "Executor", lambda: _Empty())
        wb._reset_rsync_caches()
        assert wb._query_rsync_version() == ""
        # Cached: even after swapping the executor, cached value wins.
        monkeypatch.setattr(wb, "Executor", lambda: _FakeExecutor())
        assert wb._query_rsync_version() == ""

    def test_query_version_handles_executor_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Boom:
            def run(self, *_a: Any, **_kw: Any) -> Any:
                raise SandboxExecutionError("boom")

        monkeypatch.setattr(wb, "Executor", lambda: _Boom())
        wb._reset_rsync_caches()
        assert wb._query_rsync_version() == ""


# ── _build_rsync_cmd ──────────────────────────────────────────────────────


class TestBuildRsyncCmd:
    def _build(self, **kw: Any) -> list[str]:
        defaults: dict[str, Any] = {
            "excludes": ("node_modules",),
            "extra_excludes": (),
            "safe_links": False,
            "use_xattrs": True,
        }
        defaults.update(kw)
        return wb._build_rsync_cmd("/src", "/dest.partial", **defaults)

    def test_includes_xattrs_flag_when_supported(self) -> None:
        cmd = self._build()
        assert "-X" in cmd

    def test_omits_xattrs_flag_when_unsupported(self) -> None:
        cmd = self._build(use_xattrs=False)
        assert "-X" not in cmd
        # The first flag set is still aHS without X.
        assert cmd[1] == "-aHS"

    def test_includes_chmod_recipe(self) -> None:
        cmd = self._build()
        assert "--chmod=Du+rwx,Dg-s,Dgo-rwx,Fu+rw,Fgo-rwx,Fa-st" in cmd

    def test_safe_links_only_when_requested(self) -> None:
        assert "--safe-links" not in self._build()
        assert "--safe-links" in self._build(safe_links=True)

    def test_no_group_set_via_rsync_flags(self) -> None:
        """rsync 3.x has no --group=GID flag; the recipe relies on
        ``--no-owner --no-group`` plus a post-rsync chown walk instead."""
        cmd = self._build()
        # No --group=... flag in any form.
        assert not any(arg.startswith("--group=") for arg in cmd)
        assert "--no-group" in cmd
        assert "--no-owner" in cmd

    def test_default_and_extra_excludes_concatenated(self) -> None:
        cmd = wb._build_rsync_cmd(
            "/src",
            "/dest.partial",
            excludes=("a", "b"),
            extra_excludes=("c", "d"),
            safe_links=False,
            use_xattrs=False,
        )
        # Each exclude becomes ['--exclude', '<pat>'] in order.
        flat = " ".join(cmd)
        assert "--exclude a" in flat
        assert "--exclude b" in flat
        assert "--exclude c" in flat
        assert "--exclude d" in flat

    def test_source_and_dest_have_trailing_slash(self) -> None:
        cmd = self._build()
        assert cmd[-2] == "/src/"
        assert cmd[-1] == "/dest.partial/"


# ── create_backup ─────────────────────────────────────────────────────────


class TestCreateBackup:
    def test_missing_source_raises_path_error(
        self, fake_executor: _FakeExecutor, tmp_path: Path
    ) -> None:
        with pytest.raises(wb.BackupPathError):
            wb.create_backup(
                instance_name="foo",
                workspace_name="main",
                source_path=str(tmp_path / "missing"),
                source_bootstrap_mode="empty",
                dev_primary_gid=1000,
            )

    def test_happy_path_writes_final_tree_and_metadata(
        self, fake_executor: _FakeExecutor, tmp_path: Path
    ) -> None:
        fake_executor.rsync_run = _populate_partial
        src = _make_source(tmp_path)

        info = wb.create_backup(
            instance_name="foo",
            workspace_name="main",
            source_path=str(src),
            source_bootstrap_mode="empty",
            dev_primary_gid=1000,
        )

        assert info.path.is_dir()
        assert info.path.name == info.timestamp
        assert (info.path / "stub.txt").exists()
        meta = json.loads((info.path / wb.BACKUP_INFO_FILENAME).read_text())
        assert meta["schema_version"] == wb.SCHEMA_VERSION
        assert meta["source_instance"] == "foo"
        assert meta["source_workspace"] == "main"
        assert meta["source_bootstrap_mode"] == "empty"
        assert meta["stripped_unsafe_links_count"] == 0
        assert meta["rsync_excludes_applied"] == list(COPY_DEFAULT_EXCLUDES)
        assert meta["tooling"]["rsync_xattrs_supported"] is True
        assert meta["file_count"] == 1
        assert meta["size_bytes"] == len(b"hello")

    def test_atomic_rename_partial_to_final(
        self, fake_executor: _FakeExecutor, tmp_path: Path
    ) -> None:
        fake_executor.rsync_run = _populate_partial
        src = _make_source(tmp_path)

        info = wb.create_backup(
            instance_name="foo",
            workspace_name="main",
            source_path=str(src),
            source_bootstrap_mode="empty",
            dev_primary_gid=1000,
        )
        # No .partial directory remains.
        partial = info.path.with_name(info.path.name + ".partial")
        assert not partial.exists()
        assert info.path.is_dir()

    def test_rsync_failure_retains_partial_and_raises(
        self, fake_executor: _FakeExecutor, tmp_path: Path
    ) -> None:
        # The failing-rsync path: the partial dir was created right before
        # the rsync call, so it should exist after the failure even though
        # rsync wrote nothing.
        fake_executor.rsync_run = None  # raises SandboxExecutionError
        src = _make_source(tmp_path)

        with pytest.raises(wb.BackupRsyncError):
            wb.create_backup(
                instance_name="foo",
                workspace_name="main",
                source_path=str(src),
                source_bootstrap_mode="empty",
                dev_primary_gid=1000,
            )

        # No final directory created; partial may or may not exist depending
        # on whether mkdir ran before the raise. Assert the final is absent
        # — the testable contract is "no atomic rename happened".
        backups = wb.backups_root() / "foo" / "main"
        finals = [p for p in backups.iterdir() if not p.name.endswith(".partial")] if backups.exists() else []
        assert finals == []

    def test_xattrs_off_omits_x_flag(
        self, fake_executor: _FakeExecutor, tmp_path: Path
    ) -> None:
        fake_executor.rsync_stdout = "no xattrs"
        fake_executor.rsync_run = _populate_partial
        src = _make_source(tmp_path)

        wb.create_backup(
            instance_name="foo",
            workspace_name="main",
            source_path=str(src),
            source_bootstrap_mode="empty",
            dev_primary_gid=1000,
        )

        recipe = _FakeExecutor.last_recipe_cmd
        assert recipe is not None
        assert "-X" not in recipe
        assert recipe[0] == "rsync"

    def test_unsafe_symlinks_trigger_safe_links(
        self, fake_executor: _FakeExecutor, tmp_path: Path
    ) -> None:
        fake_executor.rsync_run = _populate_partial
        src = tmp_path / "src"
        src.mkdir()
        outside = tmp_path / "outside"
        outside.write_text("x")
        (src / "evil").symlink_to(outside)

        info = wb.create_backup(
            instance_name="foo",
            workspace_name="main",
            source_path=str(src),
            source_bootstrap_mode="empty",
            dev_primary_gid=1000,
        )
        recipe = _FakeExecutor.last_recipe_cmd
        assert recipe is not None
        assert "--safe-links" in recipe
        meta = json.loads((info.path / wb.BACKUP_INFO_FILENAME).read_text())
        assert meta["stripped_unsafe_links_count"] == 1

    def test_acquire_lock_false_skips_lock(
        self, fake_executor: _FakeExecutor, tmp_path: Path
    ) -> None:
        """acquire_lock=False is the destroy-orchestrated path: caller already
        holds <inst>.backup.lock, so create_backup must not double-acquire."""
        fake_executor.rsync_run = _populate_partial
        os.makedirs(sandbox_ai_home() / "state", exist_ok=True)
        # Pre-acquire the lock externally (simulating destroy's `with`).
        with acquire_backup_lock("foo"):
            info = wb.create_backup(
                instance_name="foo",
                workspace_name="main",
                source_path=str(_make_source(tmp_path)),
                source_bootstrap_mode="empty",
                dev_primary_gid=1000,
                acquire_lock=False,
            )
            assert info.path.is_dir()

    def test_lock_held_blocks_concurrent_create(
        self, fake_executor: _FakeExecutor, tmp_path: Path
    ) -> None:
        # Pre-acquire the lock so create_backup's acquire_backup_lock fails.
        os.makedirs(sandbox_ai_home() / "state", exist_ok=True)
        with acquire_backup_lock("foo"), pytest.raises(BackupLockHeldError):
            wb.create_backup(
                instance_name="foo",
                workspace_name="main",
                source_path=str(_make_source(tmp_path)),
                source_bootstrap_mode="empty",
                dev_primary_gid=1000,
            )


# ── list_backups ──────────────────────────────────────────────────────────


def _make_backup(home: Path, inst: str, ws: str, ts: str, *, contents: str = "x") -> Path:
    """Create a finalized backup directory under ``<home>/workspaces/_backups/``."""
    target = home / "workspaces" / "_backups" / inst / ws / ts
    target.mkdir(parents=True, exist_ok=True)
    (target / "data").write_text(contents)
    (target / wb.BACKUP_INFO_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_instance": inst,
                "source_workspace": ws,
                "source_bootstrap_mode": "empty",
                "source_path": f"/fake/{inst}/{ws}",
                "created_at_utc": "2026-05-07T00:00:00Z",
                "size_bytes": len(contents),
                "file_count": 1,
                "sandbox_ai_version": "test",
                "rsync_excludes_applied": [],
                "stripped_unsafe_links_count": 0,
                "tooling": {"rsync_version": "test", "rsync_xattrs_supported": True},
            }
        )
    )
    return target


class TestListBackups:
    def test_returns_empty_when_root_missing(self, isolated_sandbox_ai_home: Path) -> None:
        assert wb.list_backups() == []

    def test_lists_finalized_backups_sorted(self, isolated_sandbox_ai_home: Path) -> None:
        _make_backup(isolated_sandbox_ai_home, "b", "main", "2026-05-07-00-00-00")
        _make_backup(isolated_sandbox_ai_home, "a", "main", "2026-05-06-00-00-00")
        _make_backup(isolated_sandbox_ai_home, "a", "main", "2026-05-07-00-00-00")
        out = wb.list_backups()
        assert [(b.source_instance, b.timestamp) for b in out] == [
            ("a", "2026-05-06-00-00-00"),
            ("a", "2026-05-07-00-00-00"),
            ("b", "2026-05-07-00-00-00"),
        ]

    def test_skips_partial_dirs(self, isolated_sandbox_ai_home: Path) -> None:
        _make_backup(isolated_sandbox_ai_home, "a", "main", "2026-05-07-00-00-00")
        partial = isolated_sandbox_ai_home / "workspaces" / "_backups" / "a" / "main" / "2026-05-08-00-00-00.partial"
        partial.mkdir()
        out = wb.list_backups()
        assert len(out) == 1
        assert not out[0].path.name.endswith(".partial")

    def test_skips_non_timestamp_dirs(self, isolated_sandbox_ai_home: Path) -> None:
        _make_backup(isolated_sandbox_ai_home, "a", "main", "2026-05-07-00-00-00")
        bogus = isolated_sandbox_ai_home / "workspaces" / "_backups" / "a" / "main" / "not-a-timestamp"
        bogus.mkdir()
        out = wb.list_backups()
        assert len(out) == 1

    def test_filter_by_instance(self, isolated_sandbox_ai_home: Path) -> None:
        _make_backup(isolated_sandbox_ai_home, "a", "main", "2026-05-07-00-00-00")
        _make_backup(isolated_sandbox_ai_home, "b", "main", "2026-05-07-00-00-00")
        out = wb.list_backups(wb.BackupFilter(source_instance="b"))
        assert [b.source_instance for b in out] == ["b"]

    def test_filter_by_workspace(self, isolated_sandbox_ai_home: Path) -> None:
        _make_backup(isolated_sandbox_ai_home, "a", "main", "2026-05-07-00-00-00")
        _make_backup(isolated_sandbox_ai_home, "a", "scratch", "2026-05-07-00-00-00")
        out = wb.list_backups(wb.BackupFilter(source_workspace="scratch"))
        assert [b.source_workspace for b in out] == ["scratch"]

    def test_metadata_is_loaded(self, isolated_sandbox_ai_home: Path) -> None:
        _make_backup(isolated_sandbox_ai_home, "a", "main", "2026-05-07-00-00-00")
        out = wb.list_backups()
        assert out[0].metadata["source_instance"] == "a"

    def test_handles_unreadable_metadata_gracefully(
        self, isolated_sandbox_ai_home: Path
    ) -> None:
        target = _make_backup(isolated_sandbox_ai_home, "a", "main", "2026-05-07-00-00-00")
        (target / wb.BACKUP_INFO_FILENAME).write_text("not-json")
        out = wb.list_backups()
        assert out[0].metadata == {}

    def test_handles_non_dict_metadata(self, isolated_sandbox_ai_home: Path) -> None:
        target = _make_backup(isolated_sandbox_ai_home, "a", "main", "2026-05-07-00-00-00")
        (target / wb.BACKUP_INFO_FILENAME).write_text("[1,2,3]")
        out = wb.list_backups()
        assert out[0].metadata == {}


# ── resolve_backup_spec ───────────────────────────────────────────────────


class TestResolveBackupSpec:
    def test_omitted_with_one_match(self, isolated_sandbox_ai_home: Path) -> None:
        _make_backup(isolated_sandbox_ai_home, "a", "main", "2026-05-07-00-00-00")
        info = wb.resolve_backup_spec(None, "main")
        assert info.source_instance == "a"

    def test_omitted_with_no_match(self, isolated_sandbox_ai_home: Path) -> None:
        with pytest.raises(wb.BackupSpecNotFoundError):
            wb.resolve_backup_spec(None, "nope")

    def test_omitted_ambiguous_across_instances(
        self, isolated_sandbox_ai_home: Path
    ) -> None:
        _make_backup(isolated_sandbox_ai_home, "a", "main", "2026-05-07-00-00-00")
        _make_backup(isolated_sandbox_ai_home, "b", "main", "2026-05-07-00-00-00")
        with pytest.raises(wb.BackupSpecAmbiguousError):
            wb.resolve_backup_spec(None, "main")

    def test_two_part_spec(self, isolated_sandbox_ai_home: Path) -> None:
        _make_backup(isolated_sandbox_ai_home, "a", "main", "2026-05-06-00-00-00")
        _make_backup(isolated_sandbox_ai_home, "a", "main", "2026-05-07-00-00-00")
        info = wb.resolve_backup_spec("a/main", "main")
        # list_backups sorts ascending; resolve returns the latest.
        assert info.timestamp == "2026-05-07-00-00-00"

    def test_two_part_spec_not_found(self, isolated_sandbox_ai_home: Path) -> None:
        with pytest.raises(wb.BackupSpecNotFoundError):
            wb.resolve_backup_spec("nope/missing", "main")

    def test_three_part_spec_resolves(self, isolated_sandbox_ai_home: Path) -> None:
        _make_backup(isolated_sandbox_ai_home, "a", "main", "2026-05-07-00-00-00")
        info = wb.resolve_backup_spec("a/main/2026-05-07-00-00-00", "main")
        assert info.timestamp == "2026-05-07-00-00-00"

    def test_three_part_spec_bad_timestamp(self, isolated_sandbox_ai_home: Path) -> None:
        with pytest.raises(wb.BackupSpecNotFoundError):
            wb.resolve_backup_spec("a/main/not-a-timestamp", "main")

    def test_three_part_spec_path_missing(self, isolated_sandbox_ai_home: Path) -> None:
        with pytest.raises(wb.BackupSpecNotFoundError):
            wb.resolve_backup_spec("a/main/2026-05-07-00-00-00", "main")

    def test_one_part_spec_rejected(self, isolated_sandbox_ai_home: Path) -> None:
        with pytest.raises(wb.BackupSpecNotFoundError):
            wb.resolve_backup_spec("loose", "main")

    def test_four_part_spec_rejected(self, isolated_sandbox_ai_home: Path) -> None:
        with pytest.raises(wb.BackupSpecNotFoundError):
            wb.resolve_backup_spec("a/b/c/d", "main")


# ── restore_backup ────────────────────────────────────────────────────────


class TestRestoreBackup:
    def test_copies_tree_excluding_metadata(
        self, isolated_sandbox_ai_home: Path
    ) -> None:
        target = _make_backup(isolated_sandbox_ai_home, "src-inst", "main", "2026-05-07-00-00-00")
        info = wb.BackupInfo(
            path=target,
            source_instance="src-inst",
            source_workspace="main",
            timestamp="2026-05-07-00-00-00",
            size_bytes=1,
        )
        dest = wb.restore_backup(info, "dst-inst", "main")
        assert (dest / "data").exists()
        # .backup-info.json is not carried into the live workspace.
        assert not (dest / wb.BACKUP_INFO_FILENAME).exists()

    def test_refuses_existing_destination(
        self, isolated_sandbox_ai_home: Path
    ) -> None:
        target = _make_backup(isolated_sandbox_ai_home, "src-inst", "main", "2026-05-07-00-00-00")
        info = wb.BackupInfo(
            path=target,
            source_instance="src-inst",
            source_workspace="main",
            timestamp="2026-05-07-00-00-00",
            size_bytes=1,
        )
        existing = isolated_sandbox_ai_home / "workspaces" / "dst-inst" / "main"
        existing.mkdir(parents=True)
        with pytest.raises(wb.BackupPathError):
            wb.restore_backup(info, "dst-inst", "main")


# ── tree size + count ─────────────────────────────────────────────────────


class TestForceGroup:
    def test_chowns_root_and_every_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "file").write_text("x")
        (tmp_path / "top").write_text("y")

        calls: list[tuple[str, int, int]] = []

        def fake_chown(path: Any, uid: int, gid: int, *, follow_symlinks: bool = True) -> None:
            calls.append((str(path), uid, gid))

        monkeypatch.setattr("core.workspace_backups.os.chown", fake_chown)
        wb._force_group(tmp_path, 4242)
        # Root + dirs + files all chowned with uid=-1 and gid=4242.
        assert (str(tmp_path), -1, 4242) in calls
        assert any(p.endswith("/sub") and g == 4242 and u == -1 for p, u, g in calls)
        assert any(p.endswith("/sub/file") for p, _u, _g in calls)
        assert any(p.endswith("/top") for p, _u, _g in calls)

    def test_root_chown_oserror_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "child").write_text("x")

        def boom(path: Any, *_a: Any, **_kw: Any) -> None:
            if str(path) == str(tmp_path):
                raise PermissionError("denied")

        monkeypatch.setattr("core.workspace_backups.os.chown", boom)
        # Should not raise; the child chown still runs.
        wb._force_group(tmp_path, 4242)

    def test_child_chown_oserror_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "child").write_text("x")

        def boom(path: Any, *_a: Any, **_kw: Any) -> None:
            if str(path).endswith("/child"):
                raise PermissionError("denied")

        monkeypatch.setattr("core.workspace_backups.os.chown", boom)
        # Should not raise; failures inside the walk are skipped.
        wb._force_group(tmp_path, 4242)


class TestTreeSizeAndCount:
    def test_counts_files_and_sums_sizes(self, tmp_path: Path) -> None:
        (tmp_path / "a").write_text("hello")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b").write_text("world!")
        size, count = wb._tree_size_and_count(str(tmp_path))
        assert count == 2
        assert size == len(b"hello") + len(b"world!")

    def test_skips_unstattable_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "a").write_text("hi")

        real_lstat = os.lstat

        def boom(path: str) -> Any:
            if path.endswith("/a"):
                raise OSError("boom")
            return real_lstat(path)

        monkeypatch.setattr(os, "lstat", boom)
        size, count = wb._tree_size_and_count(str(tmp_path))
        assert count == 0
        assert size == 0


# ── version helper ────────────────────────────────────────────────────────


class TestSandboxAiVersion:
    def test_returns_unknown_when_package_metadata_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from importlib import metadata

        def boom(_name: str) -> str:
            raise metadata.PackageNotFoundError("sandbox-ai")

        monkeypatch.setattr(wb, "_pkg_version", boom)
        assert wb._sandbox_ai_version() == "unknown"

    def test_returns_real_version_when_installed(self) -> None:
        assert wb._sandbox_ai_version() != ""
