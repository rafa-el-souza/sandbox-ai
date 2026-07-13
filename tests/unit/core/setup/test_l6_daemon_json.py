# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
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
from core.host_config import (
    DockerExecutionMode,
    minimal_host_config,
)
from core.setup import l6_daemon_json as l6
from core.setup.phase_runner import Identity, PhaseResult, SetupContext

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from core.setup.phase_runner import Phase


@pytest.fixture
def ctx() -> SetupContext:
    return SetupContext(
        host_config=minimal_host_config(
            "sandboxuser", DockerExecutionMode.SEPARATE_USER
        ),
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
        # Every crossing "delivers" exit 0 — the readiness poll's ``exit 0``
        # (runtime loaded) path. The sentinel-wrap recovery is mocked out here
        # (Executor.run is replaced), so a returncode-0 result is success.
        return subprocess.CompletedProcess(cmd, 0, "", "")

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
    _write(daemon_json, {"runtimes": {l6.RESERVED_RUNTIME_KEY: l6.EXPECTED_RUNTIME}})
    result, detail = l6.PHASE.probe(ctx)
    assert result == PhaseResult.ALREADY_CORRECT
    assert "loaded by docker" in detail


def test_probe_drift_when_file_correct_but_runtime_not_loaded(
    daemon_json: Path, ctx: SetupContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-023: file carries the key but docker has not loaded it → DRIFT (restart)."""
    _write(daemon_json, {"runtimes": {l6.RESERVED_RUNTIME_KEY: l6.EXPECTED_RUNTIME}})
    monkeypatch.setattr(l6, "_runtime_registered", lambda _hc: False)
    result, detail = l6.PHASE.probe(ctx)
    assert result == PhaseResult.DRIFT
    assert "has not loaded it" in detail


def test_probe_drift_when_value_differs(
    daemon_json: Path, ctx: SetupContext
) -> None:
    _write(
        daemon_json,
        {"runtimes": {l6.RESERVED_RUNTIME_KEY: {"path": "/old/runsc"}}},
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
    assert doc["runtimes"][l6.RESERVED_RUNTIME_KEY] == l6.EXPECTED_RUNTIME
    assert doc["debug"] is True
    assert "ensured" in detail
    assert any("restart --no-block docker" in r for r in restarts)


def test_act_separate_user_restart_crosses_via_machinectl_sentinel(
    daemon_json: Path, ctx: SetupContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C-009 §3.7 guard: the L6 ROOT crossing stays machinectl + ``sentinel=True``.

    L6 runs as root BEFORE the operator sudoers rule exists, crossing into the
    sandbox user via ``daemon_owner_crossing`` (``machinectl_cmd`` in
    separate-user) with the orchestrator-injected sentinel ON. It neither uses
    nor depends on the operator pipe ``Cmnd_Spec`` (L3a/L8's ``framed=True``
    pipe path), so it MUST NOT be touched.
    """
    _write(daemon_json, {})
    seen: list[tuple[list[str], object]] = []

    def fake_run(
        _self: object, cmd: list[str], *_a: object, **kw: object
    ) -> subprocess.CompletedProcess[str]:
        seen.append((cmd, kw.get("sentinel")))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("core.executor.Executor.run", fake_run)

    l6.PHASE.act(ctx)

    restart = [
        (c, s) for c, s in seen if "restart --no-block docker" in c[-1]
    ]
    assert len(restart) == 1
    cmd, sentinel = restart[0]
    assert "machinectl" in cmd
    assert sentinel is True
    assert "systemd-run" not in cmd


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
        i for i, r in enumerate(restarts) if "docker info" in r and "exit 0" in r
    )
    assert gate_idx < restart_idx < poll_idx


def test_act_fresh_file_when_absent(
    daemon_json: Path, ctx: SetupContext, restarts: list[str]
) -> None:
    l6.PHASE.act(ctx)
    doc = json.loads(daemon_json.read_text())
    assert doc["runtimes"][l6.RESERVED_RUNTIME_KEY] == l6.EXPECTED_RUNTIME


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
            {"runtimes": {l6.RESERVED_RUNTIME_KEY: l6.EXPECTED_RUNTIME}},
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
    """Poll exhausts without the runtime loading → act raises (F-023).

    When ``docker info`` never lists the reserved runtime, the poll loop falls
    through to ``exit 1``; the sentinel crossing recovers that non-zero inner
    exit as a :class:`SandboxExecutionError`, which propagates out of act as the
    phase FAIL (the executor's subshell wrap is what lets the inner ``exit``
    reach recovery at all — the F-023 root cause). Here Executor.run is mocked,
    so the poll crossing is made to raise exactly as the real recovery would.
    """
    _write(daemon_json, {})

    def fake_run(
        _self: object, cmd: list[str], *_a: object, **_kw: object
    ) -> subprocess.CompletedProcess[str]:
        inner = cmd[-1]
        # The poll crossing (the docker-info loop) hits its ``exit 1`` path →
        # the real executor would raise on the recovered non-zero exit; the
        # is-active gate and the restart crossing deliver exit 0.
        if "docker info" in inner:
            raise SandboxExecutionError(
                "[FATAL] Sandbox Execution Fault: Inner command failed with exit status 1."
            )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("core.executor.Executor.run", fake_run)
    with pytest.raises(SandboxExecutionError, match="exit status 1"):
        l6.PHASE.act(ctx)


def test_act_replaces_non_dict_runtimes(
    daemon_json: Path, ctx: SetupContext, restarts: list[str]
) -> None:
    _write(daemon_json, {"runtimes": "not-a-dict"})
    l6.PHASE.act(ctx)
    doc = json.loads(daemon_json.read_text())
    assert doc["runtimes"][l6.RESERVED_RUNTIME_KEY] == l6.EXPECTED_RUNTIME


def test_act_handles_empty_file(
    daemon_json: Path, ctx: SetupContext, restarts: list[str]
) -> None:
    daemon_json.parent.mkdir(parents=True, exist_ok=True)
    daemon_json.write_text("   \n", encoding="utf-8")
    l6.PHASE.act(ctx)
    doc = json.loads(daemon_json.read_text())
    assert doc["runtimes"][l6.RESERVED_RUNTIME_KEY] == l6.EXPECTED_RUNTIME


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
    _write(daemon_json, {"runtimes": {l6.RESERVED_RUNTIME_KEY: {"path": "/x"}}})
    assert l6.PHASE.reverify(ctx) is False


def test_reverify_false_when_runtime_not_loaded(
    daemon_json: Path, ctx: SetupContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-023: file deep-equal but docker has not loaded the runtime → False."""
    _write(daemon_json, {"runtimes": {l6.RESERVED_RUNTIME_KEY: l6.EXPECTED_RUNTIME}})
    monkeypatch.setattr(l6, "_runtime_registered", lambda _hc: False)
    assert l6.PHASE.reverify(ctx) is False


def test_daemon_json_path_uses_pwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ctx: SetupContext
) -> None:
    import pwd

    class _PW:
        pw_dir = str(tmp_path / "sbhome")

    monkeypatch.setattr(pwd, "getpwnam", lambda _u: _PW())
    resolved = l6._daemon_json_path(ctx)
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
        {"runtimes": {l6.RESERVED_RUNTIME_KEY: l6.EXPECTED_RUNTIME}},
    )

    def make_stale() -> None:
        # A wheel upgrade changed the expected runtimeArgs; the on-disk value
        # is now stale content. Probe must flip to DRIFT, not stay correct.
        _write(
            daemon_json,
            {"runtimes": {l6.RESERVED_RUNTIME_KEY: {"path": "/old", "runtimeArgs": []}}},
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
        assert l6._runtime_registered(ctx) is True

    def test_false_when_docker_info_omits_the_runtime(
        self, ctx: SetupContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(
            _self: object, cmd: list[str], *_a: object, **_kw: object
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 0, '{"runc":{}}', "")

        monkeypatch.setattr("core.executor.Executor.run", fake_run)
        assert l6._runtime_registered(ctx) is False

    def test_false_when_crossing_raises(
        self, ctx: SetupContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """docker down / sentinel absent → SandboxExecutionError → not-registered."""

        def fake_run(
            _self: object, cmd: list[str], *_a: object, **_kw: object
        ) -> subprocess.CompletedProcess[str]:
            raise SandboxExecutionError("docker daemon unreachable")

        monkeypatch.setattr("core.executor.Executor.run", fake_run)
        assert l6._runtime_registered(ctx) is False


# ── operator-rootless: LOCAL crossing, operator-home daemon.json (§6.3) ───────


def _oprootless_ctx() -> SetupContext:
    return SetupContext(
        host_config=minimal_host_config(
            "sandboxuser",
            mode=DockerExecutionMode.OPERATOR_ROOTLESS,
        ),
        operator="alice",
    )


def test_act_operator_rootless_local_writes_operator_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """op-rootless act writes the OPERATOR's daemon.json and restarts LOCAL.

    The daemon owner is the invoking operator, so the file lives under the
    operator's home; the restart + readiness poll cross with an empty LOCAL
    prefix (no ``machinectl``), no ``user@<uid>.service`` readiness gate (the
    operator's session is already live), and sentinel off.
    """
    home = tmp_path / "alice"
    monkeypatch.setattr("pwd.getpwnam", lambda _u: _fake_pw(str(home)))
    seen: list[tuple[list[str], object]] = []

    def fake_run(
        _self: object, cmd: list[str], *_a: object, **kw: object
    ) -> subprocess.CompletedProcess[str]:
        seen.append((cmd, kw.get("sentinel")))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("core.executor.Executor.run", fake_run)

    l6.PHASE.act(_oprootless_ctx())

    daemon_json = home / ".config" / "docker" / "daemon.json"
    doc = json.loads(daemon_json.read_text())
    assert doc["runtimes"][l6.RESERVED_RUNTIME_KEY] == l6.EXPECTED_RUNTIME
    joined = [" ".join(c) for c, _ in seen]
    assert any("restart --no-block docker" in j for j in joined)
    assert all("machinectl" not in j for j in joined)
    assert all("is-active user@" not in j for j in joined)
    # LOCAL env-injected prefix (finding 8.11) — restart/poll carry the session env.
    assert all(c[0] == "env" for c, _ in seen)
    assert all(
        any(e.startswith("HOME=") for e in c) and any(e.startswith("DOCKER_HOST=") for e in c)
        for c, _ in seen
    )
    assert all(s is False for _, s in seen)


def test_probe_operator_rootless_skips_sandbox_user_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """op-rootless probe skips the sandbox-user guard (141->148).

    The daemon owner in operator-rootless is the always-present invoking
    operator, so the `if not is_operator_rootless` guard body does not run.
    Prove it by making ``pwd.getpwnam`` raise: in separate-user that KeyError is
    the MISSING signal, but here the guard is never reached, so a correct file
    still reports ALREADY_CORRECT.
    """
    path = tmp_path / ".config" / "docker" / "daemon.json"

    def _boom(_u: str) -> object:
        raise KeyError("getpwnam(): name not found")

    monkeypatch.setattr("pwd.getpwnam", _boom)
    monkeypatch.setattr(l6, "_daemon_json_path", lambda _hc: path)
    monkeypatch.setattr(l6, "_runtime_registered", lambda _hc: True)
    _write(path, {"runtimes": {l6.RESERVED_RUNTIME_KEY: l6.EXPECTED_RUNTIME}})

    result, _ = l6.PHASE.probe(_oprootless_ctx())
    assert result == PhaseResult.ALREADY_CORRECT


def test_runtime_registered_operator_rootless_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """op-rootless ``_runtime_registered`` queries docker LOCAL with the injected
    session env (no machinectl, sentinel off) — finding 8.11."""
    monkeypatch.setattr("pwd.getpwnam", lambda _u: _fake_pw("/home/alice"))
    seen: list[tuple[list[str], object]] = []

    def fake_run(
        _self: object, cmd: list[str], *_a: object, **kw: object
    ) -> subprocess.CompletedProcess[str]:
        seen.append((cmd, kw.get("sentinel")))
        return subprocess.CompletedProcess(cmd, 0, '{"sandbox-ai-runsc":{"path":"/x"}}', "")

    monkeypatch.setattr("core.executor.Executor.run", fake_run)

    assert l6._runtime_registered(_oprootless_ctx()) is True
    (cmd, sentinel), = seen
    assert cmd[0] == "env"
    assert any(e.startswith("DOCKER_HOST=") for e in cmd)
    assert "machinectl" not in " ".join(cmd)
    assert sentinel is False


def test_phase_shape() -> None:
    assert l6.PHASE.id == "l6"
    assert l6.PHASE.depends_on == ("l5",)
    assert l6.PHASE.identity == Identity.ROOT


def test_expected_runtime_args_include_ignore_cgroups() -> None:
    """The L6 target carries --ignore-cgroups (F-057): rootless runsc cannot create
    its systemd cgroup scope on the user bus, so it must skip systemd cgroup setup
    or `sandbox start` fails at OCI task-create. Locks the runtimeArgs contract."""
    assert l6.EXPECTED_RUNTIME["path"] == "/usr/local/libexec/sandbox-ai/runsc"
    assert l6.EXPECTED_RUNTIME["runtimeArgs"] == ["--oci-seccomp", "--ignore-cgroups"]
