"""Unit tests for ``core.setup.l1_kernel`` (sysctl-only after the L1→l2a split).

L1 now owns ONLY the user-independent sysctl drop-in + the verify-only ACL FS
/ cgroup-v2 checks. The uid-scoped systemd ``Delegate=yes`` drop-in moved out
to ``core.setup.l2a_delegate`` (it depends on L2 having created the sandbox
user). L1's probe must no longer resolve any OS user. Covers the
content-aware sysctl probe (MISSING / DRIFT / ALREADY_CORRECT), the
Debian-family branch, the verify-only CONFLICT refusals, act success +
failure, reverify, and the conftest content-aware fixture.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from core.host_config import (
    DockerExecutionMode,
    MachinectlAuth,
    minimal_host_config,
)
from core.setup import l1_kernel
from core.setup.l1_kernel import PHASE, render_sysctl_dropin
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
            "sandboxuser", MachinectlAuth.SUDO, DockerExecutionMode.SEPARATE_USER
        ),
        operator="op",
    )


def _verify_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """ACL FS + cgroup v2 both pass (so the probe reaches the drop-in logic)."""
    monkeypatch.setattr(
        "core.setup.l1_kernel._acl_fs_supported", lambda: True
    )
    monkeypatch.setattr(
        "core.setup.l1_kernel._cgroup_v2_active", lambda: True
    )


# ── PHASE wiring ─────────────────────────────────────────────────────────────


def test_phase_identity_and_deps() -> None:
    assert PHASE.id == "l1"
    assert PHASE.depends_on == ("l0",)
    assert PHASE.identity == Identity.ROOT
    # separate-user only: the sysctl drop-in is host-root, so in operator-rootless
    # it is owned by the host_batch SYSCTL item + _bootstrap-host escalation
    # (D5a/O3); the runner reports the phase skipped there.
    assert PHASE.applies_in == frozenset({DockerExecutionMode.SEPARATE_USER})


def test_l1_does_not_resolve_an_os_user(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: L1's probe must NOT resolve any OS user (L1 runs before L2).

    A fresh host has no sandbox user; a ``pwd.getpwnam`` in L1's probe path
    would crash ``sandbox setup``. Make getpwnam explode and assert the probe
    still classifies cleanly.
    """
    _verify_ok(monkeypatch)
    monkeypatch.setattr("core.setup.l1_kernel.detect_distro", lambda: "fedora")
    sysctl = tmp_path / "49-sandbox-ai.conf"
    sysctl.write_text(render_sysctl_dropin())
    monkeypatch.setattr("core.setup.l1_kernel._SYSCTL_DROPIN", sysctl)

    def _boom(_n: str) -> object:
        raise KeyError("getpwnam(): name not found: 'sandbox'")

    monkeypatch.setattr("pwd.getpwnam", _boom)
    result, _ = PHASE.probe(_ctx())
    assert result == PhaseResult.ALREADY_CORRECT


def test_no_delegate_symbols_remain() -> None:
    assert not hasattr(l1_kernel, "_sandbox_uid")
    assert not hasattr(l1_kernel, "_delegate_dropin_path")
    assert not hasattr(l1_kernel, "render_delegate_dropin")


# ── render: Debian-family branch ─────────────────────────────────────────────


def test_render_sysctl_debian(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.setup.l1_kernel.detect_distro", lambda: "debian")
    body = render_sysctl_dropin()
    assert "user.max_user_namespaces=15000" in body
    assert "kernel.unprivileged_userns_clone=1" in body
    assert body.startswith("# sandbox-ai managed")


def test_render_sysctl_non_debian(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.setup.l1_kernel.detect_distro", lambda: "fedora")
    body = render_sysctl_dropin()
    assert "user.max_user_namespaces=15000" in body
    assert "kernel.unprivileged_userns_clone" not in body


# ── verify-only CONFLICT refusals ────────────────────────────────────────────


def test_probe_conflict_no_acl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "core.setup.l1_kernel._acl_fs_supported", lambda: False
    )
    result, detail = PHASE.probe(_ctx())
    assert result == PhaseResult.CONFLICT
    assert "POSIX" in detail


def test_probe_conflict_no_cgroup_v2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core.setup.l1_kernel._acl_fs_supported", lambda: True
    )
    monkeypatch.setattr(
        "core.setup.l1_kernel._cgroup_v2_active", lambda: False
    )
    result, detail = PHASE.probe(_ctx())
    assert result == PhaseResult.CONFLICT
    assert "cgroup v2" in detail


# ── probe MISSING / DRIFT / ALREADY_CORRECT ──────────────────────────────────


def test_probe_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _verify_ok(monkeypatch)
    monkeypatch.setattr("core.setup.l1_kernel.detect_distro", lambda: "debian")
    monkeypatch.setattr(
        "core.setup.l1_kernel._SYSCTL_DROPIN", tmp_path / "absent.conf"
    )
    result, detail = PHASE.probe(_ctx())
    assert result == PhaseResult.MISSING
    assert "absent" in detail


