"""Integration smoke: the offline compile recipe is byte-reproducible.

Marked ``@pytest.mark.integration`` — NOT collected by the default
``make test`` / ``make coverage`` gate (``pytest.testpaths = ["tests/unit"]``).
Runs only via ``make test-integration`` on a real-docker host with the
sandbox-ai privilege boundary configured (a real ``sandbox-ai.toml`` resolvable
via :meth:`core.host_config.HostConfig.from_toml`).

It invokes :func:`core.dispatch.compile_dispatcher` twice into two distinct
output paths against identical source + the same digest-pinned
``golang:1.23-alpine`` image and asserts the two binaries' sha512 match
(design D3 reproducibility; spec "Offline Reproducible Compile Recipe"
scenario "Reproducible build across two invocations").
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from core.dispatch import compile_dispatcher
from core.host_config import HostConfig

pytestmark = pytest.mark.integration


def _sha512(path: Path) -> str:
    return hashlib.sha512(path.read_bytes()).hexdigest()


def test_compile_dispatcher_is_byte_reproducible(tmp_path: Path) -> None:
    """Two compiles of identical source + pinned image are sha512-identical."""
    host_config = HostConfig.from_toml()

    build_a = tmp_path / "build-a"
    build_b = tmp_path / "build-b"
    out_a = tmp_path / "dispatch-a"
    out_b = tmp_path / "dispatch-b"

    compile_dispatcher(str(build_a), str(out_a), host_config)
    compile_dispatcher(str(build_b), str(out_b), host_config)

    assert out_a.is_file(), "first compile produced no binary"
    assert out_b.is_file(), "second compile produced no binary"
    assert out_a.stat().st_size > 0
    assert _sha512(out_a) == _sha512(out_b), (
        "compile is not byte-reproducible: the two output binaries differ"
    )
