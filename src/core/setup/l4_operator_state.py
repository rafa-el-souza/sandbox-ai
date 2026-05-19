"""L4 — operator state: per-user dirs + sandbox-ai.toml seed (content-aware).

Creates ``<sandbox_ai_home()>/{config,state,instances,workspaces}`` (mode
``0700``) and seeds / merges the operator's ``sandbox-ai.toml`` ``[host]``
block. Runs as the operator identity (the per-user tree is operator-owned, not
root-owned); the cross-boundary primitive for the OPERATOR identity is
``pipe_cmd`` (design D3), wired via the phase-runner's :func:`route`.

Content-aware probe (design D10):

- the required ``[host]`` keys are ``docker_unprivileged_user``,
  ``machinectl_authentication``, ``workspace_bridge_group``;
- absent toml → ``MISSING`` (act seeds it);
- toml present, a required key missing → ``DRIFT`` (act *merges* the missing
  keys, preserving every operator hand-edit in other sections / keys);
- toml present, every required key present **with a structurally valid
  value** → ``ALREADY_CORRECT``;
- toml present but a required key holds an *invalid* value (e.g.
  ``machinectl_authentication`` not in ``{sudo, polkit}``, or a non-string /
  empty user / group) → ``CONFLICT``. The phase-runner will NOT call ``act``
  on a ``CONFLICT`` — setup never overwrites operator data; the operator must
  fix the value and re-run.

The toml is read **and** rewritten with :mod:`tomlkit` (the project's pinned
toml round-trip library, also used by ``core.scaffold``) so operator comments
and untouched sections survive the merge intact.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import tomlkit
import tomlkit.exceptions

from core.host_config import sandbox_ai_home
from core.setup.phase_runner import Identity, Phase, PhaseResult

if TYPE_CHECKING:
    from pathlib import Path

    from tomlkit import TOMLDocument

    from core.setup.phase_runner import SetupContext

# Per-user directories L4 owns (mode 0700, operator-owned).
_STATE_SUBDIRS = ("config", "state", "instances", "workspaces")

# Required ``[host]`` keys and their accepted shapes.
_REQUIRED_HOST_KEYS = (
    "docker_unprivileged_user",
    "machinectl_authentication",
    "workspace_bridge_group",
)
_VALID_AUTH_VALUES = ("sudo", "polkit")

# The default seed values written when the toml (or a key) is absent. These
# mirror ``core.host_config.HostSettings`` field defaults; the operator is
# expected to hand-edit ``docker_unprivileged_user`` afterwards.
_SEED_DEFAULTS: dict[str, str] = {
    "docker_unprivileged_user": "sandbox",
    "machinectl_authentication": "sudo",
    "workspace_bridge_group": "sb-ws",
}


def _toml_path() -> Path:
    """Resolve the canonical per-user ``sandbox-ai.toml`` path."""
    return sandbox_ai_home() / "config" / "sandbox-ai.toml"


class TomlParseError(RuntimeError):
    """The operator's ``sandbox-ai.toml`` exists but is not valid TOML.

    Single-sourced refusal type for the three ``_read_toml`` callers
    (``_probe`` / ``_act`` / ``_reverify``) so the "do NOT overwrite operator
    data on a parse failure" guard lives in one place (anti-hack rule 4).
    Carries the path + the underlying ``tomlkit`` error as ``__cause__``.
    """


def _read_toml(path: Path) -> TOMLDocument | None:
    """Parse the toml at ``path`` with tomlkit; ``None`` if it does not exist.

    Raises :class:`TomlParseError` (chaining the underlying
    ``tomlkit.exceptions.ParseError``) when the file exists but does not parse
    — the single place a corrupt operator toml is converted to a typed,
    operator-actionable refusal.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        return tomlkit.parse(text)
    except tomlkit.exceptions.ParseError as exc:
        raise TomlParseError(
            f"{path} is not valid TOML ({exc}); refusing to overwrite "
            f"operator data — fix or remove it and re-run"
        ) from exc


def _host_table(doc: TOMLDocument) -> dict[str, object]:
    """Return the ``[host]`` sub-table view (empty dict if absent / wrong type)."""
    host = doc.get("host")
    if isinstance(host, dict):
        return host
    return {}


def _key_value_invalid(key: str, value: object) -> bool:
    """``True`` iff ``value`` for required ``key`` is structurally invalid."""
    if not isinstance(value, str) or not value:
        return True
    return key == "machinectl_authentication" and value not in _VALID_AUTH_VALUES


