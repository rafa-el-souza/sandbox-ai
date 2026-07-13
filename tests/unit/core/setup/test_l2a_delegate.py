# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for ``core.setup.l2a_delegate``.

The uid-scoped systemd ``Delegate=yes`` drop-in, split out of L1 so it runs
*after* L2 has created the sandbox user (its path embeds the sandbox uid).
Covers PHASE wiring, the content-aware probe (MISSING / DRIFT /
ALREADY_CORRECT), the getpwnam-absent → MISSING guard (fresh-host first run),
act, reverify, and the conftest content-aware fixture.
"""

from __future__ import annotations

import pwd
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from core.host_config import (
    DockerExecutionMode,
    minimal_host_config,
)
from core.setup.l2a_delegate import PHASE, render_delegate_dropin
from core.setup.phase_runner import Identity, PhaseResult, SetupContext

if TYPE_CHECKING:
    from collections.abc import Callable

    from core.setup.phase_runner import Phase

    ContentAwareAssertion = Callable[
        [Phase, SetupContext, Callable[[], None]], None
    ]


def _ctx() -> SetupContext:
    return SetupContext(
        host_config=minimal_host_config(
            "sandboxuser", DockerExecutionMode.SEPARATE_USER
        ),
        operator="op",
    )


def _fake_pw(uid: int) -> pwd.struct_passwd:
    return pwd.struct_passwd(
        ("sandboxuser", "x", uid, uid, "", "/home/sb", "/bin/bash")
    )


def _user_at(monkeypatch: pytest.MonkeyPatch, uid: int) -> None:
    monkeypatch.setattr("pwd.getpwnam", lambda _n: _fake_pw(uid))


# ── PHASE wiring ─────────────────────────────────────────────────────────────


def test_phase_identity_and_deps() -> None:
    assert PHASE.id == "l2a"
    assert PHASE.depends_on == ("l2",)
    assert PHASE.identity == Identity.ROOT
    assert PHASE.rollback is None
    # separate-user only: the Delegate drop-in is host-root, so in operator-rootless
    # it is owned by the host_batch DELEGATE item + _bootstrap-host escalation
    # (D5a/O3); the runner reports the phase skipped there.
    assert PHASE.applies_in == frozenset({DockerExecutionMode.SEPARATE_USER})


def test_render_delegate() -> None:
    body = render_delegate_dropin()
    assert "[Service]" in body
    assert "Delegate=yes" in body
    assert body.startswith("# sandbox-ai managed")


# ── getpwnam-absent → MISSING guard (fresh-host first run) ───────────────────


def test_probe_user_not_yet_created_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical fresh-host first run: sandbox user does not exist yet.

    The not-yet-created user IS the MISSING signal (L2 creates it) — never a
    KeyError crashing the plan/apply pass.
    """

    def _boom(_n: str) -> object:
        raise KeyError("getpwnam(): name not found: 'sandboxuser'")

    monkeypatch.setattr("pwd.getpwnam", _boom)
    result, detail = PHASE.probe(_ctx())
    assert result == PhaseResult.MISSING
    assert "does not exist yet" in detail


# ── probe MISSING / DRIFT / ALREADY_CORRECT ──────────────────────────────────


def test_probe_missing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _user_at(monkeypatch, 4242)
    monkeypatch.setattr(
        "core.setup.l2a_delegate._SYSTEMD_SYSTEM", tmp_path
    )
    result, detail = PHASE.probe(_ctx())
    assert result == PhaseResult.MISSING
    assert "absent" in detail


def test_probe_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _user_at(monkeypatch, 4242)
    monkeypatch.setattr(
        "core.setup.l2a_delegate._SYSTEMD_SYSTEM", tmp_path
    )
    dele = tmp_path / "user-4242.service.d" / "sandbox-ai-delegate.conf"
    dele.parent.mkdir(parents=True)
    dele.write_text("# stale delegate\n")
    result, detail = PHASE.probe(_ctx())
    assert result == PhaseResult.DRIFT
    assert "sandbox-ai-delegate.conf" in detail


def test_probe_already_correct(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _user_at(monkeypatch, 4242)
    monkeypatch.setattr(
        "core.setup.l2a_delegate._SYSTEMD_SYSTEM", tmp_path
    )
    dele = tmp_path / "user-4242.service.d" / "sandbox-ai-delegate.conf"
    dele.parent.mkdir(parents=True)
    dele.write_text(render_delegate_dropin())
    result, _ = PHASE.probe(_ctx())
    assert result == PhaseResult.ALREADY_CORRECT


# ── act ──────────────────────────────────────────────────────────────────────


def test_act_writes_dropin_and_reloads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _user_at(monkeypatch, 4242)
    monkeypatch.setattr(
        "core.setup.l2a_delegate._SYSTEMD_SYSTEM", tmp_path
    )
    monkeypatch.setattr("os.chown", lambda *a: None)
    runs: list[list[str]] = []

    def _run(argv: list[str], **_k: object) -> object:
        runs.append(argv)

        class _P:
            returncode = 0

        return _P()

    monkeypatch.setattr("subprocess.run", _run)
    detail = PHASE.act(_ctx())
    written = tmp_path / "user-4242.service.d" / "sandbox-ai-delegate.conf"
    assert written.read_text() == render_delegate_dropin()
    assert ["systemctl", "daemon-reload"] in runs
    assert "daemon-reload" in detail


def test_act_failure_propagates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import subprocess

    _user_at(monkeypatch, 4242)
    monkeypatch.setattr(
        "core.setup.l2a_delegate._SYSTEMD_SYSTEM", tmp_path
    )
    monkeypatch.setattr("os.chown", lambda *a: None)

    def _boom(argv: list[str], **_k: object) -> object:
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr("subprocess.run", _boom)
    with pytest.raises(subprocess.CalledProcessError):
        PHASE.act(_ctx())


def test_act_user_absent_raises_keyerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``act`` may raise (runner classifies FAIL) — only probe must be safe."""

    def _boom(_n: str) -> object:
        raise KeyError("name not found")

    monkeypatch.setattr("pwd.getpwnam", _boom)
    with pytest.raises(KeyError):
        PHASE.act(_ctx())


# ── reverify ─────────────────────────────────────────────────────────────────


def test_reverify_true(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _user_at(monkeypatch, 4242)
    monkeypatch.setattr(
        "core.setup.l2a_delegate._SYSTEMD_SYSTEM", tmp_path
    )
    dele = tmp_path / "user-4242.service.d" / "sandbox-ai-delegate.conf"
    dele.parent.mkdir(parents=True)
    dele.write_text(render_delegate_dropin())
    assert PHASE.reverify(_ctx()) is True


def test_reverify_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _user_at(monkeypatch, 4242)
    monkeypatch.setattr(
        "core.setup.l2a_delegate._SYSTEMD_SYSTEM", tmp_path
    )
    assert PHASE.reverify(_ctx()) is False


# ── content-aware probe contract (conftest fixture) ──────────────────────────


def test_content_aware(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    assert_phase_content_aware: ContentAwareAssertion,
) -> None:
    _user_at(monkeypatch, 4242)
    monkeypatch.setattr(
        "core.setup.l2a_delegate._SYSTEMD_SYSTEM", tmp_path
    )
    dele = tmp_path / "user-4242.service.d" / "sandbox-ai-delegate.conf"
    dele.parent.mkdir(parents=True)
    dele.write_text(render_delegate_dropin())

    def _make_stale() -> None:
        dele.write_text("# wheel upgrade changed the expected body\n")

    assert_phase_content_aware(PHASE, _ctx(), _make_stale)
