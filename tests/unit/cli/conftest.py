"""Shared fixtures for `tests/unit/cli/`.

Provides:

- ``user_home`` — the per-test SANDBOX_AI_HOME root (set by the
  outer ``isolated_sandbox_ai_home`` autouse fixture in tests/conftest.py).
- ``seed_registry`` (autouse) — creates ``<home>/state/instances.json``
  on every test so commands that load the registry don't have to.
- ``register`` — a callable fixture that registers an instance and
  writes its ``sandbox.toml`` via the production
  ``core.scaffold.write_sandbox_toml`` (single source of truth for
  "what a valid `sandbox.toml` looks like" — schema additions can't
  drift past the test suite, and ``IMAGE_REGISTRY`` rotations reach
  every fixture-built instance automatically).
- ``_stop_warm_check`` (autouse) — patches ``cli.main._warm_check`` to
  return False; opt-out via ``@pytest.mark.no_warm_mock``.
- ``_resolve_host_config_default`` (autouse) — patches
  ``cli.main.HostConfig.from_toml``; opt-out via
  ``@pytest.mark.no_host_config_mock``.
- ``runner`` — a fresh ``CliRunner`` per test.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest
from core.scaffold import WorkspaceSpec, write_sandbox_toml
from typer.testing import CliRunner


@pytest.fixture
def user_home() -> Path:
    """Resolve the per-test SANDBOX_AI_HOME (populated by tests/conftest.py)."""
    return Path(os.environ["SANDBOX_AI_HOME"])


@pytest.fixture(autouse=True)
def seed_registry(request: pytest.FixtureRequest, user_home: Path) -> None:
    """Create an empty ``<home>/state/instances.json`` for every test.

    Idempotent — already-populated registries (e.g., from a prior
    ``register`` call earlier in the same test) are left untouched.

    Tests that exercise the per-user tree's first-run state (e.g.,
    ``sandbox init`` from a clean home) mark themselves with
    ``@pytest.mark.no_seed_registry`` to skip this autouse seed and start
    from a truly empty home.
    """
    if "no_seed_registry" in request.keywords:
        return
    state_dir = user_home / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    reg = state_dir / "instances.json"
    if not reg.exists():
        reg.write_text("{}")


@pytest.fixture
def register(user_home: Path) -> Callable[..., Path]:
    """Return a callable that registers an instance and writes ``sandbox.toml``.

    Signature: ``register(inst, *, workspaces) -> Path`` where ``workspaces``
    is a list of ``(name, bootstrap_mode, source_or_None)`` tuples and the
    return value is the instance directory.

    The on-disk ``sandbox.toml`` is rendered through the production
    ``write_sandbox_toml`` so test fixtures track schema reality.
    """

    def _register(inst: str, *, workspaces: list[tuple[str, str, str | None]]) -> Path:
        state_dir = user_home / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        reg = state_dir / "instances.json"
        existing: dict[str, dict[str, str]] = json.loads(reg.read_text()) if reg.exists() else {}
        instance_dir = user_home / "instances" / inst
        instance_dir.mkdir(parents=True, exist_ok=True)
        existing[inst] = {"instance_dir": str(instance_dir), "created_at": "2026-01-01T00:00:00Z"}
        reg.write_text(json.dumps(existing))

        specs: list[WorkspaceSpec] = []
        for name, mode, source in workspaces:
            ws_path = user_home / "workspaces" / inst / name
            ws_path.mkdir(parents=True, exist_ok=True)
            specs.append(WorkspaceSpec(name=name, bootstrap_mode=mode, source=source, path=str(ws_path)))

        write_sandbox_toml(str(instance_dir), inst, specs)
        (instance_dir / ".initialized").write_text("")
        return instance_dir

    return _register


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


@pytest.fixture(autouse=True)
def stub_marker_write() -> object:
    """Stub the separate-user root-owned marker write.

    ``cli.main._record_separate_user_mode`` calls ``write_mode_root_owned``, which
    ``chown``s the root-owned ``/usr/local/libexec/sandbox-ai/setup-state.json`` —
    not writable in a unit test. Patched to a no-op Mock; yielded so the tests
    that assert the marker was recorded can inspect ``.call_args``.
    """
    with patch("cli.main.write_mode_root_owned") as mock:
        yield mock


@pytest.fixture(autouse=True)
def _pin_console_width() -> object:
    """Pin the CLI's Rich console to a fixed wide, non-terminal width.

    ``cli.main.console`` is a bare ``Console()`` that auto-detects the
    terminal width and wraps output to it. In a narrow environment (an
    80-column CI / pre-commit subprocess, a non-TTY pipe) Rich wraps error
    messages mid-token — e.g. a ``<home>`` path splits across a newline — and
    substring assertions on paths/messages break even though the content is
    correct. Pinning to a fixed wide, plain (no-ANSI) console makes captured
    output independent of the host's ``COLUMNS`` / PTY state. Width 1000 is
    wider than any single line the CLI emits. (Tests that pin their own
    console — e.g. the dry-run fixture gate — override this within the test.)
    """
    from rich.console import Console

    with patch("cli.main.console", Console(width=1000, force_terminal=False, color_system=None)):
        yield


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()
