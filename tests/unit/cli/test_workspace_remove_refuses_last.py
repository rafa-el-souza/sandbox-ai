"""Tests for `sandbox workspace remove`'s refuse-last-workspace contract.

The refusal MUST precede the `--backup`/`--purge` branching: no rsync,
no rmtree, no `mutate_workspaces` call, no backup directory created.
The error message MUST surface both supported replacement paths
(add-then-remove for swap workflows; destroy for instance removal).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

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


def _register_single_workspace(inst: str, ws_name: str = "main") -> Path:
    home = _user_home()
    state_dir = home / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    reg = state_dir / "instances.json"
    existing: dict[str, dict[str, str]] = json.loads(reg.read_text()) if reg.exists() else {}
    instance_dir = home / "instances" / inst
    instance_dir.mkdir(parents=True, exist_ok=True)
    existing[inst] = {"instance_dir": str(instance_dir), "created_at": "2026-01-01T00:00:00Z"}
    reg.write_text(json.dumps(existing))

    ws_path = home / "workspaces" / inst / ws_name
    ws_path.mkdir(parents=True, exist_ok=True)
    sections = (
        f"[workspaces.{ws_name}]\n"
        'bootstrap_mode = "empty"\n'
        f'path = "{ws_path}"\n'
    )
    (instance_dir / "sandbox.toml").write_text(_TOML_TEMPLATE.format(name=inst, workspaces=sections))
    (instance_dir / ".initialized").write_text("")
    return instance_dir


@pytest.fixture(autouse=True)
def _stop_warm_check() -> object:
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


_REFUSAL_FRAGMENT = "Cannot remove the last workspace from"
_REPLACEMENT_HINT = "Add a replacement workspace first"
_DESTROY_HINT = "use 'sandbox destroy"


class TestRefuseLastWorkspace:
    def test_purge_on_only_workspace_exits_with_documented_message(
        self, runner: CliRunner
    ) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        instance_dir = _register_single_workspace("foo", "main")
        ws_path = _user_home() / "workspaces" / "foo" / "main"
        toml_before = (instance_dir / "sandbox.toml").read_text()

        # Mocks must register zero calls — the refusal precedes them.
        with (
            patch("cli.main.create_backup") as mock_backup,
            patch("cli.main.mutate_workspaces") as mock_mutate,
            patch("cli.main.shutil.rmtree") as mock_rmtree,
        ):
            result = runner.invoke(app, ["workspace", "remove", "foo", "main", "--purge"])

        assert result.exit_code == 1
        # Documented message contents — the three load-bearing fragments.
        # Flatten Rich's soft-wrap whitespace so substring checks survive a narrow capture width.
        flat = " ".join(result.output.split())
        assert _REFUSAL_FRAGMENT in flat
        assert "'foo'" in flat
        assert _REPLACEMENT_HINT in flat
        assert _DESTROY_HINT in flat
        # No state mutation observable.
        mock_backup.assert_not_called()
        mock_mutate.assert_not_called()
        mock_rmtree.assert_not_called()
        assert ws_path.is_dir()
        assert (instance_dir / "sandbox.toml").read_text() == toml_before

    def test_backup_on_only_workspace_refuses_without_creating_backup(
        self, runner: CliRunner
    ) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        instance_dir = _register_single_workspace("foo", "main")
        ws_path = _user_home() / "workspaces" / "foo" / "main"
        toml_before = (instance_dir / "sandbox.toml").read_text()
        backups_root = _user_home() / "workspaces" / "_backups"

        with (
            patch("cli.main.create_backup") as mock_backup,
            patch("cli.main.mutate_workspaces") as mock_mutate,
            patch("cli.main.shutil.rmtree") as mock_rmtree,
        ):
            result = runner.invoke(app, ["workspace", "remove", "foo", "main", "--backup"])

        assert result.exit_code == 1
        flat = " ".join(result.output.split())
        assert _REFUSAL_FRAGMENT in flat
        # The refusal precedes the backup path — no backup invoked.
        mock_backup.assert_not_called()
        mock_mutate.assert_not_called()
        mock_rmtree.assert_not_called()
        # No backup directory exists for this instance/workspace.
        assert not backups_root.exists() or not (backups_root / "foo").exists()
        # Workspace tree still on disk.
        assert ws_path.is_dir()
        assert (instance_dir / "sandbox.toml").read_text() == toml_before
