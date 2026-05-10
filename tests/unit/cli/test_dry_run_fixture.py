"""Byte-equivalence fixture gate for `sandbox start --dry-run` output.

Section 5.1-5.3 of the ``refactor-plan-tuples-to-actions`` change.

This is the load-bearing structural gate that pins the dry-run command
preview output to a checked-in fixture for both ``machinectl_authentication``
modes (``sudo`` / ``polkit``). Any unintended divergence in an Action's
``.describe()`` rendering surfaces here as a byte-diff against the fixture.

Determinism strategy:

* All host-resolution boundaries (subuid/subgid resolvers,
  ``workspace_bridge_gid``, ``machinectl_cmd``) are monkeypatched to
  fixed return values matching the fixture's capture-time topology.
* ``USER`` is pinned to ``"dev"`` so the ACL plan's ``dev_user``
  resolution is stable.
* The per-test ``tmp_path`` is normalized to the literal token
  ``<TMP>`` in the captured output before comparison; the fixture
  stores paths with the same token. This keeps the fixture portable
  across hosts and CI workspaces.
* Rich's ANSI colouring is disabled (``CliRunner(mix_stderr=False)``
  on Typer captures plain text only when colour is off; we set
  ``terminal=False`` via ``runner.invoke(..., color=False)`` and post-
  process to be safe).

Regenerating the fixture:

    SANDBOX_AI_REGEN_DRYRUN_FIXTURE=1 \\
        uv run pytest tests/unit/cli/test_dry_run_fixture.py

When the env var is set, the test writes the captured output to the
fixture path and passes; CI runs without the var and asserts byte-
equivalence.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest
    from typer.testing import CliRunner


FIXTURE_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "dryrun"
FIXTURE_SUDO = FIXTURE_DIR / "start_sudo.txt"
FIXTURE_POLKIT = FIXTURE_DIR / "start_polkit.txt"

# ANSI escape stripping (Rich emits CSI sequences when force_terminal is on).
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _normalize(output: str, tmp_root: Path) -> str:
    """Replace per-run tmp_path prefix with a stable token + strip ANSI.

    Two-step replacement:
      1. Resolve ``tmp_root`` (drop any ``/private`` prefix on macOS) and
         replace it with ``<TMP>``.
      2. Replace any residual absolute pytest tmp prefix
         (``/tmp/pytest-of-…``) defensively.
    """
    text = _strip_ansi(output)
    text = text.replace(str(tmp_root), "<TMP>")
    # The dry-run preview also emits ancestor-traverse entries that walk
    # ABOVE ``tmp_root`` (e.g. ``/tmp/pytest-of-dev`` and ``/tmp``). These
    # vary by host (uid embedded in the user name); collapse them all to
    # a single ``<PYTEST_ROOT>`` token so the fixture is portable.
    text = re.sub(r"/tmp/pytest-of-[^/\s]+/pytest-\d+", "<TMP>", text)
    text = re.sub(r"/tmp/pytest-of-[^/\s]+", "<PYTEST_ROOT>", text)
    return text


def _create_tooling_plane(home: Path) -> None:
    """Materialize the ``.docker`` + ``.config`` tooling-plane templates."""
    docker_dir = home / ".docker"
    (docker_dir / "core").mkdir(parents=True, exist_ok=True)
    (docker_dir / "admin").mkdir(parents=True, exist_ok=True)
    (docker_dir / "extras").mkdir(parents=True, exist_ok=True)
    (docker_dir / "coredns").mkdir(parents=True, exist_ok=True)
    (docker_dir / "compose.yml").write_text("# compose for {{ instance_name }}\nversion: '3'\n")
    (docker_dir / "core" / "entrypoint.sh").write_text("#!/bin/bash\n")
    (docker_dir / "core" / "Dockerfile.core.wolfi").write_text("FROM {{ core_base_image }}\n")
    (docker_dir / "admin" / "entrypoint.sh").write_text("#!/bin/bash\n")
    (docker_dir / "admin" / "Dockerfile.admin.debian").write_text("FROM {{ admin_base_image }}\n")
    (docker_dir / "extras" / "db-postgres.yml").write_text("# postgres\n")
    (docker_dir / "coredns" / "Dockerfile.coredns").write_text("FROM busybox\n")

    config_dir = home / ".config"
    (config_dir / "coredns").mkdir(parents=True, exist_ok=True)
    (config_dir / "dnsdist").mkdir(parents=True, exist_ok=True)
    (config_dir / "proxy").mkdir(parents=True, exist_ok=True)
    (config_dir / "admin").mkdir(parents=True, exist_ok=True)
    (config_dir / "core").mkdir(parents=True, exist_ok=True)
    (config_dir / "coredns" / "Corefile").write_text("# Corefile for {{ instance_name }}\n")
    (config_dir / "dnsdist" / "dnsdist.conf").write_text(
        'setLocal("0.0.0.0:53")\nnewServer({address="{{ coredns_dns_ip }}:53"})\n'
    )
    (config_dir / "proxy" / "squid.conf").write_text("# squid for {{ proxy_core_ip }}\n")
    (config_dir / "proxy" / "ERR_SANDBOX_403").write_text("403 Forbidden\n")
    (config_dir / "admin" / ".zshrc").write_text("# zshrc\n")
    (config_dir / "admin" / ".tmux.conf").write_text("# tmux\n")
    (config_dir / "admin" / "gitmux.conf").write_text("# gitmux\n")
    (config_dir / "admin" / "starship.toml").write_text("# starship\n")
    (config_dir / "admin" / ".gitconfig").write_text("# gitconfig\n")
    (config_dir / "core" / ".bashrc").write_text("# bashrc\n")
    (config_dir / "core" / ".npmrc").write_text("# npmrc\n")
    (config_dir / "core" / ".gitconfig").write_text("# gitconfig\n")
    (config_dir / "core" / "CLAUDE.md").write_text("# Claude\n")
    (config_dir / "core" / "sshd_config").write_text("# sshd {{ core_ipc_ip }}\n")


def _write_ipam(user_home: Path, inst: str, slot: int) -> None:
    state_dir = user_home / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "ipam.json").write_text('{"' + inst + '": ' + str(slot) + "}")


def _capture_dry_run(
    runner: CliRunner,
    user_home: Path,
    register: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    auth_mode: str,
) -> str:
    from core.host_config import HostConfig

    inst = "fixture-inst"
    register(
        inst,
        workspaces=[
            ("main", "empty", None),
            ("scratch", "empty", None),
        ],
    )
    _write_ipam(user_home, inst, 0)
    _create_tooling_plane(user_home)

    monkeypatch.setenv("USER", "dev")
    # Replace Rich's module-level console with a fixed-width, non-terminal
    # console so the captured output is independent of the host's COLUMNS,
    # PTY state, and ANSI capability at test time. Width 500 is comfortably
    # wider than any single line the dry-run preview emits.
    import cli.main as _cli_main
    from rich.console import Console as _Console

    monkeypatch.setattr(_cli_main, "console", _Console(width=1000, force_terminal=False, color_system=None))

    # Deterministic boundary stubs — pin every host-side resolver.
    fixed_host = HostConfig.model_validate(
        {"host": {"docker_unprivileged_user": "sandbox", "machinectl_authentication": auth_mode}}
    )

    with (
        patch("cli.main.HostConfig.from_toml", return_value=fixed_host),
        patch("cli.main.workspace_bridge_gid", return_value=200500),
        patch("cli.main.host_id_for_in_container", side_effect=lambda n, _u: 100000 + n),
        patch("cli.main.host_gid_for_in_container", side_effect=lambda n, _u: 200000 + n),
        patch("core.hydration.workspace_bridge_gid", return_value=200500),
        patch("core.hydration.in_container_gid_for_host_gid", return_value=1000),
    ):
        from cli.main import app

        result = runner.invoke(app, ["start", inst, "--dry-run"], color=False)

    assert result.exit_code == 0, f"dry-run exited {result.exit_code}: {result.output}"
    return _normalize(result.output, user_home.parent)


def _assert_or_regen(captured: str, fixture_path: Path) -> None:
    if os.environ.get("SANDBOX_AI_REGEN_DRYRUN_FIXTURE"):
        fixture_path.parent.mkdir(parents=True, exist_ok=True)
        fixture_path.write_text(captured)
        return
    assert fixture_path.exists(), (
        f"Fixture {fixture_path} missing. Regenerate via "
        "SANDBOX_AI_REGEN_DRYRUN_FIXTURE=1 uv run pytest "
        f"{Path(__file__).name}"
    )
    expected = fixture_path.read_text()
    assert captured == expected, (
        f"dry-run output diverged from {fixture_path.name}.\n"
        "If the divergence is intentional, regenerate the fixture via "
        "SANDBOX_AI_REGEN_DRYRUN_FIXTURE=1 and review the diff before "
        "committing."
    )


class TestDryRunFixtureGate:
    """Pin the dry-run output for both auth modes against a checked-in fixture."""

    def test_dry_run_sudo_byte_equivalent(
        self,
        runner: CliRunner,
        user_home: Path,
        register: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured = _capture_dry_run(runner, user_home, register, monkeypatch, "sudo")
        _assert_or_regen(captured, FIXTURE_SUDO)

    def test_dry_run_polkit_byte_equivalent(
        self,
        runner: CliRunner,
        user_home: Path,
        register: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured = _capture_dry_run(runner, user_home, register, monkeypatch, "polkit")
        _assert_or_regen(captured, FIXTURE_POLKIT)
