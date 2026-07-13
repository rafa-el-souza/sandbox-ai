# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared test fixtures for unit tests.

Centralizes Rich Console construction for deterministic, ANSI-free output capture.
"""

import re
from io import StringIO
from typing import TYPE_CHECKING, NamedTuple, Protocol

import pytest
from rich.console import Console

if TYPE_CHECKING:
    from core.host_config import HostConfig


class HostConfigFactory(Protocol):
    def __call__(self, *, user: str = ...) -> HostConfig: ...


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
def project_config_factory() -> HostConfigFactory:
    """Build ``HostConfig`` instances for the host-config flow.

    Usage::

        def test_x(project_config_factory: HostConfigFactory) -> None:
            pc = project_config_factory(user="sandbox")
    """
    from core.host_config import HostConfig

    def _make(*, user: str = "sandbox") -> HostConfig:
        return HostConfig.model_validate(
            {"host": {"docker_unprivileged_user": user}}
        )

    return _make


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
