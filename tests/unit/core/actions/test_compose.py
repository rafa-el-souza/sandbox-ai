"""Tests for ``ComposeUpAction`` (typed instance intent; one dispatch seam).

The Action carries only the typed instance name. Both ``.execute()`` and the
dry-run ``.render_command()`` derive their command from
``core.dispatch.build_invocation`` (the SAME seam ``core.dispatch.invoke``
consumes) so the live and dry-run paths cannot drift.
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from core.actions.compose import ComposeUpAction
from core.actions.context import ActionContext
from core.compose import compose_project_name
from core.executor import Executor
from core.host_config import DockerExecutionMode, MachinectlAuth

if TYPE_CHECKING:
    from core.host_config import HostConfig


class _FakeHostSettings:
    docker_unprivileged_user = "claude-sandbox"
    docker_execution_mode = DockerExecutionMode.SEPARATE_USER

    def __init__(self, auth: MachinectlAuth) -> None:
        self.machinectl_authentication = auth


class _FakeHostConfig:
    def __init__(self, auth: MachinectlAuth = MachinectlAuth.SUDO) -> None:
        self.host = _FakeHostSettings(auth)


def _hc(auth: MachinectlAuth = MachinectlAuth.SUDO) -> HostConfig:
    return cast("HostConfig", _FakeHostConfig(auth))


def _seed_instance(home: Path, inst: str) -> None:
    inst_dir = home / "instances" / inst
    (inst_dir / "docker").mkdir(parents=True, exist_ok=True)
    (inst_dir / "docker" / "compose.yml").write_text("services: {}\n")
    (inst_dir / ".sandbox.env").write_text("")
    (inst_dir / "sandbox.toml").write_text(
        f'[instance]\nname = "{inst}"\nhost_uid = "1000"\n\n'
        '[workspaces.main]\nbootstrap_mode = "empty"\n'
        f'path = "{home}/workspaces/{inst}/main"\n'
        "\n[components.db_postgres]\nenabled = false\n"
    )
    state = home / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "instances.json").write_text(
        _json.dumps({inst: {"instance_dir": str(inst_dir), "created_at": "2026-01-01T00:00:00Z"}})
    )


def _ctx(
    host_config: HostConfig | None,
    auth: MachinectlAuth = MachinectlAuth.SUDO,
    executor: Executor | None = None,
) -> ActionContext:
    return ActionContext(
        host_user="claude-sandbox",
        auth=auth,
        executor=executor or Executor(),
        instance_dir=Path("/inst"),
        host_config=host_config,
    )


class _FakeExecutor:
    def __init__(self) -> None:
        self.invocations: list[tuple[list[str], dict[str, object]]] = []

    def run(self, cmd: list[str], **kw: object) -> object:
        self.invocations.append((cmd, dict(kw)))
        return None


def test_action_carries_typed_instance_name_only() -> None:
    action = ComposeUpAction(instance_name="demo")
    assert action.instance_name == "demo"
    assert action.describe() == "demo"
    assert not hasattr(action, "inner_command")


def test_render_command_formally_overrides_base_default() -> None:
    """``ComposeUpAction.render_command`` is a real override of the base
    ``Action.render_command`` default (it does NOT inherit the
    describe()-delegating default), and ``describe()`` stays the pure
    identity returning the instance name."""
    from core.actions.base import Action

    assert ComposeUpAction.render_command is not Action.render_command
    action = ComposeUpAction(instance_name="demo")
    assert action.describe() == "demo"


def test_render_command_emits_q6_wire_form(isolated_sandbox_ai_home: Path) -> None:
    _seed_instance(isolated_sandbox_ai_home, "demo")
    proj = compose_project_name("demo")
    inst_dir = isolated_sandbox_ai_home / "instances" / "demo"
    rendered = ComposeUpAction(instance_name="demo").render_command(_hc())
    # SUDO separate-user crossings now ride the privileged byte-pipe (C-009 D2),
    # NOT machinectl shell — the rendered command carries the sudo_pipe_cmd
    # prefix and no machinectl token.
    assert "machinectl" not in rendered
    assert "sudo systemd-run -q --pipe --uid=claude-sandbox /bin/bash -c" in rendered
    assert (
        f"/usr/local/libexec/sandbox-ai/dispatch compose-up demo "
        f"--project {proj} "
        f"--env-file {inst_dir / '.sandbox.env'} "
        f"--compose-file {inst_dir / 'docker' / 'compose.yml'}"
    ) in rendered


def test_execute_routes_through_dispatch_invoke(
    isolated_sandbox_ai_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    _seed_instance(isolated_sandbox_ai_home, "demo")
    captured: dict[str, object] = {}

    def fake_run(
        self: object, cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
    hc = _hc()
    ComposeUpAction(instance_name="demo").execute(_ctx(hc))
    # The crossed argv is byte-identical to what build_invocation produces and
    # to what the dry-run render_command would show (one seam).
    from core.dispatch import Op, build_invocation

    assert captured["cmd"] == build_invocation(Op.COMPOSE_UP, ["demo"], hc)


def test_execute_preserves_raise_on_failure(
    isolated_sandbox_ai_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.exceptions import SandboxExecutionError

    _seed_instance(isolated_sandbox_ai_home, "demo")

    def fake_run(self: object, cmd: list[str], **kwargs: object) -> object:
        raise SandboxExecutionError("[FATAL] compose up failed")

    monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
    with pytest.raises(SandboxExecutionError):
        ComposeUpAction(instance_name="demo").execute(_ctx(_hc()))


def test_execute_requires_host_config_on_context() -> None:
    with pytest.raises(ValueError, match=r"requires ActionContext\.host_config"):
        ComposeUpAction(instance_name="demo").execute(_ctx(None))


def test_polkit_auth_drops_sudo_prefix_in_render(isolated_sandbox_ai_home: Path) -> None:
    _seed_instance(isolated_sandbox_ai_home, "demo")
    rendered = ComposeUpAction(instance_name="demo").render_command(_hc(MachinectlAuth.POLKIT))
    assert rendered.startswith("machinectl shell claude-sandbox@.host /bin/bash -c")


def test_render_and_execute_share_one_seam(
    isolated_sandbox_ai_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Structural byte-equivalence: dry-run render == live crossed argv."""
    import shlex
    import subprocess

    _seed_instance(isolated_sandbox_ai_home, "demo")
    captured: dict[str, object] = {}

    def fake_run(
        self: object, cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
    hc = _hc()
    action = ComposeUpAction(instance_name="demo")
    action.execute(_ctx(hc))
    assert shlex.join(cast("list[str]", captured["cmd"])) == action.render_command(hc)
