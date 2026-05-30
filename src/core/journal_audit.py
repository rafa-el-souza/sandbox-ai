"""Operator-side journald audit shim for ``operator-rootless`` mode.

In ``separate-user`` mode every runtime op crosses into the dedicated
unprivileged ``sandbox`` user via the root-owned Go dispatcher, which emits a
structured systemd-journald record (``SANDBOX_AI_*`` fields) for the audit
trail. In ``operator-rootless`` mode (C-003, design D4) the dispatcher binary
is bypassed entirely — ops run as plain local subprocesses — so that audit
record would be lost. This module re-emits the SAME structured record
operator-side, mirroring the Go dispatcher's ``journalLog`` /
``encodeJournalFields`` byte-for-byte.

There is intentionally no ``systemd`` Python dependency. Like the Go binary,
this writes the native journal protocol directly over an ``AF_UNIX`` /
``SOCK_DGRAM`` socket to ``/run/systemd/journal/socket`` using only stdlib
``socket``.

Intentional divergence from the Go version: the Go dispatcher prints a stderr
note when journald is unavailable. Operator-side, stderr is the user's own
terminal, so this shim stays silent — it catches ``OSError`` and returns
without raising or printing. The op must never fail because journald is down.
"""

from __future__ import annotations

import socket
import struct
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_JOURNAL_SOCKET = "/run/systemd/journal/socket"


def _truncate256(s: str) -> str:
    """Return ``s`` truncated to 256 chars (mirror Go ``truncate256``)."""
    return s[:256] if len(s) > 256 else s


def _encode_journal_fields(fields: Mapping[str, str]) -> bytes:
    """Serialize fields in the native journal export format.

    Mirrors Go ``encodeJournalFields``: a newline-free value encodes as
    ``KEY=VALUE\\n``; a value containing a newline encodes as ``KEY\\n`` +
    8-byte little-endian length of the value's **bytes** + the raw value +
    ``\\n``. All our values are newline-free in practice, but the multiline
    form is implemented faithfully.
    """
    out = bytearray()
    for key, value in fields.items():
        key_bytes = key.encode("utf-8")
        value_bytes = value.encode("utf-8")
        if b"\n" in value_bytes:
            out += key_bytes
            out += b"\n"
            out += struct.pack("<Q", len(value_bytes))
            out += value_bytes
            out += b"\n"
        else:
            out += key_bytes
            out += b"="
            out += value_bytes
            out += b"\n"
    return bytes(out)


def emit_op_audit(
    op: str,
    args: Sequence[str],
    target_argv: Sequence[str],
    instance: str,
) -> None:
    """Emit one structured journald record for a local op (operator-rootless).

    Faithful mirror of the Go dispatcher's ``journalLog``: builds the
    ``SANDBOX_AI_*`` field set and writes a single datagram over the native
    journal protocol. Silent best-effort — any ``OSError`` (journald socket
    missing/unconnectable/unwritable) is swallowed so the op never fails
    because journald is unavailable.
    """
    fields = {
        "MESSAGE": f"dispatch {op} {' '.join(args)}",
        "PRIORITY": "6",
        "SANDBOX_AI_OP": op,
        "SANDBOX_AI_ARGS_SUMMARY": _truncate256(",".join(args)),
        "SANDBOX_AI_TARGET_ARGV_SUMMARY": _truncate256(" ".join(target_argv)),
        "SANDBOX_AI_INSTANCE": instance,
    }
    payload = _encode_journal_fields(fields)

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.connect(_JOURNAL_SOCKET)
            sock.send(payload)
        finally:
            sock.close()
    except OSError:
        return
