"""Shared fixtures and helpers for `tests/unit/cli/`.

Centralizes: the per-user-home / registry / instance-dir helpers, the
autouse `_warm_check` and `HostConfig.from_toml` patches, and the
`CliRunner` fixture. Every CLI unit test file under this directory picks
these up automatically — no per-file copies.

The instance ``sandbox.toml`` is built via the production
``core.scaffold.write_sandbox_toml`` rather than a test-local template.
This keeps "what a valid `sandbox.toml` looks like" anchored to the
production code path: schema additions can't drift past the test suite,
and an `IMAGE_REGISTRY` rotation reaches every fixture-built instance
automatically.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from core.scaffold import WorkspaceSpec, write_sandbox_toml
from typer.testing import CliRunner


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
    The on-disk ``sandbox.toml`` is rendered through the production
    ``write_sandbox_toml`` so test fixtures track schema reality.
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

    specs: list[WorkspaceSpec] = []
    for name, mode, source in workspaces:
        ws_path = home / "workspaces" / inst / name
        ws_path.mkdir(parents=True, exist_ok=True)
        specs.append(WorkspaceSpec(name=name, bootstrap_mode=mode, source=source, path=str(ws_path)))

    write_sandbox_toml(str(instance_dir), inst, specs)
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
