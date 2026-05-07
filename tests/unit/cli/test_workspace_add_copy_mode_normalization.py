"""Tests for `sandbox workspace add --copy`'s 0700 mode-normalization contract.

`--empty` already produces 0700 via `mkdir(..., mode=0o700)`. Pre-fix
`--copy` inherited the source mode through rsync's `-a` flag, so a `0775`
source produced a `0775` workspace, undermining the privacy default.
The fix calls `os.chmod(<ws-root>, 0o700)` after rsync returns.
"""

from __future__ import annotations

import json
import os
import stat
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


def _register(inst: str, *, workspaces: list[tuple[str, str, str | None]]) -> Path:
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


def _mode_bits(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def _simulate_rsync_inheriting_source_mode(src: str, dst: str) -> None:
    """Stand-in for `core.workspace_copy.copy_workspace`.

    The real rsync invocation uses `-a` which preserves source mode. The
    pre-fix bug class is that this same mode lands on the workspace
    directory unchanged. We simulate by chmod'ing the dst to the src mode
    after the fixture mkdir(0o700) — exactly what rsync would do.
    """
    src_mode = _mode_bits(Path(src))
    os.chmod(dst, src_mode)


class TestCopyModeNormalization:
    def test_copy_from_0775_source_produces_0700_workspace(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])
        src = tmp_path / "src"
        src.mkdir(mode=0o775)
        os.chmod(src, 0o775)  # mkdir's mode is umask-filtered; force 0775 explicitly.
        (src / "file.txt").write_text("data")

        with patch("cli.main.copy_workspace", side_effect=_simulate_rsync_inheriting_source_mode):
            result = runner.invoke(
                app,
                ["workspace", "add", "foo", "--copy", f"backend={src}"],
            )

        assert result.exit_code == 0, result.output
        ws_path = _user_home() / "workspaces" / "foo" / "backend"
        assert ws_path.is_dir()
        # Source was 0775, but the workspace MUST be normalized to 0700.
        assert _mode_bits(ws_path) == 0o700, (
            f"copy-mode inherited source mode: {oct(_mode_bits(ws_path))}"
        )

    def test_empty_mode_produces_0700_workspace(self, runner: CliRunner) -> None:
        """Parity / regression guard: --empty has always produced 0700 via mkdir."""
        from cli.main import app

        _seed_registry(_user_home())
        _register("foo", workspaces=[("main", "empty", None)])

        result = runner.invoke(app, ["workspace", "add", "foo", "--empty", "extra"])

        assert result.exit_code == 0, result.output
        ws_path = _user_home() / "workspaces" / "foo" / "extra"
        assert ws_path.is_dir()
        assert _mode_bits(ws_path) == 0o700
