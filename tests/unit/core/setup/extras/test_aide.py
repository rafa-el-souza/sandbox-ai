"""Unit tests for the optional AIDE integration phase.

Covers: probe refusals (binary absent → CONFLICT, conf.d dir absent →
CONFLICT), probe MISSING / ALREADY_CORRECT / DRIFT, the content-aware
contract, the act (file write + 0644 + optional --config-check + the
``aide --init`` finalization-summary prompt when the DB is absent), reverify
true/false, and the PHASE shape. ``shutil.which`` + ``Executor`` + the
reserved-namespace paths are all faked — no real aide is touched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from core.exceptions import SandboxExecutionError
from core.host_config import DockerExecutionMode, MachinectlAuth, minimal_host_config
from core.setup.extras import aide
from core.setup.phase_runner import Identity, PhaseResult, SetupContext

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from core.setup.phase_runner import Phase


@pytest.fixture
def ctx() -> SetupContext:
    return SetupContext(
        host_config=minimal_host_config(
            "sandboxuser", MachinectlAuth.SUDO, DockerExecutionMode.SEPARATE_USER
        ),
        operator="op",
    )


@dataclass
class _Result:
    stdout: str


@dataclass
class _Exec:
    """Fake Executor: records argv, drives per-command outcomes."""

    calls: list[list[str]] = field(default_factory=list)
    help_supports_config_check: bool = True
    help_ok: bool = True
    config_check_ok: bool = True

    def run(self, cmd: list[str], **_kw: object) -> object:
        self.calls.append(cmd)
        if cmd == ["aide", "--help"]:
            if not self.help_ok:
                raise SandboxExecutionError("no help")
            return _Result(
                "--config-check  check config\n"
                if self.help_supports_config_check
                else "usage: aide [options]\n"
            )
        if cmd == ["aide", "--config-check"] and not self.config_check_ok:
            raise SandboxExecutionError("bad config")
        return _Result("")


@dataclass
class _World:
    db_path: str = ""
    which_aide: bool = True
    executor: _Exec = field(default_factory=_Exec)

    def create_db(self) -> None:
        """Materialize the AIDE baseline DB at the faked path."""
        with open(self.db_path, "wb") as fh:
            fh.write(b"AIDE-DB")


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _World:
    w = _World()

    conf_d = tmp_path / "aide.conf.d"
    conf_d.mkdir()
    dropin = conf_d / "sandbox-ai.conf"
    db = tmp_path / "aide.db"

    monkeypatch.setattr(aide, "_CONF_D_DIR", str(conf_d))
    monkeypatch.setattr(aide, "_CONF_DROPIN_PATH", str(dropin))
    monkeypatch.setattr(aide, "_AIDE_DB_PATH", str(db))

    def fake_which(name: str) -> str | None:
        if name == "aide":
            return "/usr/bin/aide" if w.which_aide else None
        return None

    monkeypatch.setattr("shutil.which", fake_which)
    monkeypatch.setattr(aide, "Executor", lambda: w.executor)
    monkeypatch.setattr("os.chown", lambda *_a, **_k: None)

    w.db_path = str(db)
    return w


def _write_existing(content: str) -> None:
    with open(aide._CONF_DROPIN_PATH, "w", encoding="utf-8") as fh:
        fh.write(content)


# ── Probe refusals ───────────────────────────────────────────────────────────


def test_probe_refuses_when_aide_absent(
    world: _World, ctx: SetupContext
) -> None:
    world.which_aide = False
    result, detail = aide.PHASE.probe(ctx)
    assert result == PhaseResult.CONFLICT
    assert "aide not installed" in detail


def test_probe_refuses_when_conf_d_missing(
    world: _World, ctx: SetupContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(aide, "_CONF_D_DIR", "/nonexistent/conf.d")
    result, detail = aide.PHASE.probe(ctx)
    assert result == PhaseResult.CONFLICT
    assert "does not support" in detail


# ── Probe state classification ───────────────────────────────────────────────


def test_probe_missing_when_dropin_absent(
    world: _World, ctx: SetupContext
) -> None:
    result, detail = aide.PHASE.probe(ctx)
    assert result == PhaseResult.MISSING
    assert "absent" in detail


def test_probe_already_correct_when_content_matches(
    world: _World, ctx: SetupContext
) -> None:
    _write_existing(aide._CONF_CONTENT)
    result, _ = aide.PHASE.probe(ctx)
    assert result == PhaseResult.ALREADY_CORRECT


def test_probe_drift_when_content_differs(
    world: _World, ctx: SetupContext
) -> None:
    _write_existing("# stale\n/old NORMAL\n")
    result, detail = aide.PHASE.probe(ctx)
    assert result == PhaseResult.DRIFT
    assert "differs" in detail


# ── Content-aware contract ───────────────────────────────────────────────────


def test_content_aware(
    world: _World,
    ctx: SetupContext,
    assert_phase_content_aware: Callable[
        [Phase, SetupContext, Callable[[], None]], None
    ],
) -> None:
    _write_existing(aide._CONF_CONTENT)

    def make_stale() -> None:
        # Operator (or a stale wheel) left a drop-in that no longer matches
        # the canonical managed-binary snippet.
        _write_existing("# sandbox-ai managed\n/usr/local/libexec/x NORMAL\n")

    assert_phase_content_aware(aide.PHASE, ctx, make_stale)


# ── Act ──────────────────────────────────────────────────────────────────────


def test_act_writes_canonical_content(
    world: _World, ctx: SetupContext
) -> None:
    detail = aide.PHASE.act(ctx)
    with open(aide._CONF_DROPIN_PATH, encoding="utf-8") as fh:
        written = fh.read()
    assert written == aide._CONF_CONTENT
    assert "/usr/local/libexec/sandbox-ai/dispatch NORMAL" in written
    assert "/usr/local/libexec/sandbox-ai/runsc NORMAL" in written
    assert written.startswith(
        "# sandbox-ai managed — do not edit; rerun 'sudo sandbox setup'"
    )
    assert "wrote" in detail


def test_act_runs_config_check_when_supported(
    world: _World, ctx: SetupContext
) -> None:
    detail = aide.PHASE.act(ctx)
    assert ["aide", "--config-check"] in world.executor.calls
    assert "validated via aide --config-check" in detail


def test_act_skips_config_check_when_unsupported(
    world: _World, ctx: SetupContext
) -> None:
    world.executor.help_supports_config_check = False
    detail = aide.PHASE.act(ctx)
    assert ["aide", "--config-check"] not in world.executor.calls
    assert "validated" not in detail


def test_act_skips_config_check_when_help_errors(
    world: _World, ctx: SetupContext
) -> None:
    world.executor.help_ok = False
    detail = aide.PHASE.act(ctx)
    assert ["aide", "--config-check"] not in world.executor.calls
    assert "validated" not in detail


def test_act_appends_db_init_prompt_when_db_absent(
    world: _World, ctx: SetupContext
) -> None:
    detail = aide.PHASE.act(ctx)
    assert "sudo aide --init" in detail
    assert "10+ minutes" in detail


def test_act_omits_db_init_prompt_when_db_present(
    world: _World, ctx: SetupContext
) -> None:
    world.create_db()
    detail = aide.PHASE.act(ctx)
    assert "aide --init" not in detail


# ── Reverify ─────────────────────────────────────────────────────────────────


def test_reverify_true_after_act(world: _World, ctx: SetupContext) -> None:
    aide.PHASE.act(ctx)
    assert aide.PHASE.reverify(ctx) is True


def test_reverify_false_when_dropin_absent(
    world: _World, ctx: SetupContext
) -> None:
    assert aide.PHASE.reverify(ctx) is False


def test_reverify_false_when_content_differs(
    world: _World, ctx: SetupContext
) -> None:
    _write_existing("# stale\n")
    assert aide.PHASE.reverify(ctx) is False


# ── PHASE shape ──────────────────────────────────────────────────────────────


def test_phase_shape() -> None:
    assert aide.PHASE.id == "aide"
    assert aide.PHASE.depends_on == ("l8",)
    assert aide.PHASE.identity == Identity.ROOT
    assert aide.PHASE.rollback is None
