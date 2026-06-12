# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the L7 helper-image pre-pull phase.

Covers: probe branches (ALREADY_CORRECT pinned-digest cached, DRIFT
tag-moved, MISSING no-image), act pull, reverify true/false, the content-aware
fixture (pinned→tag-drift), and the PHASE shape. ``Executor.run`` is faked —
no real machinectl / docker.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest
from core.exceptions import SandboxExecutionError
from core.host_config import (
    DockerExecutionMode,
    MachinectlAuth,
    minimal_host_config,
)
from core.setup import l7_helper_image as l7
from core.setup.phase_runner import Identity, PhaseResult, SetupContext

if TYPE_CHECKING:
    from collections.abc import Callable

    from core.setup.phase_runner import Phase


@pytest.fixture
def ctx() -> SetupContext:
    return SetupContext(
        host_config=minimal_host_config(
            "sandboxuser", MachinectlAuth.SUDO, DockerExecutionMode.SEPARATE_USER
        ),
        operator="op",
    )


class _Store:
    """Fake local image store: a set of resolvable refs."""

    def __init__(self) -> None:
        self.refs: set[str] = set()
        self.pulled: list[str] = []


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    s = _Store()

    def fake_run(
        _self: object,
        cmd: list[str],
        *_a: object,
        **_kw: object,
    ) -> subprocess.CompletedProcess[str]:
        inner = cmd[-1]
        if inner.startswith("docker image inspect "):
            ref = inner.removeprefix("docker image inspect ")
            if ref in s.refs:
                return subprocess.CompletedProcess(cmd, 0, "[]", "")
            raise SandboxExecutionError("no such image")
        if inner.startswith("docker pull "):
            ref = inner.removeprefix("docker pull ")
            s.pulled.append(ref)
            s.refs.add(ref)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(f"unexpected: {inner}")

    monkeypatch.setattr("core.executor.Executor.run", fake_run)
    return s


def test_probe_already_correct_when_digest_cached(
    store: _Store, ctx: SetupContext
) -> None:
    store.refs.add(l7._HELPER_REF)
    result, _ = l7.PHASE.probe(ctx)
    assert result == PhaseResult.ALREADY_CORRECT


def test_probe_drift_when_tag_moved(store: _Store, ctx: SetupContext) -> None:
    store.refs.add(l7._HELPER_TAGGED)
    result, detail = l7.PHASE.probe(ctx)
    assert result == PhaseResult.DRIFT
    assert "not at the pinned digest" in detail


def test_probe_missing_when_no_image(store: _Store, ctx: SetupContext) -> None:
    result, _ = l7.PHASE.probe(ctx)
    assert result == PhaseResult.MISSING


def test_act_pulls_pinned_digest(store: _Store, ctx: SetupContext) -> None:
    detail = l7.PHASE.act(ctx)
    assert l7._HELPER_REF in store.pulled
    assert "pulled" in detail


def test_act_separate_user_pull_crosses_via_machinectl_sentinel(
    monkeypatch: pytest.MonkeyPatch, ctx: SetupContext
) -> None:
    """C-009 §3.7 guard: the L7 ROOT crossing stays machinectl + ``sentinel=True``.

    L7 runs as root BEFORE the operator sudoers rule exists, crossing into the
    sandbox user via ``daemon_owner_crossing`` (``machinectl_cmd`` in
    separate-user) with the orchestrator-injected sentinel ON. It neither uses
    nor depends on the operator pipe ``Cmnd_Spec`` (L3a/L8's ``framed=True``
    pipe path), so it MUST NOT be touched.
    """
    seen: list[tuple[list[str], object]] = []

    def fake_run(
        _self: object, cmd: list[str], *_a: object, **kw: object
    ) -> subprocess.CompletedProcess[str]:
        seen.append((cmd, kw.get("sentinel")))
        inner = cmd[-1]
        if inner.startswith("docker image inspect "):
            raise SandboxExecutionError("no such image")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("core.executor.Executor.run", fake_run)

    l7.PHASE.act(ctx)

    pulls = [
        (c, s) for c, s in seen if c[-1].startswith("docker pull ")
    ]
    assert len(pulls) == 1
    cmd, sentinel = pulls[0]
    assert "machinectl" in cmd
    assert sentinel is True
    assert "systemd-run" not in cmd


def test_reverify_true_after_pull(store: _Store, ctx: SetupContext) -> None:
    l7.PHASE.act(ctx)
    assert l7.PHASE.reverify(ctx) is True


def test_reverify_false_when_absent(store: _Store, ctx: SetupContext) -> None:
    assert l7.PHASE.reverify(ctx) is False


def test_content_aware(
    store: _Store,
    ctx: SetupContext,
    assert_phase_content_aware: Callable[
        [Phase, SetupContext, Callable[[], None]], None
    ],
) -> None:
    store.refs.add(l7._HELPER_REF)

    def make_stale() -> None:
        # An upstream rotation moved the tag: the pinned digest is gone, only
        # the (now-different) tag resolves locally. Probe must report DRIFT.
        store.refs.discard(l7._HELPER_REF)
        store.refs.add(l7._HELPER_TAGGED)

    assert_phase_content_aware(l7.PHASE, ctx, make_stale)


# ── operator-rootless: LOCAL docker pull, no machinectl (§6.4) ────────────────


def test_act_operator_rootless_local_pull(monkeypatch: pytest.MonkeyPatch) -> None:
    """op-rootless act pulls the pinned digest LOCAL with the injected session env
    (no machinectl, sentinel off) — finding 8.11.

    The daemon owner is the operator, so the pull is a local ``docker`` subprocess
    in the operator's session — but the sterile Executor scrubs the env, so the
    crossing injects HOME / XDG_RUNTIME_DIR / DBUS / DOCKER_HOST so ``docker`` hits
    the rootless socket (not the rootful ``/var/run/docker.sock``).
    """
    import pwd

    monkeypatch.setattr(
        "core.setup.phase_runner.pwd.getpwnam",
        lambda _n: pwd.struct_passwd(("alice", "x", 5000, 5000, "", "/home/alice", "/bin/bash")),
    )
    seen: list[tuple[list[str], object]] = []

    def fake_run(
        _self: object, cmd: list[str], *_a: object, **kw: object
    ) -> subprocess.CompletedProcess[str]:
        seen.append((cmd, kw.get("sentinel")))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("core.executor.Executor.run", fake_run)
    ctx = SetupContext(
        host_config=minimal_host_config(
            "sandboxuser",
            MachinectlAuth.SUDO,
            mode=DockerExecutionMode.OPERATOR_ROOTLESS,
        ),
        operator="alice",
    )

    detail = l7.PHASE.act(ctx)

    assert f"docker pull {l7._HELPER_REF}" in detail or "pulled" in detail
    (cmd, sentinel), = seen
    assert cmd == [
        "env",
        "HOME=/home/alice",
        "XDG_RUNTIME_DIR=/run/user/5000",
        "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/5000/bus",
        "DOCKER_HOST=unix:///run/user/5000/docker.sock",
        "/bin/bash",
        "-c",
        f"docker pull {l7._HELPER_REF}",
    ]
    assert "machinectl" not in " ".join(cmd)
    assert sentinel is False


def test_phase_shape() -> None:
    assert l7.PHASE.id == "l7"
    assert l7.PHASE.depends_on == ("l65",)
    assert l7.PHASE.identity == Identity.SANDBOX