def test_probe_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _verify_ok(monkeypatch)
    monkeypatch.setattr("core.setup.l1_kernel.detect_distro", lambda: "debian")
    sysctl = tmp_path / "49-sandbox-ai.conf"
    sysctl.write_text("# stale\nuser.max_user_namespaces=1\n")
    monkeypatch.setattr("core.setup.l1_kernel._SYSCTL_DROPIN", sysctl)
    result, detail = PHASE.probe(_ctx())
    assert result == PhaseResult.DRIFT
    assert "49-sandbox-ai.conf" in detail


def test_probe_already_correct(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _verify_ok(monkeypatch)
    monkeypatch.setattr("core.setup.l1_kernel.detect_distro", lambda: "debian")
    sysctl = tmp_path / "49-sandbox-ai.conf"
    sysctl.write_text(render_sysctl_dropin())
    monkeypatch.setattr("core.setup.l1_kernel._SYSCTL_DROPIN", sysctl)
    result, _ = PHASE.probe(_ctx())
    assert result == PhaseResult.ALREADY_CORRECT


# ── act success + failure ────────────────────────────────────────────────────


def test_act_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _verify_ok(monkeypatch)
    monkeypatch.setattr("core.setup.l1_kernel.detect_distro", lambda: "debian")
    sysctl = tmp_path / "49-sandbox-ai.conf"
    monkeypatch.setattr("core.setup.l1_kernel._SYSCTL_DROPIN", sysctl)
    monkeypatch.setattr("os.chown", lambda *a: None)
    runs: list[list[str]] = []

    def _run(argv: list[str], **_k: object) -> object:
        runs.append(argv)

        class _P:
            returncode = 0

        return _P()

    monkeypatch.setattr("subprocess.run", _run)
    detail = PHASE.act(_ctx())
    assert sysctl.read_text() == render_sysctl_dropin()
    assert ["sysctl", "-w", "user.max_user_namespaces=15000"] in runs
    assert "kernel.unprivileged_userns_clone=1" in detail


def test_act_non_debian_skips_userns_clone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _verify_ok(monkeypatch)
    monkeypatch.setattr("core.setup.l1_kernel.detect_distro", lambda: "fedora")
    monkeypatch.setattr(
        "core.setup.l1_kernel._SYSCTL_DROPIN", tmp_path / "s.conf"
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
    assert "kernel.unprivileged_userns_clone" not in detail
    assert not any("kernel.unprivileged_userns_clone=1" in a for r in runs for a in r)


def test_act_failure_propagates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import subprocess

    _verify_ok(monkeypatch)
    monkeypatch.setattr("core.setup.l1_kernel.detect_distro", lambda: "debian")
    monkeypatch.setattr(
        "core.setup.l1_kernel._SYSCTL_DROPIN", tmp_path / "s.conf"
    )
    monkeypatch.setattr("os.chown", lambda *a: None)

    def _boom(argv: list[str], **_k: object) -> object:
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr("subprocess.run", _boom)
    with pytest.raises(subprocess.CalledProcessError):
        PHASE.act(_ctx())


# ── reverify ─────────────────────────────────────────────────────────────────


def test_reverify_true(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _verify_ok(monkeypatch)
    monkeypatch.setattr("core.setup.l1_kernel.detect_distro", lambda: "debian")
    sysctl = tmp_path / "s.conf"
    sysctl.write_text(render_sysctl_dropin())
    monkeypatch.setattr("core.setup.l1_kernel._SYSCTL_DROPIN", sysctl)
    assert PHASE.reverify(_ctx()) is True


def test_reverify_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _verify_ok(monkeypatch)
    monkeypatch.setattr("core.setup.l1_kernel.detect_distro", lambda: "debian")
    monkeypatch.setattr(
        "core.setup.l1_kernel._SYSCTL_DROPIN", tmp_path / "absent.conf"
    )
    assert PHASE.reverify(_ctx()) is False


# ── verify-only helpers (direct, for coverage of subprocess branches) ────────


def test_acl_fs_supported_true(monkeypatch: pytest.MonkeyPatch) -> None:
    class _P:
        returncode = 0

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _P())
    assert l1_kernel._acl_fs_supported() is True


def test_acl_fs_supported_subprocess_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_a: object, **_k: object) -> object:
        raise OSError

    monkeypatch.setattr("subprocess.run", _boom)
    assert l1_kernel._acl_fs_supported() is False


def test_cgroup_v2_active(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "core.setup.l1_kernel.Path.exists", lambda self: True
    )
    assert l1_kernel._cgroup_v2_active() is True


def test_cgroup_v2_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "core.setup.l1_kernel.Path.exists", lambda self: False
    )
    assert l1_kernel._cgroup_v2_active() is False


# ── content-aware probe contract (conftest fixture) ──────────────────────────


def test_content_aware(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    assert_phase_content_aware: ContentAwareAssertion,
) -> None:
    _verify_ok(monkeypatch)
    monkeypatch.setattr("core.setup.l1_kernel.detect_distro", lambda: "debian")
    sysctl = tmp_path / "49-sandbox-ai.conf"
    sysctl.write_text(render_sysctl_dropin())
    monkeypatch.setattr("core.setup.l1_kernel._SYSCTL_DROPIN", sysctl)

    def _make_stale() -> None:
        sysctl.write_text("# wheel upgrade changed the expected body\n")

    assert_phase_content_aware(PHASE, _ctx(), _make_stale)
