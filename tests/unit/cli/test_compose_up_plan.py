# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for `_compose_up_cmd_plan` / `_phase_compose_up` — the compose-up seam.

Post-runtime-dispatcher (C-001 / Q6): the plan yields a typed
:class:`ComposeUpAction` carrying ONLY the instance name; the ``--project`` /
``--env-file`` / ``--compose-file`` operands + env-prefix + verb are resolved
internally by ``core.dispatch.build_invocation`` (the single
command-construction seam ``core.dispatch.invoke`` also consumes).

Covers:

- The plan returns a typed ``ComposeUpAction`` (no ``inner_command`` string).
- The Q6 wire form the dry-run renders (no comma-joined filenames).
- Live/dry-run parity: ``_phase_compose_up``'s crossed argv is byte-identical
  to the Action's ``.render_command(host_config)``.
"""

from __future__ import annotations

import json as _json
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

from cli.main import _compose_up_cmd_plan, _phase_compose_up
from core.compose import compose_project_name
from core.host_config import DockerExecutionMode

if TYPE_CHECKING:
    import pytest
    from core.host_config import HostConfig


class _FakeHostSettings:
    docker_unprivileged_user = "sandbox"
    docker_execution_mode = DockerExecutionMode.SEPARATE_USER


class _FakeHostConfig:
    def __init__(self) -> None:
        self.host = _FakeHostSettings()


def _hc() -> HostConfig:
    return cast("HostConfig", _FakeHostConfig())


def _seed_instance(home: Path, inst: str, *, pg: bool = False) -> None:
    inst_dir = home / "instances" / inst
    (inst_dir / "docker" / "extras").mkdir(parents=True, exist_ok=True)
    (inst_dir / "docker" / "compose.yml").write_text("services: {}\n")
    (inst_dir / ".sandbox.env").write_text("")
    (inst_dir / "sandbox.toml").write_text(
        f'[instance]\nname = "{inst}"\nhost_uid = "1000"\n\n'
        '[workspaces.main]\nbootstrap_mode = "empty"\n'
        f'path = "{home}/workspaces/{inst}/main"\n'
        f"\n[components.db_postgres]\nenabled = {str(pg).lower()}\n"
    )
    state = home / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "instances.json").write_text(
        _json.dumps({inst: {"instance_dir": str(inst_dir), "created_at": "2026-01-01T00:00:00Z"}})
    )


class TestComposeUpCmdPlan:
    def test_returns_typed_compose_up_action(self) -> None:
        from core.actions import ComposeUpAction

        action = _compose_up_cmd_plan("t")
        assert isinstance(action, ComposeUpAction)
        assert action.instance_name == "t"
        assert not hasattr(action, "inner_command")

    def test_render_command_contains_base_compose_file_flag(
        self, isolated_sandbox_ai_home: Path
    ) -> None:
        _seed_instance(isolated_sandbox_ai_home, "demo", pg=True)
        inst_dir = isolated_sandbox_ai_home / "instances" / "demo"
        rendered = _compose_up_cmd_plan("demo").render_command(_hc())
        assert f"--compose-file {inst_dir / 'docker' / 'compose.yml'}" in rendered

    def test_render_command_contains_extras_compose_file_flag(
        self, isolated_sandbox_ai_home: Path
    ) -> None:
        _seed_instance(isolated_sandbox_ai_home, "demo", pg=True)
        inst_dir = isolated_sandbox_ai_home / "instances" / "demo"
        rendered = _compose_up_cmd_plan("demo").render_command(_hc())
        assert f"--compose-file {inst_dir / 'docker' / 'extras' / 'db-postgres.yml'}" in rendered

    def test_render_command_contains_env_file_flag(
        self, isolated_sandbox_ai_home: Path
    ) -> None:
        _seed_instance(isolated_sandbox_ai_home, "demo")
        inst_dir = isolated_sandbox_ai_home / "instances" / "demo"
        rendered = _compose_up_cmd_plan("demo").render_command(_hc())
        assert f"--env-file {inst_dir / '.sandbox.env'}" in rendered

    def test_render_command_carries_dispatch_op_and_project(
        self, isolated_sandbox_ai_home: Path
    ) -> None:
        # The crossed inner string is the Q6 wire form
        # ``dispatch compose-up <inst> --project <P> …``; the ``up -d --build
        # --wait`` verb is op-hardcoded INSIDE the dispatcher binary and is
        # NOT present in the crossed command (that is the whole point of Q6).
        _seed_instance(isolated_sandbox_ai_home, "alpha")
        rendered = _compose_up_cmd_plan("alpha").render_command(_hc())
        assert "dispatch compose-up alpha" in rendered
        assert f"--project {compose_project_name('alpha')}" in rendered
        assert "up -d --build --wait" not in rendered

    def test_render_command_does_not_comma_join_filenames(
        self, isolated_sandbox_ai_home: Path
    ) -> None:
        """Regression guard: the helper-cp shadow used `", ".join(files)`."""
        _seed_instance(isolated_sandbox_ai_home, "demo", pg=True)
        rendered = _compose_up_cmd_plan("demo").render_command(_hc())
        assert ", " not in rendered


class TestPhaseComposeUpParity:
    def test_phase_compose_up_crosses_byte_identical_to_render(
        self, isolated_sandbox_ai_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_phase_compose_up`'s crossed argv MUST equal the Action's render.

        Patches ``core.dispatch.Executor.run`` (the single execution path
        ComposeUpAction routes through via ``invoke``) to capture the argv;
        it is asserted byte-identical to the dry-run ``.render_command``.
        """
        import subprocess

        _seed_instance(isolated_sandbox_ai_home, "demo")
        hc = _hc()
        expected = _compose_up_cmd_plan("demo").render_command(hc)
        captured: dict[str, object] = {}

        def fake_run(
            self: object, cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        inst_dir = str(isolated_sandbox_ai_home / "instances" / "demo")
        with patch("cli.main._resolve_full_host_config", return_value=hc):
            _phase_compose_up("demo", inst_dir, hc)

        assert shlex.join(cast("list[str]", captured["cmd"])) == expected
