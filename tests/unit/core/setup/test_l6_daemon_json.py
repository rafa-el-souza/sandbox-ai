"""Unit tests for the L6 daemon.json reserved-key phase.

Covers: probe branches (MISSING file-absent, MISSING key-absent, DRIFT
differing-value, DRIFT file-correct-but-runtime-not-loaded, ALREADY_CORRECT
deep-equal-and-loaded), act create + merge (preserving operator runtimes) +
always-restart (StartLimit-safe, F-023), the corrupt-file refusal, reverify
true/false (incl. runtime-not-loaded), the runtime-aware ``_runtime_registered``
helper, the content-aware fixture, and the PHASE shape. ``pwd`` + ``Executor.run``
are faked — no real user / docker. The runtime-aware probe/reverify cross into
docker via ``_runtime_registered``; tests that assert ALREADY_CORRECT /
reverify-true mock it (the ``registered`` fixture) so they need no real docker.
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
        inner = cmd[-1]
        seen.append(inner)
        # The readiness-poll crossing reports the runtime LOADED (its inner
        # echoes the loaded marker on success); the is-active gate and the
        # restart crossing carry no marker. Returning a delivered result (no
        # raise) keeps act tests off the lost-sentinel retry loop.
        stdout = (
            l6._RUNTIME_LOADED_MARKER
            if l6._RUNTIME_LOADED_MARKER in inner
            else ""
        )
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    monkeypatch.setattr("core.executor.Executor.run", fake_run)
    return seen


@pytest.fixture
def registered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the runtime-aware check to report the runsc runtime as loaded.

    Probe/reverify cross into docker via ``_runtime_registered`` (F-023). Tests
    asserting ALREADY_CORRECT / reverify-true mock it True so they don't need a
    real docker; the helper itself is exercised directly in TestRuntimeRegistered.
    """
    monkeypatch.setattr(l6, "_runtime_registered", lambda _hc: True)


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


def test_probe_already_correct(
    daemon_json: Path, registered: None, ctx: SetupContext
) -> None:
    """File deep-equal AND docker has loaded the runtime → ALREADY_CORRECT."""
    _write(daemon_json, {"runtimes": {l6._RESERVED_RUNTIME_KEY: l6._EXPECTED_RUNTIME}})
    result, detail = l6.PHASE.probe(ctx)
    assert result == PhaseResult.ALREADY_CORRECT
    assert "loaded by docker" in detail


