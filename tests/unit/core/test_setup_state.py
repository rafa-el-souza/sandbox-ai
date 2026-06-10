"""Tests for the execution-mode marker reader/writer (`core.setup_state`, D6)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from core import setup_state
from core.host_config import DockerExecutionMode


@pytest.fixture
def marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the marker path into a tmp dir for isolation."""
    path = tmp_path / "libexec" / "setup-state.json"
    monkeypatch.setattr(setup_state, "MARKER_PATH", path)
    return path


def test_read_mode_absent_file_returns_none(marker: Path) -> None:
    assert not marker.exists()
    assert setup_state.read_mode("alice") is None


def test_write_then_read_round_trip(marker: Path) -> None:
    setup_state.write_mode("alice", DockerExecutionMode.OPERATOR_ROOTLESS)
    assert setup_state.read_mode("alice") is DockerExecutionMode.OPERATOR_ROOTLESS


def test_read_mode_no_entry_for_operator_returns_none(marker: Path) -> None:
    setup_state.write_mode("alice", DockerExecutionMode.SEPARATE_USER)
    assert setup_state.read_mode("bob") is None


def test_write_preserves_other_operators(marker: Path) -> None:
    setup_state.write_mode("alice", DockerExecutionMode.OPERATOR_ROOTLESS)
    setup_state.write_mode("bob", DockerExecutionMode.SEPARATE_USER)

    assert setup_state.read_mode("alice") is DockerExecutionMode.OPERATOR_ROOTLESS
    assert setup_state.read_mode("bob") is DockerExecutionMode.SEPARATE_USER


def test_write_overwrites_same_operator_without_touching_others(marker: Path) -> None:
    setup_state.write_mode("alice", DockerExecutionMode.SEPARATE_USER)
    setup_state.write_mode("bob", DockerExecutionMode.OPERATOR_ROOTLESS)
    setup_state.write_mode("alice", DockerExecutionMode.OPERATOR_ROOTLESS)

    assert setup_state.read_mode("alice") is DockerExecutionMode.OPERATOR_ROOTLESS
    assert setup_state.read_mode("bob") is DockerExecutionMode.OPERATOR_ROOTLESS


def test_write_lands_mode_0644(marker: Path) -> None:
    setup_state.write_mode("alice", DockerExecutionMode.SEPARATE_USER)
    mode = stat.S_IMODE(marker.stat().st_mode)
    assert mode == 0o644


def test_write_serializes_expected_shape(marker: Path) -> None:
    setup_state.write_mode("alice", DockerExecutionMode.OPERATOR_ROOTLESS)
    data = json.loads(marker.read_text())
    assert data == {"operators": {"alice": {"mode": "operator-rootless"}}}


def test_read_mode_malformed_json_raises(marker: Path) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{ not valid json")
    with pytest.raises(json.JSONDecodeError):
        setup_state.read_mode("alice")


def test_write_mode_atomic_cleanup_on_failure(
    marker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure during the atomic replace removes the temp file and re-raises."""

    def boom(_src: str, _dst: object) -> None:
        raise OSError("rename failed")

    monkeypatch.setattr("core.setup_state.os.replace", boom)
    with pytest.raises(OSError, match="rename failed"):
        setup_state.write_mode("alice", DockerExecutionMode.SEPARATE_USER)

    # No stray temp files left behind in the marker directory.
    leftovers = list(marker.parent.glob(".setup-state-*.tmp"))
    assert leftovers == []


# ── resolve_execution_mode (runtime authority, D11) + write_mode_root_owned ──


def test_resolve_execution_mode_returns_recorded(marker: Path) -> None:
    setup_state.write_mode("alice", DockerExecutionMode.OPERATOR_ROOTLESS)
    assert (
        setup_state.resolve_execution_mode("alice")
        is DockerExecutionMode.OPERATOR_ROOTLESS
    )


def test_resolve_execution_mode_missing_marker_fails_closed(marker: Path) -> None:
    assert not marker.exists()
    with pytest.raises(setup_state.ModeMarkerMissing, match="sandbox setup"):
        setup_state.resolve_execution_mode("alice")


def test_resolve_execution_mode_no_entry_for_operator_fails_closed(marker: Path) -> None:
    setup_state.write_mode("alice", DockerExecutionMode.SEPARATE_USER)
    with pytest.raises(setup_state.ModeMarkerMissing, match="bob"):
        setup_state.resolve_execution_mode("bob")


def test_write_mode_root_owned_writes_and_root_owns(
    marker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared root-owned write lands content + 0644 + chown root:root."""
    chowns: list[tuple[object, int, int]] = []
    monkeypatch.setattr("core.setup_state.os.chown", lambda p, u, g: chowns.append((p, u, g)))

    setup_state.write_mode_root_owned("alice", DockerExecutionMode.SEPARATE_USER)

    assert setup_state.read_mode("alice") is DockerExecutionMode.SEPARATE_USER
    assert stat.S_IMODE(marker.stat().st_mode) == 0o644
    assert chowns == [(marker, 0, 0)]
