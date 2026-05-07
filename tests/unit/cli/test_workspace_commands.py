"""Tests for ``sandbox workspace add | remove | rename``.

Mirrors task 9.5's validation matrix from instance-workspace-model.
``_warm_check`` is mocked everywhere so tests do not exercise the daemon;
``create_backup`` is mocked to avoid running rsync.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

if TYPE_CHECKING:
    from tests.unit.conftest import HostConfigFactory  # noqa: F401


_TOML_TEMPLATE = """\
[instance]
name = "{name}"
host_uid = "1000"
warmup_prompt = ""

{workspaces}

[core]
shm_size = "2gb"
pids_limit = 400
base_image = "cgr.dev/chainguard/wolfi-base:latest"
base_distro_family = "wolfi"
git_user = ""
git_email = ""

[admin]
shm_size = "2gb"
pids_limit = 400
base_image = "debian:trixie-slim"
base_distro_family = "debian"

[runtimes]
python = true
typescript = true
rust = true
go = false

[runtimes.node]
version = "20.12.2"
nvm_version = "0.39.7"

[components]
mcp_firecrawl = false
mcp_puppeteer = false

[components.db_postgres]
enabled = false
expose_host_ports = [5432]

[components.ingress]
web_ports = [3000, 8080]

[proxy.whitelist]
domains = [".github.com"]
"""


def _user_home() -> Path:
    return Path(os.environ["SANDBOX_AI_HOME"])


def _seed_registry(home: Path) -> None:
    state_dir = home / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    reg = state_dir / "instances.json"
    if not reg.exists():
        reg.write_text("{}")


def _register(inst: str, *, workspaces: list[tuple[str, str, str | None]]) -> Path:
    """Register ``inst`` and write a minimal instance dir + sandbox.toml.

    ``workspaces`` is a list of ``(name, bootstrap_mode, source_or_None)``.
    Each workspace path is created at ``<home>/workspaces/<inst>/<name>``.
    """
    home = _user_home()
    state_dir = home / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    reg = state_dir / "instances.json"
    existing: dict[str, dict[str, str]] = json.loads(reg.read_text()) if reg.exists() else {}
    instance_dir = home / "instances" / inst
    instance_dir.mkdir(parents=True, exist_ok=True)
    existing[inst] = {"instance_dir": str(instance_dir), "created_at": "2026-01-01T00:00:00Z"}
    reg.write_text(json.dumps(existing))

    sections: list[str] = []
    for name, mode, source in workspaces:
        ws_path = home / "workspaces" / inst / name
        ws_path.mkdir(parents=True, exist_ok=True)
        sections.append(f"[workspaces.{name}]")
        sections.append(f'bootstrap_mode = "{mode}"')
        if source is not None:
            sections.append(f'source = "{source}"')
        sections.append(f'path = "{ws_path}"')
        sections.append("")
    rendered = "\n".join(sections).rstrip() + "\n"

    (instance_dir / "sandbox.toml").write_text(_TOML_TEMPLATE.format(name=inst, workspaces=rendered))
    (instance_dir / ".initialized").write_text("")
    return instance_dir


@pytest.fixture(autouse=True)
def _stop_warm_check() -> object:
    """Default: instance is stopped (warm check returns False)."""
    with patch("cli.main._warm_check", return_value=False):
        yield


@pytest.fixture(autouse=True)
def _resolve_host_config_default() -> object:
    from core.host_config import HostConfig

    cfg = HostConfig.model_validate(
        {"host": {"docker_unprivileged_user": "sandbox", "machinectl_authentication": "sudo"}}
    )
    with patch("cli.main.HostConfig.from_toml", return_value=cfg):
        yield


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


# ── workspace add ────────────────────────────────────────────────────────


class TestWorkspaceAdd:
    def test_no_flags_rejected(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        result = runner.invoke(app, ["workspace", "add", "foo"])
        assert result.exit_code == 1
        assert "at least one --copy or --empty" in result.output

    def test_unknown_instance_rejected(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        result = runner.invoke(app, ["workspace", "add", "nope", "--empty", "extra"])
        assert result.exit_code == 1
        assert "no sandbox instance" in result.output.lower()

    def test_copy_without_equals_rejected(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        result = runner.invoke(app, ["workspace", "add", "foo", "--copy", "/some/path"])
        assert result.exit_code != 0
        assert "NAME=PATH" in result.output

    def test_duplicate_name_across_flags_rejected(self, runner: CliRunner, tmp_path: Path) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        src = tmp_path / "src"
        src.mkdir()
        result = runner.invoke(
            app,
            ["workspace", "add", "foo", "--copy", f"dup={src}", "--empty", "dup"],
        )
        assert result.exit_code != 0
        assert "more than once" in result.output

    def test_collision_with_existing_rejected(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        result = runner.invoke(app, ["workspace", "add", "foo", "--empty", "main"])
        assert result.exit_code == 1
        assert "already exists" in result.output

    def test_running_instance_rejected(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        with patch("cli.main._warm_check", return_value=True):
            result = runner.invoke(app, ["workspace", "add", "foo", "--empty", "extra"])
        assert result.exit_code == 1
        assert "must be stopped" in result.output

    def test_backup_lock_held_rejected(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        with patch("cli.main.is_backup_lock_held", return_value=True):
            result = runner.invoke(app, ["workspace", "add", "foo", "--empty", "extra"])
        assert result.exit_code == 1
        assert "backup in progress" in result.output.lower()

    def test_source_must_exist(self, runner: CliRunner, tmp_path: Path) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        result = runner.invoke(
            app,
            ["workspace", "add", "foo", "--copy", f"x={tmp_path / 'missing'}"],
        )
        assert result.exit_code != 0
        # Rich's error panel inserts box-drawing chars and newlines mid-message;
        # strip them before substring matching.
        flat = "".join(c for c in result.output if c.isalnum() or c in " '/.").lower()
        flat = " ".join(flat.split())
        assert "does not exist" in flat

    def test_happy_path_empty_workspace(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        instance_dir = _register("foo", workspaces=[("main", "empty", None)])
        result = runner.invoke(app, ["workspace", "add", "foo", "--empty", "extra"])
        assert result.exit_code == 0, result.output
        assert "Added workspace" in result.output

        # Verify sandbox.toml has the new entry.
        toml_text = (instance_dir / "sandbox.toml").read_text()
        assert "[workspaces.extra]" in toml_text
        assert "[workspaces.main]" in toml_text
        # Workspace dir created.
        assert (_user_home() / "workspaces" / "foo" / "extra").is_dir()

    def test_happy_path_copy_workspace(self, runner: CliRunner, tmp_path: Path) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        src = tmp_path / "src"
        src.mkdir()
        (src / "file.txt").write_text("data")

        with patch("cli.main.copy_workspace") as mock_copy:
            result = runner.invoke(
                app,
                ["workspace", "add", "foo", "--copy", f"backend={src}"],
            )
        assert result.exit_code == 0, result.output
        mock_copy.assert_called_once()

    def test_lock_contention_rejected(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        with patch("cli.main._acquire_state_lock", side_effect=BlockingIOError):
            result = runner.invoke(app, ["workspace", "add", "foo", "--empty", "extra"])
        assert result.exit_code == 1
        assert "already in progress" in result.output


# ── workspace remove ─────────────────────────────────────────────────────


class TestWorkspaceRemove:
    def test_mutex_backup_and_purge(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        result = runner.invoke(app, ["workspace", "remove", "foo", "main", "--backup", "--purge"])
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output

    def test_unknown_workspace_rejected(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        result = runner.invoke(app, ["workspace", "remove", "foo", "nope", "--purge"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_non_tty_without_flags_rejected(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        # CliRunner's invoke runs without a TTY by default.
        with patch("cli.main._stdin_is_tty", return_value=False):
            result = runner.invoke(app, ["workspace", "remove", "foo", "main"])
        assert result.exit_code == 1
        assert "non-interactive" in result.output.lower()

    def test_purge_happy_path(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        instance_dir = _register("foo", workspaces=[("main", "empty", None), ("scratch", "empty", None)])
        scratch = _user_home() / "workspaces" / "foo" / "scratch"
        assert scratch.is_dir()

        result = runner.invoke(app, ["workspace", "remove", "foo", "scratch", "--purge"])
        assert result.exit_code == 0, result.output
        assert not scratch.exists()
        toml_text = (instance_dir / "sandbox.toml").read_text()
        assert "[workspaces.scratch]" not in toml_text
        assert "[workspaces.main]" in toml_text

    def test_backup_happy_path(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        instance_dir = _register("foo", workspaces=[("main", "empty", None), ("scratch", "empty", None)])
        scratch = _user_home() / "workspaces" / "foo" / "scratch"

        with patch("cli.main.create_backup") as mock_backup:
            result = runner.invoke(app, ["workspace", "remove", "foo", "scratch", "--backup"])
        assert result.exit_code == 0, result.output
        mock_backup.assert_called_once()
        assert not scratch.exists()
        toml_text = (instance_dir / "sandbox.toml").read_text()
        assert "[workspaces.scratch]" not in toml_text

    def test_backup_failure_aborts(self, runner: CliRunner) -> None:
        from cli.main import app
        from core.workspace_backups import BackupRsyncError

        _seed_registry(_user_home())
        instance_dir = _register("foo", workspaces=[("main", "empty", None), ("scratch", "empty", None)])
        scratch = _user_home() / "workspaces" / "foo" / "scratch"

        with patch("cli.main.create_backup", side_effect=BackupRsyncError("boom")):
            result = runner.invoke(app, ["workspace", "remove", "foo", "scratch", "--backup"])
        assert result.exit_code == 1
        assert "backup failed" in result.output.lower()
        # Workspace still exists; sandbox.toml not mutated.
        assert scratch.is_dir()
        toml_text = (instance_dir / "sandbox.toml").read_text()
        assert "[workspaces.scratch]" in toml_text

    def test_running_instance_rejected(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        with patch("cli.main._warm_check", return_value=True):
            result = runner.invoke(app, ["workspace", "remove", "foo", "main", "--purge"])
        assert result.exit_code == 1
        assert "must be stopped" in result.output

    def test_last_workspace_emits_warning(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        result = runner.invoke(app, ["workspace", "remove", "foo", "main", "--purge"])
        assert result.exit_code == 0, result.output
        assert "zero workspaces" in result.output

    def test_tty_prompt_yes_runs_backup(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None), ("scratch", "empty", None)])
        with (
            patch("cli.main._stdin_is_tty", return_value=True),
            patch("cli.main.typer.prompt", return_value="Y"),
            patch("cli.main.create_backup") as mock_backup,
        ):
            result = runner.invoke(app, ["workspace", "remove", "foo", "scratch"])
        assert result.exit_code == 0, result.output
        mock_backup.assert_called_once()

    def test_lock_contention_rejected(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None), ("scratch", "empty", None)])
        with patch("cli.main._acquire_state_lock", side_effect=BlockingIOError):
            result = runner.invoke(app, ["workspace", "remove", "foo", "scratch", "--purge"])
        assert result.exit_code == 1
        assert "already in progress" in result.output

    def test_tty_prompt_no_runs_purge(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None), ("scratch", "empty", None)])
        with (
            patch("cli.main._stdin_is_tty", return_value=True),
            patch("cli.main.typer.prompt", return_value="n"),
            patch("cli.main.create_backup") as mock_backup,
        ):
            result = runner.invoke(app, ["workspace", "remove", "foo", "scratch"])
        assert result.exit_code == 0, result.output
        mock_backup.assert_not_called()


# ── workspace rename ─────────────────────────────────────────────────────


class TestWorkspaceRename:
    def test_same_name_rejected(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        result = runner.invoke(app, ["workspace", "rename", "foo", "main", "main"])
        assert result.exit_code == 1
        assert "identical" in result.output

    def test_invalid_new_name_rejected(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        result = runner.invoke(app, ["workspace", "rename", "foo", "main", "_bad"])
        assert result.exit_code != 0

    def test_old_not_found_rejected(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        result = runner.invoke(app, ["workspace", "rename", "foo", "missing", "newname"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_new_collides_with_existing(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None), ("scratch", "empty", None)])
        result = runner.invoke(app, ["workspace", "rename", "foo", "main", "scratch"])
        assert result.exit_code == 1
        assert "already exists" in result.output

    def test_running_instance_rejected(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        with patch("cli.main._warm_check", return_value=True):
            result = runner.invoke(app, ["workspace", "rename", "foo", "main", "primary"])
        assert result.exit_code == 1
        assert "must be stopped" in result.output

    def test_happy_path(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        instance_dir = _register("foo", workspaces=[("main", "empty", None)])
        old_path = _user_home() / "workspaces" / "foo" / "main"
        new_path = _user_home() / "workspaces" / "foo" / "primary"
        assert old_path.is_dir()
        assert not new_path.exists()

        result = runner.invoke(app, ["workspace", "rename", "foo", "main", "primary"])
        assert result.exit_code == 0, result.output
        assert not old_path.exists()
        assert new_path.is_dir()
        toml_text = (instance_dir / "sandbox.toml").read_text()
        assert "[workspaces.primary]" in toml_text
        assert "[workspaces.main]" not in toml_text
        assert str(new_path) in toml_text

    def test_exdev_explicit_error(self, runner: CliRunner) -> None:
        import errno

        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        with patch("os.rename", side_effect=OSError(errno.EXDEV, "cross-device")):
            result = runner.invoke(app, ["workspace", "rename", "foo", "main", "primary"])
        assert result.exit_code == 1
        assert "cross-filesystem" in result.output.lower() or "cross-fs" in result.output.lower()

    def test_lock_contention_rejected(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        with patch("cli.main._acquire_state_lock", side_effect=BlockingIOError):
            result = runner.invoke(app, ["workspace", "rename", "foo", "main", "primary"])
        assert result.exit_code == 1
        assert "already in progress" in result.output

    def test_other_oserror_propagates(self, runner: CliRunner) -> None:
        import errno

        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        with patch("os.rename", side_effect=OSError(errno.EACCES, "perms")):
            result = runner.invoke(app, ["workspace", "rename", "foo", "main", "primary"])
        assert result.exit_code != 0


# ── replace_workspaces_section helper ─────────────────────────────────────


class TestReplaceWorkspacesSection:
    """Direct coverage for the toml mutation helper (in core.scaffold)."""

    def _make_toml(self, ws_block: str) -> str:
        return _TOML_TEMPLATE.format(name="foo", workspaces=ws_block)

    def test_replaces_single_workspace_block(self) -> None:
        from core.scaffold import WorkspaceSpec, replace_workspaces_section

        toml = self._make_toml('[workspaces.main]\nbootstrap_mode = "empty"\npath = "/p"\n')
        result = replace_workspaces_section(
            toml,
            [
                WorkspaceSpec(name="main", bootstrap_mode="empty", source=None, path="/p"),
                WorkspaceSpec(name="other", bootstrap_mode="empty", source=None, path="/q"),
            ],
        )
        assert "[workspaces.main]" in result
        assert "[workspaces.other]" in result
        assert "[core]" in result  # next section preserved

    def test_appends_when_no_block_present(self) -> None:
        from core.scaffold import WorkspaceSpec, replace_workspaces_section

        toml = "[instance]\nname = 'foo'\n"
        result = replace_workspaces_section(
            toml,
            [WorkspaceSpec(name="main", bootstrap_mode="empty", source=None, path="/p")],
        )
        assert "[workspaces.main]" in result

    def test_handles_block_at_end_of_file(self) -> None:
        from core.scaffold import WorkspaceSpec, replace_workspaces_section

        toml = '[instance]\nname="foo"\n\n[workspaces.main]\nbootstrap_mode = "empty"\npath = "/p"\n'
        result = replace_workspaces_section(
            toml,
            [WorkspaceSpec(name="renamed", bootstrap_mode="empty", source=None, path="/q")],
        )
        assert "[workspaces.renamed]" in result
        assert "[workspaces.main]" not in result


# ── workspace restore ────────────────────────────────────────────────────


def _make_backup(
    inst: str, ws_name: str, ts: str, *, source_instance: str | None = None
) -> Path:
    """Create a finalized backup tree under ``<home>/workspaces/_backups/`` and
    return the backup directory."""
    src_inst = source_instance or inst
    target = _user_home() / "workspaces" / "_backups" / src_inst / ws_name / ts
    target.mkdir(parents=True, exist_ok=True)
    (target / "data.txt").write_text("payload")
    (target / ".backup-info.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_instance": src_inst,
                "source_workspace": ws_name,
                "source_bootstrap_mode": "empty",
                "source_path": f"/orig/{src_inst}/{ws_name}",
                "created_at_utc": "2026-05-07T00:00:00Z",
                "size_bytes": 7,
                "file_count": 1,
                "sandbox_ai_version": "test",
                "rsync_excludes_applied": [],
                "stripped_unsafe_links_count": 0,
                "tooling": {"rsync_version": "test", "rsync_xattrs_supported": True},
            }
        )
    )
    return target


class TestWorkspaceRestore:
    def test_unknown_dest_instance(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        result = runner.invoke(app, ["workspace", "restore", "missing", "main"])
        assert result.exit_code == 1
        assert "no sandbox instance" in result.output.lower()

    def test_dest_workspace_collision_rejected(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        result = runner.invoke(app, ["workspace", "restore", "foo", "main"])
        assert result.exit_code == 1
        assert "already exists" in result.output

    def test_no_backup_found_rejected(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        result = runner.invoke(app, ["workspace", "restore", "foo", "scratch"])
        assert result.exit_code == 1
        assert "no backups" in result.output.lower()

    def test_omitted_spec_picks_latest_unique_match(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        instance_dir = _register("foo", workspaces=[("main", "empty", None)])
        _make_backup("foo", "scratch", "2026-05-07-00-00-00")

        result = runner.invoke(app, ["workspace", "restore", "foo", "scratch"])
        assert result.exit_code == 0, result.output
        restored = _user_home() / "workspaces" / "foo" / "scratch"
        assert (restored / "data.txt").exists()
        toml_text = (instance_dir / "sandbox.toml").read_text()
        assert "[workspaces.scratch]" in toml_text
        assert 'bootstrap_mode = "copy"' in toml_text

    def test_omitted_spec_ambiguous_refuses(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        _make_backup("a", "scratch", "2026-05-07-00-00-00", source_instance="a")
        _make_backup("b", "scratch", "2026-05-07-00-00-00", source_instance="b")

        result = runner.invoke(app, ["workspace", "restore", "foo", "scratch"])
        assert result.exit_code == 1
        assert "multiple source instances" in result.output.lower()

    def test_fully_qualified_spec(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        _make_backup("oldfoo", "scratch", "2026-05-07-00-00-00", source_instance="oldfoo")

        result = runner.invoke(
            app,
            ["workspace", "restore", "foo", "scratch", "--from", "oldfoo/scratch/2026-05-07-00-00-00"],
        )
        assert result.exit_code == 0, result.output

    def test_invalid_dest_workspace_name(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        result = runner.invoke(app, ["workspace", "restore", "foo", "_bad"])
        assert result.exit_code != 0

    def test_running_instance_rejected(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        _make_backup("foo", "scratch", "2026-05-07-00-00-00")
        with patch("cli.main._warm_check", return_value=True):
            result = runner.invoke(app, ["workspace", "restore", "foo", "scratch"])
        assert result.exit_code == 1
        assert "must be stopped" in result.output

    def test_lock_contention_rejected(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        _make_backup("foo", "scratch", "2026-05-07-00-00-00")
        with patch("cli.main._acquire_state_lock", side_effect=BlockingIOError):
            result = runner.invoke(app, ["workspace", "restore", "foo", "scratch"])
        assert result.exit_code == 1
        assert "already in progress" in result.output

    def test_backup_lock_held_rejected(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        _make_backup("foo", "scratch", "2026-05-07-00-00-00")
        with patch("cli.main.is_backup_lock_held", return_value=True):
            result = runner.invoke(app, ["workspace", "restore", "foo", "scratch"])
        assert result.exit_code == 1
        assert "backup in progress" in result.output.lower()


# ── workspace list ───────────────────────────────────────────────────────


class TestWorkspaceList:
    def test_unknown_instance(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        result = runner.invoke(app, ["workspace", "list", "missing"])
        assert result.exit_code == 1

    def test_default_lists_live_and_backups(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        _make_backup("foo", "main", "2026-05-07-00-00-00")
        result = runner.invoke(app, ["workspace", "list", "foo"])
        assert result.exit_code == 0, result.output
        flat = " ".join(result.output.split())
        assert "Live workspaces (foo)" in flat
        assert "main" in flat
        assert "Backups" in flat
        assert "foo/main/2026-05-07-00-00-00" in flat

    def test_no_backups_flag_suppresses_section(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        _make_backup("foo", "main", "2026-05-07-00-00-00")
        result = runner.invoke(app, ["workspace", "list", "foo", "--no-backups"])
        assert result.exit_code == 0
        assert "Backups" not in result.output

    def test_json_output_shape(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        _make_backup("foo", "main", "2026-05-07-00-00-00")
        result = runner.invoke(app, ["workspace", "list", "foo", "--json"])
        assert result.exit_code == 0
        # Rich's print_json may pretty-print; parse the entire output.
        payload = json.loads(result.output)
        assert "workspaces" in payload
        assert payload["workspaces"][0]["name"] == "main"
        assert payload["workspaces"][0]["bootstrap_mode"] == "empty"
        assert payload["backups"][0]["id"] == "foo/main/2026-05-07-00-00-00"
        assert payload["backups"][0]["source_workspace"] == "main"


class TestFormatAge:
    def test_seconds(self) -> None:
        import datetime as _dt

        from cli.main import _format_age

        ts = (_dt.datetime.now(tz=_dt.UTC) - _dt.timedelta(seconds=30)).strftime("%Y-%m-%d-%H-%M-%S")
        assert _format_age(ts).endswith("s ago")

    def test_minutes(self) -> None:
        import datetime as _dt

        from cli.main import _format_age

        ts = (_dt.datetime.now(tz=_dt.UTC) - _dt.timedelta(minutes=30)).strftime("%Y-%m-%d-%H-%M-%S")
        assert _format_age(ts).endswith("m ago")

    def test_hours(self) -> None:
        import datetime as _dt

        from cli.main import _format_age

        ts = (_dt.datetime.now(tz=_dt.UTC) - _dt.timedelta(hours=5)).strftime("%Y-%m-%d-%H-%M-%S")
        assert _format_age(ts).endswith("h ago")

    def test_days(self) -> None:
        import datetime as _dt

        from cli.main import _format_age

        ts = (_dt.datetime.now(tz=_dt.UTC) - _dt.timedelta(days=10)).strftime("%Y-%m-%d-%H-%M-%S")
        assert _format_age(ts).endswith("d ago")

    def test_invalid_timestamp(self) -> None:
        from cli.main import _format_age

        assert _format_age("not-a-timestamp") == "unknown"
