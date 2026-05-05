"""Subprocess-level test: build the wheel, install it into an isolated env,
and verify the packaged ``templates`` module ships and is importable.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_wheel_ships_templates_package(tmp_path: Path) -> None:
    """Built wheel installs into a fresh venv and exposes the `templates` package."""
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()

    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist_dir), str(REPO_ROOT)],
        check=True,
        capture_output=True,
        text=True,
    )

    wheels = list(dist_dir.glob("*.whl"))
    assert len(wheels) == 1, f"Expected one wheel, found: {wheels}"
    wheel = wheels[0]

    venv_dir = tmp_path / "venv"
    subprocess.run(
        ["uv", "venv", str(venv_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    venv_python = venv_dir / "bin" / "python"
    assert venv_python.exists(), f"venv python missing at {venv_python}"

    install_env = {**os.environ, "VIRTUAL_ENV": str(venv_dir)}
    subprocess.run(
        ["uv", "pip", "install", "--python", str(venv_python), str(wheel)],
        check=True,
        capture_output=True,
        text=True,
        env=install_env,
    )

    probe = (
        "import templates; "
        "from importlib.resources import files; "
        "compose = files('templates').joinpath('docker', 'compose.yml').read_text(); "
        "assert compose.strip(), 'compose.yml is empty in wheel'; "
        "print('OK')"
    )
    result = subprocess.run(
        [str(venv_python), "-c", probe],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "OK" in result.stdout

    sandbox_bin = venv_dir / "bin" / "sandbox"
    assert sandbox_bin.exists(), f"sandbox CLI missing at {sandbox_bin}"
