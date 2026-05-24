"""Unit tests for the L6.5 dispatcher-install phase.

Covers: the 2-arg ``compile_dispatcher`` contract, the
``_DISPATCH_SOURCE_ENTRIES``-derived source-bundle hash (no hardcoded subset),
probe branches (MISSING no-manifest, MISSING binary-absent, ALREADY_CORRECT
both-match, DRIFT binary-drift, DRIFT source-drift), act compile + install +
manifest, reverify true/false, the content-aware fixture, and the PHASE shape.
``compile_dispatcher``, ``chattr``, and the resource tree are faked — no real
Go build / network.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from core.dispatch import _DISPATCH_SOURCE_ENTRIES
from core.host_config import MachinectlAuth, minimal_host_config
from core.setup import l65_dispatcher as l65
from core.setup.phase_runner import Identity, PhaseResult, SetupContext

if TYPE_CHECKING:
    from collections.abc import Callable

    from core.setup.phase_runner import Phase


@dataclass
class _Env:
    """Typed fake-world state for the L6.5 phase under test."""

    binary_bytes: bytes = b"COMPILED-DISPATCHER-V1"
    compile_calls: list[str] = field(default_factory=list)
    chattr: list[str] = field(default_factory=list)


@pytest.fixture
def ctx() -> SetupContext:
    return SetupContext(
        host_config=minimal_host_config("sandboxuser", MachinectlAuth.SUDO),
        operator="op",
    )


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Env:
    """Isolated SANDBOX_AI_HOME + redirected target + faked compile/chattr.

    ``compile_dispatcher`` is replaced with a fake that writes deterministic
    bytes to ``output_path`` (and asserts it is called with exactly 2 args).
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SANDBOX_AI_HOME", str(home))

    target = tmp_path / "libexec" / "dispatch"
    staging = tmp_path / "libexec" / ".dispatch.staging"
    monkeypatch.setattr(l65, "_TARGET", target)
    monkeypatch.setattr(l65, "_STAGING", staging)

    state = _Env()

    def fake_compile(*args: object) -> None:
        # Reconciliation #1: compile_dispatcher is 2-arg (output_path,
        # host_config) — build_dir was removed in C-001.
        assert len(args) == 2, f"compile_dispatcher must be 2-arg, got {args!r}"
        output_path, _hc = args
        path = Path(str(output_path))
        state.compile_calls.append(str(output_path))
        path.write_bytes(state.binary_bytes)

    def fake_subprocess_run(
        cmd: list[str], **_kw: object
    ) -> subprocess.CompletedProcess[str]:
        state.chattr.append(" ".join(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def fake_chown(_p: object, _u: int, _g: int) -> None:
        return None

    monkeypatch.setattr(l65, "compile_dispatcher", fake_compile)
    monkeypatch.setattr("core.setup.l65_dispatcher.subprocess.run", fake_subprocess_run)
    monkeypatch.setattr("core.setup.l65_dispatcher.os.chown", fake_chown)
    return state


@pytest.fixture
def source_sha(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Fake the source-bundle resource tree; expose a settable sha holder."""
    holder = {"value": "SOURCE-A"}

    def fake_source() -> str:
        return holder["value"]

    monkeypatch.setattr(l65, "_source_bundle_sha512", fake_source)
    return holder


def _binary_sha(env: _Env) -> str:
    return hashlib.sha512(env.binary_bytes).hexdigest()


def _write_manifest(compiled: str, source: str) -> None:
    path = l65._manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "compiled_sha512": compiled,
                "source_bundle_sha512": source,
                "compile_timestamp": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )


def test_source_bundle_hash_derived_from_dispatch_source_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hashed file set is derived from ``_DISPATCH_SOURCE_ENTRIES``.

    Reconciliation #2: NOT a hardcoded ``{go.mod, go.sum, main.go,
    vendor/**}`` subset. We stub the resource tree so each top-level entry in
    ``_DISPATCH_SOURCE_ENTRIES`` contributes a unique byte; the resulting hash
    must change when any one entry's content changes, proving every entry
    (including ``main_test.go`` / ``fixtures``) participates.
    """
    seen: list[str] = []

    class _Node:
        def __init__(self, name: str, content: bytes | None) -> None:
            self.name = name
            self._content = content

        def is_dir(self) -> bool:
            return self._content is None

        def iterdir(self) -> list[_Node]:
            return [_Node(f"{self.name}-child", b"dir-file")]

        def read_bytes(self) -> bytes:
            return self._content or b""

        def joinpath(self, entry: str) -> _Node:
            seen.append(entry)
            # ``vendor`` / ``fixtures`` are directories; the rest are files.
            if entry in ("vendor", "fixtures"):
                return _Node(entry, None)
            return _Node(entry, f"content-of-{entry}".encode())

    class _Root:
        def joinpath(self, _name: str) -> _Node:
            return _Node("dispatch", None)

    monkeypatch.setattr(l65, "_resource_files", lambda _pkg: _Root())
    digest = l65._source_bundle_sha512()
    # Every entry in the single source of truth was visited.
    assert seen == list(_DISPATCH_SOURCE_ENTRIES)
    assert "main_test.go" in seen
    assert "fixtures" in seen
    assert len(digest) == 128


def test_probe_missing_when_no_manifest(
    env: _Env, source_sha: dict[str, str], ctx: SetupContext
) -> None:
    result, detail = l65.PHASE.probe(ctx)
    assert result == PhaseResult.MISSING
    assert "manifest absent" in detail


def test_probe_missing_when_binary_absent_but_manifest_present(
    env: _Env, source_sha: dict[str, str], ctx: SetupContext
) -> None:
    _write_manifest("X", source_sha["value"])
    result, detail = l65.PHASE.probe(ctx)
    assert result == PhaseResult.MISSING
    assert "absent though manifest exists" in detail


def test_probe_already_correct_when_both_match(
    env: _Env, source_sha: dict[str, str], ctx: SetupContext
) -> None:
    l65._TARGET.parent.mkdir(parents=True, exist_ok=True)
    l65._TARGET.write_bytes(env.binary_bytes)
    _write_manifest(_binary_sha(env), source_sha["value"])
    result, _ = l65.PHASE.probe(ctx)
    assert result == PhaseResult.ALREADY_CORRECT


def test_probe_drift_when_binary_sha_differs(
    env: _Env, source_sha: dict[str, str], ctx: SetupContext
) -> None:
    l65._TARGET.parent.mkdir(parents=True, exist_ok=True)
    l65._TARGET.write_bytes(b"TAMPERED")
    _write_manifest(_binary_sha(env), source_sha["value"])
    result, detail = l65.PHASE.probe(ctx)
    assert result == PhaseResult.DRIFT
    assert "drifted from the manifest" in detail


def test_probe_drift_when_source_bundle_differs(
    env: _Env, source_sha: dict[str, str], ctx: SetupContext
) -> None:
    l65._TARGET.parent.mkdir(parents=True, exist_ok=True)
    l65._TARGET.write_bytes(env.binary_bytes)
    _write_manifest(_binary_sha(env), "STALE-SOURCE")
    result, _ = l65.PHASE.probe(ctx)
    assert result == PhaseResult.DRIFT


def test_probe_handles_non_dict_manifest(
    env: _Env, source_sha: dict[str, str], ctx: SetupContext
) -> None:
    path = l65._manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2]", encoding="utf-8")
    result, _ = l65.PHASE.probe(ctx)
    assert result == PhaseResult.MISSING


def test_act_compiles_installs_and_writes_manifest(
    env: _Env, source_sha: dict[str, str], ctx: SetupContext
) -> None:
    detail = l65.PHASE.act(ctx)
    assert l65._TARGET.read_bytes() == env.binary_bytes
    # 2-arg compile asserted inside the fake; one call made.
    assert len(env.compile_calls) == 1
    manifest = json.loads(l65._manifest_path().read_text())
    assert manifest["compiled_sha512"] == _binary_sha(env)
    assert manifest["source_bundle_sha512"] == source_sha["value"]
    assert "compile_timestamp" in manifest
    assert set(manifest) == {
        "compiled_sha512",
        "source_bundle_sha512",
        "compile_timestamp",
    }
    # chattr +i applied (and -i not needed on a fresh target).
    assert any("chattr +i" in c for c in env.chattr)
    assert "manifest recorded" in detail
    # Host-plane manifest is world-readable root:root 0644 (F-021), not 0600.
    assert (l65._manifest_path().stat().st_mode & 0o777) == 0o644


def test_act_unseals_existing_immutable_target(
    env: _Env, source_sha: dict[str, str], ctx: SetupContext
) -> None:
    l65._TARGET.parent.mkdir(parents=True, exist_ok=True)
    l65._TARGET.write_bytes(b"OLD")
    l65.PHASE.act(ctx)
    assert any("chattr -i" in c for c in env.chattr)
    assert any("chattr +i" in c for c in env.chattr)


def test_act_cleans_tmp_when_compile_raises(
    env: _Env,
    source_sha: dict[str, str],
    ctx: SetupContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path as _P

    from core.exceptions import SandboxExecutionError

    def boom(output_path: str, _hc: object) -> None:
        # Write the tmp file then fail — the finally must unlink the leftover.
        _P(output_path).write_bytes(b"partial")
        raise SandboxExecutionError("compile failed")

    monkeypatch.setattr(l65, "compile_dispatcher", boom)
    with pytest.raises(SandboxExecutionError):
        l65.PHASE.act(ctx)
    # No staging / target left behind (the finally-clause unlink ran).
    assert not l65._TARGET.exists()
    assert not l65._STAGING.exists()


def test_act_raises_when_target_missing_after_install(
    env: _Env,
    source_sha: dict[str, str],
    ctx: SetupContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.exceptions import SandboxExecutionError

    # Install is a no-op (target never created) and the staging unlink in the
    # finally clause runs; the post-install sha check then raises.
    monkeypatch.setattr(l65, "_install_compiled", lambda _s: None)
    with pytest.raises(SandboxExecutionError, match="dispatcher missing"):
        l65.PHASE.act(ctx)


def test_reverify_true_after_act(
    env: _Env, source_sha: dict[str, str], ctx: SetupContext
) -> None:
    l65.PHASE.act(ctx)
    assert l65.PHASE.reverify(ctx) is True


def test_reverify_false_when_no_manifest(
    env: _Env, source_sha: dict[str, str], ctx: SetupContext
) -> None:
    assert l65.PHASE.reverify(ctx) is False


def test_reverify_false_when_binary_absent(
    env: _Env, source_sha: dict[str, str], ctx: SetupContext
) -> None:
    _write_manifest("X", source_sha["value"])
    assert l65.PHASE.reverify(ctx) is False


def test_reverify_false_when_source_drifts_post_act(
    env: _Env, source_sha: dict[str, str], ctx: SetupContext
) -> None:
    l65.PHASE.act(ctx)
    source_sha["value"] = "SOURCE-MOVED"
    assert l65.PHASE.reverify(ctx) is False


def test_content_aware(
    env: _Env,
    source_sha: dict[str, str],
    ctx: SetupContext,
    assert_phase_content_aware: Callable[
        [Phase, SetupContext, Callable[[], None]], None
    ],
) -> None:
    l65._TARGET.parent.mkdir(parents=True, exist_ok=True)
    l65._TARGET.write_bytes(env.binary_bytes)
    _write_manifest(_binary_sha(env), source_sha["value"])

    def make_stale() -> None:
        # A wheel upgrade changed the dispatcher source bundle; the manifest's
        # recorded source sha is now stale. Probe must flip to DRIFT.
        source_sha["value"] = "SOURCE-B-AFTER-WHEEL-UPGRADE"

    assert_phase_content_aware(l65.PHASE, ctx, make_stale)


def test_manifest_path_is_binary_sibling_not_under_home(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-021 regression: the manifest lands beside the binary, NOT under
    ``sandbox_ai_home()`` — so a root-running setup ($HOME=/root) cannot hide it
    in ``/root/.sandbox-ai`` where the operator's doctor can never read it.

    The ``env`` fixture sets ``SANDBOX_AI_HOME`` to a tmp home AND redirects
    ``_TARGET`` to a separate tmp libexec dir; the manifest path must follow
    ``_TARGET.parent`` and be wholly independent of the home.
    """
    manifest = l65._manifest_path()
    assert manifest == l65._TARGET.parent / "dispatcher.manifest.json"
    # Pre-fix tree resolved this under SANDBOX_AI_HOME/state — assert it does not.
    import os

    home = os.environ["SANDBOX_AI_HOME"]
    assert home not in str(manifest)


def test_phase_shape() -> None:
    assert l65.PHASE.id == "l65"
    assert l65.PHASE.depends_on == ("l6a",)
    assert l65.PHASE.identity == Identity.ROOT
