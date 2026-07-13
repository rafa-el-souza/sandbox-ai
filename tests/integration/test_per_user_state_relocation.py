# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""Subprocess-level coverage for the per-user state relocation change.

Each test invokes the CLI via ``uv run sandbox …`` with ``SANDBOX_AI_HOME``
redirected to ``tmp_path``. The user's real ``~/.sandbox-ai/`` is never touched.
"""

from __future__ import annotations

import getpass
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT_FOR_IMPORT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT_FOR_IMPORT / "src"))
try:
    from core.setup_state import read_entry
finally:
    sys.path.pop(0)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env(home: Path) -> dict[str, str]:
    """Build a subprocess env that redirects per-user home to ``home``."""
    env = os.environ.copy()
    env["SANDBOX_AI_HOME"] = str(home)
    return env


def _run_sandbox(
    args: list[str],
    *,
    home: Path,
    stdin: int | bytes | None = subprocess.DEVNULL,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """Invoke ``uv run sandbox <args>`` and return the completed process."""
    return subprocess.run(
        ["uv", "run", "sandbox", *args],
        cwd=str(REPO_ROOT),
        env=_env(home),
        capture_output=True,
        text=True,
        timeout=timeout,
        input=stdin if isinstance(stdin, bytes) else None,
        stdin=stdin if not isinstance(stdin, bytes) else None,
        check=False,
    )


@pytest.mark.parametrize("command", ["start", "stop", "destroy", "status", "attach"])
def test_lifecycle_commands_fail_on_uninitialized_host(tmp_path: Path, command: str) -> None:
    """Lifecycle commands exit 1 with the canonical error when the tree is absent.

    Change 5: every lifecycle command takes an explicit ``<inst>`` argument.
    ``status`` accepts an optional argument so we omit it for the all-instances
    summary path, which still requires the per-user tree.
    """
    home = tmp_path / ".sandbox-ai"
    args: list[str] = [command]
    if command != "status":
        args.append("anything")
    if command == "destroy":
        args.extend(["--force", "--backup-workspaces=none"])
    result = _run_sandbox(args, home=home)
    assert result.returncode == 1
    assert "per-user state not initialized" in result.stdout.lower() + result.stderr.lower()
    assert str(home) in result.stdout + result.stderr
    assert "sandbox init" in (result.stdout + result.stderr).lower()
    # Crucial: tree was not created by the failed command.
    assert not home.exists() or not (home / "state" / "instances.json").exists()


def test_init_without_setup_fails_setup_first(tmp_path: Path) -> None:
    """`sandbox init <inst>` on an unprovisioned host (no marker) fails setup-first.

    Post-C-013 ``init`` is setup-first: with no setup-state marker for the
    operator it exits before touching per-user state, pointing the operator at
    ``sudo sandbox setup``. This exercises the *unprovisioned* path, so skip when
    a marker IS present (the marker lives at a fixed root path this subprocess
    test cannot redirect).
    """
    if read_entry(getpass.getuser()) is not None:
        pytest.skip("skipped: a setup-state marker is present (host provisioned); needs no marker")
    home = tmp_path / ".sandbox-ai"
    result = _run_sandbox(["init", "myinst"], home=home, stdin=subprocess.DEVNULL)
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    # Rich wraps lines; collapse newlines before substring matching.
    flat = combined.replace("\n", "")
    assert "isn't set up yet" in flat
    assert "sudo sandbox setup" in flat
    # The setup-first message names no toml/marker file.
    assert "sandbox-ai.toml" not in flat


def test_init_creates_state_dir_with_mode_0700(tmp_path: Path) -> None:
    """Init creates `<home>/state/` with mode 0700 on a provisioned host.

    ``init`` is setup-first, so this requires a real setup-state marker for the
    operator; skip when none is present (the marker lives at a fixed root path
    this subprocess test cannot create/redirect). RUNs on a provisioned host,
    SKIPs on an unprovisioned one.
    """
    if read_entry(getpass.getuser()) is None:
        pytest.skip("skipped: no setup-state marker (host unprovisioned); init is setup-first")
    home = tmp_path / ".sandbox-ai"
    _run_sandbox(["init", "myinst"], home=home, stdin=subprocess.DEVNULL)
    assert (home / "state").is_dir()
    # Freshly created by ensure_per_user_state → mode 0700
    assert stat.S_IMODE((home / "state").stat().st_mode) == 0o700


def test_resolved_home_appears_in_doctor_output(tmp_path: Path) -> None:
    """`sandbox doctor` displays the resolved per-user home (env var visibility)."""
    home = tmp_path / ".sandbox-ai"
    result = _run_sandbox(["doctor", "--user", "root"], home=home)
    combined = result.stdout + result.stderr
    assert "Per-user home:" in combined
    assert str(home) in combined
