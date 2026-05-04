"""Shared test fixtures applied across both unit and integration suites."""

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_sandbox_ai_user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect SANDBOX_AI_USER_HOME to a per-test tmp_path subdirectory.

    Ensures every test sees a fresh per-user home, eliminating cross-test
    contamination of host config and orchestrator state. The directory is
    NOT created here; tests that need a populated home create it explicitly
    (or invoke the production code paths that do so).
    """
    home = tmp_path / ".sandbox-ai"
    monkeypatch.setenv("SANDBOX_AI_USER_HOME", str(home))
    yield home
