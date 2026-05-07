"""Shared fixtures and helpers for `tests/unit/cli/`.

Centralizes: the canonical `sandbox.toml` template used to register
fixture instances, the per-user-home / registry / instance-dir helpers,
the autouse `_warm_check` and `HostConfig.from_toml` patches, and the
`CliRunner` fixture. Every CLI unit test file under this directory picks
these up automatically — no per-file copies.

The `_TOML_TEMPLATE` here is a deliberate test fixture, not a production
artifact; the production analogue lives in `core.scaffold` (rendered via
`write_sandbox_toml`). See task 5.5 in the originating change for the
follow-up that replaces this template with the production builder.
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
def _stop_warm_check(request: pytest.FixtureRequest) -> object:
    """Default: instance is stopped (warm check returns False).

    Tests that exercise `_warm_check` directly mark themselves with
    ``@pytest.mark.no_warm_mock`` to bypass this autouse patch.
    """
    if "no_warm_mock" in request.keywords:
        yield
        return
    with patch("cli.main._warm_check", return_value=False):
        yield


@pytest.fixture(autouse=True)
def _resolve_host_config_default(request: pytest.FixtureRequest) -> object:
    """Tests that exercise ``HostConfig.from_toml`` directly mark themselves
    with ``@pytest.mark.no_host_config_mock`` to bypass this autouse patch.
    """
    if "no_host_config_mock" in request.keywords:
        yield
        return
    from core.host_config import HostConfig

    cfg = HostConfig.model_validate(
        {"host": {"docker_unprivileged_user": "sandbox", "machinectl_authentication": "sudo"}}
    )
    with patch("cli.main.HostConfig.from_toml", return_value=cfg):
        yield


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()