def _classify_host_table(host: dict[str, object]) -> PhaseResult:
    """Classify an existing ``[host]`` table against the required-key contract.

    - any present required key with an invalid value → ``CONFLICT``;
    - all required keys present + valid → ``ALREADY_CORRECT``;
    - otherwise (≥1 required key absent, none invalid) → ``DRIFT``.
    """
    for key in _REQUIRED_HOST_KEYS:
        if key in host and _key_value_invalid(key, host[key]):
            return PhaseResult.CONFLICT
    if all(key in host for key in _REQUIRED_HOST_KEYS):
        return PhaseResult.ALREADY_CORRECT
    return PhaseResult.DRIFT


def _dirs_present() -> bool:
    """``True`` iff every owned per-user subdir already exists."""
    home = sandbox_ai_home()
    return all((home / sub).is_dir() for sub in _STATE_SUBDIRS)


def _probe(_ctx: SetupContext) -> tuple[PhaseResult, str]:
    """Content-aware probe over the per-user dirs + the toml ``[host]`` block."""
    path = _toml_path()
    try:
        doc = _read_toml(path)
    except TomlParseError as exc:
        return PhaseResult.CONFLICT, str(exc)
    if doc is None:
        return PhaseResult.MISSING, f"{path} absent; will seed [host] block"

    table_result = _classify_host_table(_host_table(doc))
    if table_result == PhaseResult.CONFLICT:
        return (
            PhaseResult.CONFLICT,
            f"{path} [host] block has an invalid value for a required key "
            f"({', '.join(_REQUIRED_HOST_KEYS)}); refusing to overwrite "
            f"operator data — fix the value and re-run setup",
        )
    if table_result == PhaseResult.DRIFT:
        return (
            PhaseResult.DRIFT,
            f"{path} [host] block missing a required key; will merge",
        )
    if not _dirs_present():
        return (
            PhaseResult.MISSING,
            "per-user state dirs absent; will create mode 0700",
        )
    return PhaseResult.ALREADY_CORRECT, "operator state present and valid"


def _act(_ctx: SetupContext) -> str:
    """Create owned dirs (mode 0700) and seed / merge the toml ``[host]`` block.

    The phase-runner guarantees ``act`` is never called on a ``CONFLICT``
    probe, so a present-but-invalid value is never reached here — this only
    seeds (absent) or merges-in missing required keys (drift), preserving every
    other operator key and tomlkit-tracked comment.

    A corrupt-toml ``CONFLICT`` is caught by ``_probe`` and skips ``act``; the
    only way a parse failure reaches the ``_read_toml`` call below is a TOCTOU
    (the file was corrupted between probe and act). ``_read_toml`` raises the
    typed :class:`TomlParseError` (never a bare ``tomlkit`` ``ParseError``),
    which the phase-runner classifies as ``FAIL`` — operator data is never
    overwritten.
    """
    home = sandbox_ai_home()
    for sub in _STATE_SUBDIRS:
        (home / sub).mkdir(parents=True, mode=0o700, exist_ok=True)

    path = _toml_path()
    doc = _read_toml(path)
    if doc is None:
        doc = tomlkit.document()
    host = doc.get("host")
    if not isinstance(host, dict):
        host = tomlkit.table()
        doc["host"] = host
    added: list[str] = []
    for key in _REQUIRED_HOST_KEYS:
        if key not in host:
            host[key] = _SEED_DEFAULTS[key]
            added.append(key)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    if added:
        return f"seeded/merged [host] keys: {', '.join(added)}"
    return "per-user state dirs created (toml already complete)"


def _reverify(_ctx: SetupContext) -> bool:
    """Confirm dirs exist and every required ``[host]`` key is present + valid."""
    if not _dirs_present():
        return False
    try:
        doc = _read_toml(_toml_path())
    except TomlParseError:
        return False
    if doc is None:
        return False
    host = _host_table(doc)
    return all(
        key in host and not _key_value_invalid(key, host[key])
        for key in _REQUIRED_HOST_KEYS
    )


PHASE = Phase(
    id="l4",
    name="operator state (per-user dirs + sandbox-ai.toml seed)",
    identity=Identity.OPERATOR,
    probe=_probe,
    act=_act,
    reverify=_reverify,
    depends_on=("l2",),
)
