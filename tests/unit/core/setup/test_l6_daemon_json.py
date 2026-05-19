"""Unit tests for the L6 daemon.json reserved-key phase.

Covers: probe branches (MISSING file-absent, MISSING key-absent,
ALREADY_CORRECT deep-equal, DRIFT differing-value), act create + merge
(preserving operator runtimes) + restart cliff + no-restart-on-noop, the
corrupt-file refusal, reverify true/false, the content-aware fixture, and the
PHASE shape. ``pwd`` + ``Executor.run`` are faked — no real user / docker.
"""

from __future__ import annotations

import json
import pwd
import subprocess
from typing import TYPE_CHECKING

import pytest
from core.exceptions import SandboxExecutionError
from core.host_config import MachinectlAuth, minimal_host_config
from core.setup import l6_daemon_json as l6
from core.setup.phase_runner import Identity, PhaseResult, SetupContext

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from core.setup.phase_runner import Phase


@pytest.fixture
def ctx() -> SetupContext:
    return SetupContext(
        host_config=minimal_host_config("sandboxuser", MachinectlAuth.SUDO),
        operator="op",
    )


def _fake_pw(home: str) -> pwd.struct_passwd:
    return pwd.struct_passwd(
        ("sandboxuser", "x", 4242, 4242, "", home, "/bin/bash")
    )


@pytest.fixture
def daemon_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the daemon.json path to a tmp file (pwd home faked).

    The probe resolves the path from ``pwd.getpwnam(...).pw_dir`` via the
    shared sandbox-user guard, so fake ``pwd.getpwnam`` to return ``tmp_path``
    as the sandbox user's home; ``_daemon_json_path`` (still used by act /
    reverify) is patched to the same tmp file.
    """
    path = tmp_path / ".config" / "docker" / "daemon.json"
    monkeypatch.setattr("pwd.getpwnam", lambda _u: _fake_pw(str(tmp_path)))
    monkeypatch.setattr(l6, "_daemon_json_path", lambda _hc: path)
    return path


@pytest.fixture
def restarts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def fake_run(
        _self: object,
        cmd: list[str],
        *_a: object,
        **_kw: object,
    ) -> subprocess.CompletedProcess[str]:
        seen.append(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("core.executor.Executor.run", fake_run)
    return seen


def _write(path: Path, doc: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_probe_missing_when_file_absent(
    daemon_json: Path, ctx: SetupContext
) -> None:
    result, _ = l6.PHASE.probe(ctx)
    assert result == PhaseResult.MISSING


def test_probe_missing_when_key_absent(
    daemon_json: Path, ctx: SetupContext
) -> None:
    _write(daemon_json, {"runtimes": {"other": {"path": "/x"}}})
    result, detail = l6.PHASE.probe(ctx)
    assert result == PhaseResult.MISSING
    assert "reserved runtime key absent" in detail


def test_probe_missing_when_runtimes_not_a_dict(
    daemon_json: Path, ctx: SetupContext
) -> None:
    _write(daemon_json, {"runtimes": "not-a-dict"})
    result, _ = l6.PHASE.probe(ctx)
    assert result == PhaseResult.MISSING


def test_probe_already_correct(daemon_json: Path, ctx: SetupContext) -> None:
    _write(daemon_json, {"runtimes": {l6._RESERVED_RUNTIME_KEY: l6._EXPECTED_RUNTIME}})
    result, _ = l6.PHASE.probe(ctx)
    assert result == PhaseResult.ALREADY_CORRECT


def test_probe_drift_when_value_differs(
    daemon_json: Path, ctx: SetupContext
) -> None:
    _write(
        daemon_json,
        {"runtimes": {l6._RESERVED_RUNTIME_KEY: {"path": "/old/runsc"}}},
    )
    result, detail = l6.PHASE.probe(ctx)
    assert result == PhaseResult.DRIFT
    assert "differs from expected" in detail


def test_probe_refuses_non_object_json(
    daemon_json: Path, ctx: SetupContext
) -> None:
    _write(daemon_json, [1, 2, 3])
    with pytest.raises(SandboxExecutionError):
        l6.PHASE.probe(ctx)


def test_probe_missing_when_sandbox_user_not_yet_created(
    ctx: SetupContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B1-class guard: a not-yet-created sandbox user is MISSING, not a crash.

    The probe resolves ``~<sandbox-user>`` via the shared guard; on a fresh
    host (user created by an earlier phase) ``pwd.getpwnam`` raises KeyError —
    the probe must return MISSING, never let the KeyError escape.
    """

    def _boom(_u: str) -> object:
        raise KeyError("getpwnam(): name not found: 'sandboxuser'")

    monkeypatch.setattr("pwd.getpwnam", _boom)
    result, detail = l6.PHASE.probe(ctx)
    assert result == PhaseResult.MISSING
    assert "does not exist yet" in detail


