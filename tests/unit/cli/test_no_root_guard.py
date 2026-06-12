# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""No-root guard on the runtime commands (host-config delta, C-004 §3.2 / D8).

The 11 runtime commands (`init`, `start`, `stop`, `status`, `attach`, `destroy`,
and the five `workspace` subcommands) refuse to run as root (`euid == 0`) so
`sandbox_ai_home()` (`~/.sandbox-ai`) never resolves to `/root/.sandbox-ai`.
`setup` and `doctor` are exempt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from cli.main import app

if TYPE_CHECKING:
    from typer.testing import CliRunner

# argv for each guarded runtime command. The guard is the FIRST line of each
# body, so a minimal argv (enough to satisfy typer's required arguments) reaches
# it before any other validation or state read.
_GUARDED_COMMANDS: list[tuple[str, list[str]]] = [
    ("init", ["init", "inst"]),
    ("start", ["start", "inst"]),
    ("stop", ["stop", "inst"]),
    ("status", ["status", "inst"]),
    ("attach", ["attach", "inst"]),
    ("destroy", ["destroy", "inst"]),
    ("workspace_add", ["workspace", "add", "inst", "--empty", "ws"]),
    ("workspace_remove", ["workspace", "remove", "inst", "ws"]),
    ("workspace_rename", ["workspace", "rename", "inst", "old", "new"]),
    ("workspace_restore", ["workspace", "restore", "inst", "ws"]),
    ("workspace_list", ["workspace", "list", "inst"]),
]


@pytest.mark.parametrize(
    ("name", "argv"),
    _GUARDED_COMMANDS,
    ids=[name for name, _ in _GUARDED_COMMANDS],
)
def test_runtime_command_refuses_root(name: str, argv: list[str], runner: CliRunner) -> None:
    """Each runtime command refuses euid==0 with the operator-account message,
    before any state is read or mutated."""
    with (
        patch("cli.main.os.geteuid", return_value=0),
        # If the guard ever let execution proceed, these would be the first
        # state read/creation for the runtime commands — assert never reached.
        patch("cli.main._require_per_user_state_initialized") as state_mock,
        patch("cli.main.ensure_per_user_state") as ensure_mock,
    ):
        result = runner.invoke(app, argv)
    assert result.exit_code == 1, f"{name} should refuse root (exit 1)"
    assert "not root" in result.output
    assert "/root/.sandbox-ai" in result.output
    # The guard short-circuits before any state read/creation.
    state_mock.assert_not_called()
    ensure_mock.assert_not_called()


@pytest.mark.parametrize(
    ("name", "argv"),
    _GUARDED_COMMANDS,
    ids=[name for name, _ in _GUARDED_COMMANDS],
)
def test_runtime_command_passes_guard_when_non_root(
    name: str, argv: list[str], runner: CliRunner
) -> None:
    """When euid != 0 the guard is a no-op: execution proceeds past it (here it
    reaches the next step rather than emitting the root-refusal message)."""
    with patch("cli.main.os.geteuid", return_value=1000):
        result = runner.invoke(app, argv)
    # The command may still fail for other reasons (missing instance, etc.) —
    # the point is it did NOT hit the root-refusal path.
    assert "not root" not in result.output, f"{name} wrongly refused a non-root euid"


def test_setup_exempt_from_no_root_guard(runner: CliRunner) -> None:
    """`setup` is exempt: as root it does NOT emit the runtime no-root message
    (it has its own separate-user root *requirement* — proceeds past the
    entry root-check)."""
    with (
        patch("cli.main.os.geteuid", return_value=0),
        patch("cli.main._build_setup_context_with_operator", side_effect=KeyboardInterrupt),
    ):
        result = runner.invoke(app, ["setup"])
    assert "not root" not in result.output


def test_doctor_exempt_from_no_root_guard(runner: CliRunner) -> None:
    """`doctor` is exempt: invoked as root it does not emit the no-root message."""
    with (
        patch("cli.main.os.geteuid", return_value=0),
        patch("cli.main.run_checks", return_value=[]),
        patch("cli.main.render_results"),
        patch("cli.main.detect_distro"),
        patch("cli.main.build_check_registry"),
    ):
        result = runner.invoke(app, ["doctor", "--user", "sandbox"])
    assert "not root" not in result.output