def test_probe_drift_when_file_correct_but_runtime_not_loaded(
    daemon_json: Path, ctx: SetupContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-023: file carries the key but docker has not loaded it → DRIFT (restart)."""
    _write(daemon_json, {"runtimes": {l6._RESERVED_RUNTIME_KEY: l6._EXPECTED_RUNTIME}})
    monkeypatch.setattr(l6, "_runtime_registered", lambda _hc: False)
    result, detail = l6.PHASE.probe(ctx)
    assert result == PhaseResult.DRIFT
    assert "has not loaded it" in detail


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
    assert "ensured" in detail
    assert any("restart --no-block docker" in r for r in restarts)


def test_act_settle_gate_runs_before_restart_crossing(
    daemon_json: Path, ctx: SetupContext, restarts: list[str]
) -> None:
    """The user-manager settle gate must precede the restart crossing (E2a).

    Round-5 fedora: L6's first crossing landed while L5's dockerd
    enable/restart was still churning ``user@<uid>.service`` → empty stdout →
    sentinel-not-found. The root-side ``systemctl is-active user@<uid>.service``
    gate must run BEFORE the ``systemctl --user restart docker`` crossing, which
    must in turn precede the runtime-readiness poll.
    """
    _write(daemon_json, {})
    l6.PHASE.act(ctx)
    gate_idx = next(
        i for i, r in enumerate(restarts) if "is-active user@4242.service" in r
    )
    restart_idx = next(
        i for i, r in enumerate(restarts) if "restart --no-block docker" in r
    )
    poll_idx = next(
        i for i, r in enumerate(restarts) if l6._RUNTIME_LOADED_MARKER in r
    )
    assert gate_idx < restart_idx < poll_idx


def test_act_fresh_file_when_absent(
    daemon_json: Path, ctx: SetupContext, restarts: list[str]
) -> None:
    l6.PHASE.act(ctx)
    doc = json.loads(daemon_json.read_text())
    assert doc["runtimes"][l6._RESERVED_RUNTIME_KEY] == l6._EXPECTED_RUNTIME


def test_act_always_restarts_even_when_byte_identical(
    daemon_json: Path, ctx: SetupContext, restarts: list[str]
) -> None:
    """F-023: act ALWAYS restarts — no byte-identical short-circuit.

    The probe screens out the already-loaded case (ALREADY_CORRECT → act not
    called). When act DOES run, a restart is always warranted — including the
    file-correct-but-runtime-not-loaded DRIFT — so a byte-identical file must
    still restart (StartLimit-safe: reset-failed precedes the restart).
    """
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
    l6.PHASE.act(ctx)
    assert any("restart --no-block docker" in r for r in restarts)
    assert any("reset-failed docker.service" in r for r in restarts)


def test_act_raises_when_runtime_never_loads(
    daemon_json: Path, ctx: SetupContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Poll delivers but docker never loaded the runtime → act raises (F-023).

    The readiness-poll crossing ran its full loop and emitted the ABSENT marker
    (``docker info`` never listed the reserved runtime). That is a genuine
    convergence failure — distinct from a lost sentinel (which raises inside the
    crossing and is retried) — so act must surface a diagnostic naming the
    unloaded runtime, never silently report success.
    """
    _write(daemon_json, {})

    def fake_run(
        _self: object, cmd: list[str], *_a: object, **_kw: object
    ) -> subprocess.CompletedProcess[str]:
        inner = cmd[-1]
        # is-active gate + restart deliver empty; the poll delivers the ABSENT
        # marker. Every crossing delivers (no lost-sentinel raise).
        stdout = (
            l6._RUNTIME_ABSENT_MARKER
            if l6._RUNTIME_ABSENT_MARKER in inner
            else ""
        )
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    monkeypatch.setattr("core.executor.Executor.run", fake_run)
    with pytest.raises(SandboxExecutionError, match="was not loaded within"):
        l6.PHASE.act(ctx)


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
    daemon_json: Path, registered: None, ctx: SetupContext, restarts: list[str]
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


def test_reverify_false_when_runtime_not_loaded(
    daemon_json: Path, ctx: SetupContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-023: file deep-equal but docker has not loaded the runtime → False."""
    _write(daemon_json, {"runtimes": {l6._RESERVED_RUNTIME_KEY: l6._EXPECTED_RUNTIME}})
    monkeypatch.setattr(l6, "_runtime_registered", lambda _hc: False)
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
    registered: None,
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


class TestRuntimeRegistered:
    """The runtime-aware ``_runtime_registered`` helper (F-023)."""

    def test_true_when_docker_info_lists_the_runtime(
        self, ctx: SetupContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(
            _self: object, cmd: list[str], *_a: object, **_kw: object
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                cmd, 0, '{"runc":{},"sandbox-ai-runsc":{"path":"/x"}}', ""
            )

        monkeypatch.setattr("core.executor.Executor.run", fake_run)
        assert l6._runtime_registered(ctx.host_config) is True

    def test_false_when_docker_info_omits_the_runtime(
        self, ctx: SetupContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(
            _self: object, cmd: list[str], *_a: object, **_kw: object
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 0, '{"runc":{}}', "")

        monkeypatch.setattr("core.executor.Executor.run", fake_run)
        assert l6._runtime_registered(ctx.host_config) is False

    def test_false_when_crossing_raises(
        self, ctx: SetupContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """docker down / sentinel absent → SandboxExecutionError → not-registered."""

        def fake_run(
            _self: object, cmd: list[str], *_a: object, **_kw: object
        ) -> subprocess.CompletedProcess[str]:
            raise SandboxExecutionError("docker daemon unreachable")

        monkeypatch.setattr("core.executor.Executor.run", fake_run)
        assert l6._runtime_registered(ctx.host_config) is False


def test_phase_shape() -> None:
    assert l6.PHASE.id == "l6"
    assert l6.PHASE.depends_on == ("l5",)
    assert l6.PHASE.identity == Identity.ROOT