def test_act_creates_and_merges_preserving_operator_runtimes(
    daemon_json: Path, ctx: SetupContext, restarts: list[str]
) -> None:
    _write(
        daemon_json,
        {"runtimes": {"op-runtime": {"path": "/usr/bin/op"}}, "debug": True},
    )
    detail = l6.PHASE.act(ctx)
    doc = json.loads(daemon_json.read_text())
    assert doc["runtimes"]["op-runtime"] == {"path": "/usr/bin/op"}
    assert doc["runtimes"][l6._RESERVED_RUNTIME_KEY] == l6._EXPECTED_RUNTIME
    assert doc["debug"] is True
    assert "merged" in detail
    assert any("restart docker" in r for r in restarts)


def test_act_fresh_file_when_absent(
    daemon_json: Path, ctx: SetupContext, restarts: list[str]
) -> None:
    l6.PHASE.act(ctx)
    doc = json.loads(daemon_json.read_text())
    assert doc["runtimes"][l6._RESERVED_RUNTIME_KEY] == l6._EXPECTED_RUNTIME


def test_act_no_restart_when_byte_identical(
    daemon_json: Path, ctx: SetupContext, restarts: list[str]
) -> None:
    canonical = (
        json.dumps(
            {"runtimes": {l6._RESERVED_RUNTIME_KEY: l6._EXPECTED_RUNTIME}},
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    daemon_json.parent.mkdir(parents=True, exist_ok=True)
    daemon_json.write_text(canonical, encoding="utf-8")
    detail = l6.PHASE.act(ctx)
    assert "no restart" in detail
    assert restarts == []


def test_act_replaces_non_dict_runtimes(
    daemon_json: Path, ctx: SetupContext, restarts: list[str]
) -> None:
    _write(daemon_json, {"runtimes": "not-a-dict"})
    l6.PHASE.act(ctx)
    doc = json.loads(daemon_json.read_text())
    assert doc["runtimes"][l6._RESERVED_RUNTIME_KEY] == l6._EXPECTED_RUNTIME


def test_act_handles_empty_file(
    daemon_json: Path, ctx: SetupContext, restarts: list[str]
) -> None:
    daemon_json.parent.mkdir(parents=True, exist_ok=True)
    daemon_json.write_text("   \n", encoding="utf-8")
    l6.PHASE.act(ctx)
    doc = json.loads(daemon_json.read_text())
    assert doc["runtimes"][l6._RESERVED_RUNTIME_KEY] == l6._EXPECTED_RUNTIME


def test_reverify_true_after_act(
    daemon_json: Path, ctx: SetupContext, restarts: list[str]
) -> None:
    l6.PHASE.act(ctx)
    assert l6.PHASE.reverify(ctx) is True


def test_reverify_false_when_absent(
    daemon_json: Path, ctx: SetupContext
) -> None:
    assert l6.PHASE.reverify(ctx) is False


def test_reverify_false_when_value_wrong(
    daemon_json: Path, ctx: SetupContext
) -> None:
    _write(daemon_json, {"runtimes": {l6._RESERVED_RUNTIME_KEY: {"path": "/x"}}})
    assert l6.PHASE.reverify(ctx) is False


def test_daemon_json_path_uses_pwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ctx: SetupContext
) -> None:
    import pwd

    class _PW:
        pw_dir = str(tmp_path / "sbhome")

    monkeypatch.setattr(pwd, "getpwnam", lambda _u: _PW())
    resolved = l6._daemon_json_path(ctx.host_config)
    assert resolved == tmp_path / "sbhome" / ".config" / "docker" / "daemon.json"


def test_content_aware(
    daemon_json: Path,
    ctx: SetupContext,
    restarts: list[str],
    assert_phase_content_aware: Callable[
        [Phase, SetupContext, Callable[[], None]], None
    ],
) -> None:
    _write(
        daemon_json,
        {"runtimes": {l6._RESERVED_RUNTIME_KEY: l6._EXPECTED_RUNTIME}},
    )

    def make_stale() -> None:
        # A wheel upgrade changed the expected runtimeArgs; the on-disk value
        # is now stale content. Probe must flip to DRIFT, not stay correct.
        _write(
            daemon_json,
            {"runtimes": {l6._RESERVED_RUNTIME_KEY: {"path": "/old", "runtimeArgs": []}}},
        )

    assert_phase_content_aware(l6.PHASE, ctx, make_stale)


def test_phase_shape() -> None:
    assert l6.PHASE.id == "l6"
    assert l6.PHASE.depends_on == ("l5",)
    assert l6.PHASE.identity == Identity.ROOT
