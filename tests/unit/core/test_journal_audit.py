"""Tests for the operator-side journald audit shim (``core.journal_audit``)."""

from __future__ import annotations

import socket
import struct
from typing import TYPE_CHECKING

import pytest
from core import journal_audit

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def journal_listener(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[socket.socket]:
    """Bind a real ``AF_UNIX``/``SOCK_DGRAM`` listener and point the shim at it.

    Yields the bound receiving socket; ``_JOURNAL_SOCKET`` is monkeypatched to
    the listener's path so ``emit_op_audit`` delivers to it.
    """
    sock_path = tmp_path / "journal.socket"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    listener.bind(str(sock_path))
    monkeypatch.setattr("core.journal_audit._JOURNAL_SOCKET", str(sock_path))
    try:
        yield listener
    finally:
        listener.close()


def _parse_fields(payload: bytes) -> dict[str, str]:
    """Parse a journal export payload into a {KEY: VALUE} dict (newline-free)."""
    fields: dict[str, str] = {}
    for line in payload.split(b"\n"):
        if not line:
            continue
        key, _, value = line.partition(b"=")
        fields[key.decode("utf-8")] = value.decode("utf-8")
    return fields


def test_truncate256_short_unchanged() -> None:
    assert journal_audit._truncate256("abc") == "abc"
    boundary = "x" * 256
    assert journal_audit._truncate256(boundary) == boundary


def test_truncate256_long_truncated() -> None:
    long = "y" * 300
    result = journal_audit._truncate256(long)
    assert result == "y" * 256
    assert len(result) == 256


def test_encode_journal_fields_newline_free() -> None:
    encoded = journal_audit._encode_journal_fields({"KEY": "VALUE"})
    assert encoded == b"KEY=VALUE\n"


def test_encode_journal_fields_multiline() -> None:
    value = "line1\nline2"
    encoded = journal_audit._encode_journal_fields({"KEY": value})
    expected = b"KEY\n" + struct.pack("<Q", len(value.encode("utf-8"))) + value.encode("utf-8") + b"\n"
    assert encoded == expected


def test_emit_op_audit_delivers_record(journal_listener: socket.socket) -> None:
    journal_audit.emit_op_audit(
        "compose-up",
        ["inst"],
        ["/bin/bash", "-c", "docker compose ... up"],
        "inst",
    )
    payload = journal_listener.recv(65536)
    fields = _parse_fields(payload)
    assert fields["MESSAGE"] == "dispatch compose-up inst"
    assert fields["PRIORITY"] == "6"
    assert fields["SANDBOX_AI_OP"] == "compose-up"
    assert fields["SANDBOX_AI_INSTANCE"] == "inst"
    assert fields["SANDBOX_AI_ARGS_SUMMARY"] == "inst"
    assert fields["SANDBOX_AI_TARGET_ARGV_SUMMARY"] == "/bin/bash -c docker compose ... up"


def test_emit_op_audit_does_not_emit_check_field(journal_listener: socket.socket) -> None:
    journal_audit.emit_op_audit("auth-probe", [], ["/bin/bash", "-c", "echo ok"], "inst")
    payload = journal_listener.recv(65536)
    fields = _parse_fields(payload)
    assert "SANDBOX_AI_CHECK" not in fields


def test_emit_op_audit_truncates_long_summaries(journal_listener: socket.socket) -> None:
    long_args = ["a" * 400]
    long_argv = ["b" * 400]
    journal_audit.emit_op_audit("compose-up", long_args, long_argv, "inst")
    payload = journal_listener.recv(65536)
    fields = _parse_fields(payload)
    assert len(fields["SANDBOX_AI_ARGS_SUMMARY"]) == 256
    assert len(fields["SANDBOX_AI_TARGET_ARGV_SUMMARY"]) == 256


def test_emit_op_audit_silent_when_socket_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "does-not-exist.socket"
    monkeypatch.setattr("core.journal_audit._JOURNAL_SOCKET", str(missing))
    # Must not raise even though connect() fails with FileNotFoundError (an OSError).
    journal_audit.emit_op_audit("compose-up", ["inst"], ["/bin/bash"], "inst")
