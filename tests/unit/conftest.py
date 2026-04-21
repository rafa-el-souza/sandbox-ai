"""Shared test fixtures for unit tests.

Centralizes Rich Console construction for deterministic, ANSI-free output capture.
"""

import re
from io import StringIO
from typing import NamedTuple

import pytest
from rich.console import Console

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class CapturedConsole(NamedTuple):
    """A Rich Console wired to a StringIO buffer with an ANSI-stripped accessor."""

    console: Console
    buffer: StringIO

    @property
    def plain_output(self) -> str:
        """Return captured output with all ANSI escape sequences stripped."""
        return _ANSI_RE.sub("", self.buffer.getvalue())

    @property
    def raw_output(self) -> str:
        """Return captured output with ANSI escapes intact (for escape-aware assertions)."""
        return self.buffer.getvalue()


@pytest.fixture()
def captured_console() -> CapturedConsole:
    """Provide a Rich Console that captures output to an in-memory buffer.

    Usage in tests::

        def test_something(captured_console: CapturedConsole) -> None:
            render_results(results, console=captured_console.console)
            assert "2/2 passed" in captured_console.plain_output

    The ``plain_output`` property strips ANSI escape sequences, which Rich
    emits even with ``no_color=True`` when ``force_terminal=True`` is set
    (bold, dim, underline escapes are structural, not color).  Using this
    fixture eliminates the recurring class of assertion failures caused by
    interleaved ``\\x1b[1m`` / ``\\x1b[0m`` sequences breaking contiguous
    substring matches.
    """
    buf = StringIO()
    console = Console(
        file=buf,
        force_terminal=True,
        no_color=True,
        highlight=False,
        width=120,
    )
    return CapturedConsole(console=console, buffer=buf)
