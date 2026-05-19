"""Unit tests for the L4 operator-state phase.

Covers: the content-aware fixture (correct→stale), every probe branch
(MISSING absent-toml, MISSING dirs-absent, DRIFT missing-key, ALREADY_CORRECT,
CONFLICT invalid-value), act seed + merge (preserving operator hand-edits),
reverify success + the two reverify-false branches, and the exported PHASE
shape.
"""

from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING, Any

import pytest
from core.host_config import MachinectlAuth, minimal_host_config
from core.setup import l4_operator_state as l4
from core.setup.phase_runner import Identity, PhaseResult, SetupContext

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from core.setup.phase_runner import Phase


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``sandbox_ai_home()`` to an isolated tmp tree."""
    monkeypatch.setenv("SANDBOX_AI_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def ctx() -> SetupContext:
    return SetupContext(
        host_config=minimal_host_config("sandboxuser", MachinectlAuth.SUDO),
        operator="op",
    )


def _write_toml(home: Path, body: str) -> None:
    cfg = home / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "sandbox-ai.toml").write_text(body, encoding="utf-8")


def _make_dirs(home: Path) -> None:
    for sub in l4._STATE_SUBDIRS:
        (home / sub).mkdir(parents=True, exist_ok=True)


def _load(home: Path) -> dict[str, Any]:
    with (home / "config" / "sandbox-ai.toml").open("rb") as fh:
        return tomllib.load(fh)


_VALID_BODY = (
    '[host]\n'
    'docker_unprivileged_user = "sandbox"\n'
    'machinectl_authentication = "sudo"\n'
    'workspace_bridge_group = "sb-ws"\n'
)


def test_probe_missing_when_toml_absent(home: Path, ctx: SetupContext) -> None:
    result, detail = l4.PHASE.probe(ctx)
    assert result == PhaseResult.MISSING
    assert "absent" in detail


def test_probe_missing_when_dirs_absent_but_toml_complete(
    home: Path, ctx: SetupContext
) -> None:
    _write_toml(home, _VALID_BODY)
    result, detail = l4.PHASE.probe(ctx)
    assert result == PhaseResult.MISSING
    assert "dirs absent" in detail


def test_probe_drift_when_required_key_missing(
    home: Path, ctx: SetupContext
) -> None:
    _make_dirs(home)
    _write_toml(home, '[host]\ndocker_unprivileged_user = "sandbox"\n')
    result, detail = l4.PHASE.probe(ctx)
    assert result == PhaseResult.DRIFT
    assert "missing a required key" in detail


def test_probe_already_correct(home: Path, ctx: SetupContext) -> None:
    _make_dirs(home)
    _write_toml(home, _VALID_BODY)
    result, _ = l4.PHASE.probe(ctx)
    assert result == PhaseResult.ALREADY_CORRECT


def test_probe_conflict_on_invalid_auth_value(
    home: Path, ctx: SetupContext
) -> None:
    _make_dirs(home)
    _write_toml(
        home,
        '[host]\n'
        'docker_unprivileged_user = "sandbox"\n'
        'machinectl_authentication = "bogus"\n'
        'workspace_bridge_group = "sb-ws"\n',
    )
    result, detail = l4.PHASE.probe(ctx)
    assert result == PhaseResult.CONFLICT
    assert "invalid value" in detail


def test_probe_conflict_on_empty_user_value(
    home: Path, ctx: SetupContext
) -> None:
    _make_dirs(home)
    _write_toml(
        home,
        '[host]\n'
        'docker_unprivileged_user = ""\n'
        'machinectl_authentication = "sudo"\n'
        'workspace_bridge_group = "sb-ws"\n',
    )
    result, _ = l4.PHASE.probe(ctx)
    assert result == PhaseResult.CONFLICT


def test_probe_conflict_on_non_string_value(
    home: Path, ctx: SetupContext
) -> None:
    _make_dirs(home)
    _write_toml(
        home,
        '[host]\n'
        'docker_unprivileged_user = 7\n'
        'machinectl_authentication = "sudo"\n'
        'workspace_bridge_group = "sb-ws"\n',
    )
    result, _ = l4.PHASE.probe(ctx)
    assert result == PhaseResult.CONFLICT


def test_probe_drift_when_host_not_a_table(
    home: Path, ctx: SetupContext
) -> None:
    _make_dirs(home)
    _write_toml(home, 'host = "not-a-table"\n')
    # A non-table ``host`` value yields an empty host view → required keys
    # absent → DRIFT (act will replace with a proper [host] table).
    result, _ = l4.PHASE.probe(ctx)
    assert result == PhaseResult.DRIFT


def test_act_seeds_when_absent(home: Path, ctx: SetupContext) -> None:
    detail = l4.PHASE.act(ctx)
    assert "seeded/merged" in detail
    for sub in l4._STATE_SUBDIRS:
        d = home / sub
        assert d.is_dir()
        assert (d.stat().st_mode & 0o777) == 0o700
    host = _load(home)["host"]
    for key in l4._REQUIRED_HOST_KEYS:
        assert key in host


def test_act_merges_preserving_operator_handedits(
    home: Path, ctx: SetupContext
) -> None:
    _write_toml(
        home,
        '# operator comment\n'
        '[host]\n'
        'docker_unprivileged_user = "custom-user"\n'
        '\n'
        '[operator_section]\n'
        'untouched = true\n',
    )
    detail = l4.PHASE.act(ctx)
    assert "machinectl_authentication" in detail
    text = (home / "config" / "sandbox-ai.toml").read_text()
    assert "# operator comment" in text
    assert "untouched = true" in text
    doc = _load(home)
    # Operator's hand-edited value is preserved, not overwritten by the seed.
    assert doc["host"]["docker_unprivileged_user"] == "custom-user"
    assert doc["host"]["machinectl_authentication"] == "sudo"
    assert doc["operator_section"]["untouched"] is True


def test_act_dirs_only_when_toml_already_complete(
    home: Path, ctx: SetupContext
) -> None:
    _write_toml(home, _VALID_BODY)
    detail = l4.PHASE.act(ctx)
    assert "already complete" in detail


def test_act_replaces_non_table_host(home: Path, ctx: SetupContext) -> None:
    _write_toml(home, 'host = "not-a-table"\n')
    l4.PHASE.act(ctx)
    doc = _load(home)
    assert isinstance(doc["host"], dict)
    for key in l4._REQUIRED_HOST_KEYS:
        assert key in doc["host"]


def test_reverify_true_after_act(home: Path, ctx: SetupContext) -> None:
    l4.PHASE.act(ctx)
    assert l4.PHASE.reverify(ctx) is True


def test_reverify_false_when_dirs_absent(home: Path, ctx: SetupContext) -> None:
    _write_toml(home, _VALID_BODY)
    assert l4.PHASE.reverify(ctx) is False


def test_reverify_false_when_toml_absent(home: Path, ctx: SetupContext) -> None:
    _make_dirs(home)
    assert l4.PHASE.reverify(ctx) is False


def test_reverify_false_on_invalid_value(home: Path, ctx: SetupContext) -> None:
    _make_dirs(home)
    _write_toml(
        home,
        '[host]\n'
        'docker_unprivileged_user = "sandbox"\n'
        'machinectl_authentication = "nope"\n'
        'workspace_bridge_group = "sb-ws"\n',
    )
    assert l4.PHASE.reverify(ctx) is False


def test_reverify_false_on_corrupt_toml(home: Path, ctx: SetupContext) -> None:
    _make_dirs(home)
    _write_toml(home, "this is not = valid = toml ===\n[[[")
    assert l4.PHASE.reverify(ctx) is False


def test_probe_conflict_on_corrupt_toml_does_not_raise(
    home: Path, ctx: SetupContext
) -> None:
    # A syntactically-corrupt operator toml must NOT crash the plan pass
    # (which calls probe outside any try/except). The probe converts the
    # parse failure into a clean CONFLICT refusal — the spec's "refuse with
    # diagnostic, do NOT overwrite operator data".
    _make_dirs(home)
    _write_toml(home, "this is not = valid = toml ===\n[[[")
    result, detail = l4.PHASE.probe(ctx)
    assert result == PhaseResult.CONFLICT
    assert "not valid TOML" in detail
    assert "refusing to overwrite operator data" in detail
    assert "sandbox-ai.toml" in detail


def test_act_corrupt_toml_toctou_raises_typed_not_raw_parseerror(
    home: Path, ctx: SetupContext
) -> None:
    # CONFLICT skips act, so a parse failure only reaches _act via a TOCTOU
    # (file corrupted between probe and act). _act must raise the typed
    # TomlParseError (runner-classifiable as FAIL), never a bare tomlkit
    # ParseError, so operator data is never overwritten.
    import tomlkit.exceptions

    _make_dirs(home)
    _write_toml(home, "broken = = =\n[[[")
    with pytest.raises(l4.TomlParseError) as excinfo:
        l4.PHASE.act(ctx)
    assert isinstance(
        excinfo.value.__cause__, tomlkit.exceptions.ParseError
    )
    assert not isinstance(excinfo.value, tomlkit.exceptions.ParseError)


def test_content_aware(
    home: Path,
    ctx: SetupContext,
    assert_phase_content_aware: Callable[
        [Phase, SetupContext, Callable[[], None]], None
    ],
) -> None:
    _make_dirs(home)
    _write_toml(home, _VALID_BODY)

    def make_stale() -> None:
        # A wheel upgrade-style change: drop a now-required key from the
        # operator's on-disk toml. The probe must flip to DRIFT.
        _write_toml(home, '[host]\ndocker_unprivileged_user = "sandbox"\n')

    assert_phase_content_aware(l4.PHASE, ctx, make_stale)


def test_phase_shape() -> None:
    assert l4.PHASE.id == "l4"
    assert l4.PHASE.depends_on == ("l2",)
    assert l4.PHASE.identity == Identity.OPERATOR
