"""Tests for `_compose_up_cmd_plan` — the single source of truth for ``docker compose up``.

Covers:

- Shape of the plan string (compose ``-f`` flag, ``--env-file``, suffix).
- Regression guard against the helper-cp shadow re-emerging (no ``", "``
  joining filenames in place of compose ``-f`` flags).
- Live/dry-run parity: the inner ``bash -c`` argument that
  ``_phase_compose_up`` passes to :class:`Executor` is byte-identical to
  the plan's return value.
"""

from unittest.mock import patch

from cli.main import _compose_up_cmd_plan, _phase_compose_up
from core.hydration import InstanceConfig


def _config_with_postgres() -> InstanceConfig:
    """Fixture: instance with at least one extra compose ``-f`` flag."""
    return InstanceConfig.model_validate(
        {
            "instance": {"name": "t", "host_uid": "1000"},
            "workspaces": {"main": {"bootstrap_mode": "empty", "path": "/x"}},
            "components_db_postgres": {"enabled": True},
        }
    )


class TestComposeUpCmdPlan:
    def test_contains_base_compose_file_flag(self) -> None:
        cmd = _compose_up_cmd_plan("/inst", "myproj", _config_with_postgres())
        assert "-f /inst/docker/compose.yml" in cmd

    def test_contains_extras_compose_file_flag(self) -> None:
        cmd = _compose_up_cmd_plan("/inst", "myproj", _config_with_postgres())
        assert "-f /inst/docker/extras/db-postgres.yml" in cmd

    def test_contains_env_file_flag(self) -> None:
        cmd = _compose_up_cmd_plan("/inst", "myproj", _config_with_postgres())
        assert "--env-file /inst/.sandbox.env" in cmd

    def test_ends_with_up_suffix(self) -> None:
        cmd = _compose_up_cmd_plan("/inst", "myproj", _config_with_postgres())
        assert cmd.endswith("up -d --build --wait")

    def test_does_not_contain_comma_joined_filenames(self) -> None:
        """Regression guard: the helper-cp shadow used `", ".join(files)`.

        If that pattern ever leaks back into the compose-up plan, the
        rendered command would contain `", "` between filenames in place
        of `-f` flags. Assert it never appears.
        """
        cmd = _compose_up_cmd_plan("/inst", "myproj", _config_with_postgres())
        assert ", " not in cmd

    def test_includes_compose_project_name(self) -> None:
        cmd = _compose_up_cmd_plan("/inst", "alpha", _config_with_postgres())
        assert "COMPOSE_PROJECT_NAME=alpha" in cmd


class TestPhaseComposeUpParity:
    def test_phase_compose_up_passes_plan_string_byte_identical(self) -> None:
        """`_phase_compose_up` MUST pass the plan helper's return value verbatim.

        Patches ``Executor`` to capture the argv handed to ``run``; the inner
        ``bash -c`` argument (final element of argv) is asserted equal,
        byte-for-byte, to ``_compose_up_cmd_plan(...)`` for the same inputs.
        """
        config = _config_with_postgres()
        instance_dir = "/inst"
        project_name = "myproj"

        expected = _compose_up_cmd_plan(instance_dir, project_name, config)

        with patch("cli.main.Executor") as MockExec:
            _phase_compose_up(instance_dir, project_name, "sandbox", config)
            argv = MockExec.return_value.run.call_args[0][0]

        assert argv[-1] == expected
