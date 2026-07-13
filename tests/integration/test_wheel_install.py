# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""Subprocess-level test: build the wheel, install it into an isolated env,
and verify the packaged ``templates`` module ships and is importable.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_SRC = REPO_ROOT / "src" / "templates"


def _source_manifest() -> dict[str, str]:
    """Map every non-`.py` data file under `src/templates/` to its sha256.

    Derived from the source tree so the wheel assertion has a single source of
    truth — a packaging narrowing that drops, truncates, or alters any data
    file fails the byte-identity compare below, with no hand-maintained list to
    drift out of sync. (Empty files like the vendored `go.sum` are handled
    naturally: an empty file has a well-defined digest.)
    """
    return {
        p.relative_to(TEMPLATES_SRC).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in TEMPLATES_SRC.rglob("*")
        if p.is_file() and p.suffix != ".py" and "__pycache__" not in p.parts
    }


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

    expected = _source_manifest()
    assert expected, "no data files discovered under src/templates"

    # In the installed wheel, hash every expected data file and emit a
    # `{relpath: sha256}` manifest. `files('templates')` resolves to the
    # installed package, so comparing this against the source manifest proves
    # the wheel ships every data file byte-for-byte (catches drops, truncation,
    # and content drift). Missing files are reported as the sentinel "MISSING".
    probe = (
        "import hashlib, json, sys; "
        "from importlib.resources import files; "
        "root = files('templates'); "
        "expected = json.loads(sys.argv[1]); "
        "out = {}; "
        "\nfor rel in expected:\n"
        "    f = root.joinpath(*rel.split('/'))\n"
        "    out[rel] = hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else 'MISSING'\n"
        "print(json.dumps(out))"
    )
    result = subprocess.run(
        [str(venv_python), "-c", probe, json.dumps(expected)],
        check=True,
        capture_output=True,
        text=True,
    )
    shipped = json.loads(result.stdout)
    assert shipped == expected, (
        "wheel template manifest differs from source: "
        f"{ {k: shipped.get(k) for k in expected if shipped.get(k) != expected[k]} }"
    )

    sandbox_bin = venv_dir / "bin" / "sandbox"
    assert sandbox_bin.exists(), f"sandbox CLI missing at {sandbox_bin}"
