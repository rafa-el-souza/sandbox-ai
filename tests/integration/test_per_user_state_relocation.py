"""Subprocess-level coverage for the per-user state relocation change.

Each test invokes the CLI via ``uv run sandbox …`` with ``SANDBOX_AI_USER_HOME``
redirected to ``tmp_path``. The user's real ``~/.sandbox-ai/`` is never touched.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env(home: Path) -> dict[str, str]:
    """Build a subprocess env that redirects per-user home to ``home``."""
    env = os.environ.copy()
    env["SANDBOX_AI_USER_HOME"] = str(home)
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
    """Lifecycle commands exit 1 with the canonical error when the tree is absent."""
    home = tmp_path / ".sandbox-ai"
    args = [command]
    if command == "destroy":
        args.append("--force")
    result = _run_sandbox(args, home=home)
    assert result.returncode == 1
    assert "per-user state not initialized" in result.stdout.lower() + result.stderr.lower()
    assert str(home) in result.stdout + result.stderr
    assert "sandbox init" in (result.stdout + result.stderr).lower()
    # Crucial: tree was not created by the failed command.
    assert not home.exists() or not (home / "state" / "instances.json").exists()


def test_non_tty_init_without_config_fails(tmp_path: Path) -> None:
    """Non-TTY `sandbox init` with no canonical host config exits with guidance."""
    home = tmp_path / ".sandbox-ai"
    result = _run_sandbox(["init"], home=home, stdin=subprocess.DEVNULL)
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    # Rich wraps lines; collapse newlines before substring matching.
    flat = combined.replace("\n", "")
    assert "non-interactive" in flat.lower()
    assert str(home / "config" / "sandbox-ai.toml") in flat


def test_init_creates_state_dir_with_mode_0700(tmp_path: Path) -> None:
    """Init creates `<home>/state/` with mode 0700 on a clean host.

    Uses a pre-seeded host config to bypass the interactive prompt without
    pre-creating any directories. ensure_per_user_tree then creates the
    full tree with mode 0700.
    """
    home = tmp_path / ".sandbox-ai"
    # Pre-create a parent dir for the host config and use mkdtemp-style:
    # we want to verify that ensure_per_user_tree creates `home` itself with 0700.
    # So we cannot pre-create `home`. Instead, pre-seed the config file by writing
    # to a sibling location, then move it after ensure_per_user_tree has run is
    # not possible from outside. Workaround: pre-create only `<home>/config/` to
    # hold the seed and assert mode 0700 on `<home>/state/` (created fresh).
    (home / "config").mkdir(parents=True)
    (home / "config" / "sandbox-ai.toml").write_text(
        '[host]\ndocker_unprivileged_user = "preseed"\nmachinectl_authentication = "sudo"\n'
    )
    _run_sandbox(["init"], home=home, stdin=subprocess.DEVNULL)
    assert (home / "state").is_dir()
    # Freshly created by ensure_per_user_tree → mode 0700
    assert stat.S_IMODE((home / "state").stat().st_mode) == 0o700


def test_resolved_home_appears_in_doctor_output(tmp_path: Path) -> None:
    """`sandbox doctor` displays the resolved per-user home (env var visibility)."""
    home = tmp_path / ".sandbox-ai"
    result = _run_sandbox(["doctor", "--user", "root"], home=home)
    combined = result.stdout + result.stderr
    assert "Per-user home:" in combined
    assert str(home) in combined
