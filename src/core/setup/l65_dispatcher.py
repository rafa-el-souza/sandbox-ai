# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""L6.5 — dispatcher install (offline reproducible compile + manifest).

Compiles the Go dispatcher via ``core.dispatch.compile_dispatcher`` (sister
change ``runtime-dispatcher``) and installs it root-owned + immutable at
``/usr/local/libexec/sandbox-ai/dispatch``. Identity ``ROOT`` (the install
mechanics — staging, chown, chattr — are root-side; the compile itself crosses
into the lingering daemon user internally via ``pipe_cmd``, owned entirely by
``compile_dispatcher``).

Two C-001 reconciliations this phase honors (do NOT regress):

1. **``core.dispatch.compile_dispatcher(output_path, host_config)`` is
   2-arg.** ``build_dir`` was removed in C-001's ratified Finding-L/M-FixD-2b
   redesign — the function owns its build dir internally (a per-call
   ``mktemp -d`` on the lingering daemon user's ``/run/user/<uid>`` tmpfs),
   crosses via ``pipe_cmd``, and returns the built binary over the crossing's
   captured stdout written to ``output_path`` mode ``0755``. This phase passes
   exactly 2 args; ``output_path`` is a root-owned staging path that is then
   ``chmod 0755`` + ``chown root:root`` + ``os.replace``'d onto the reserved
   target and sealed with ``chattr +i``. **Linger is an architectural
   prerequisite of the compile itself** (no linger ⇒ no ``/run/user/<uid>`` ⇒
   ``compile_dispatcher`` fails); the ``depends_on=("l6a",)`` chain (and L5's
   ``enable-linger`` upstream) satisfies it.

2. **The manifest's ``source_bundle_sha512`` is derived from
   ``core.dispatch.DISPATCH_SOURCE_ENTRIES``** (the single source of truth —
   currently ``main.go, main_test.go, go.mod, go.sum, vendor, fixtures``), NOT
   a hardcoded ``{go.mod, go.sum, main.go, vendor/**}`` subset (which omits
   ``main_test.go``/``fixtures/`` and would miss a Python↔Go parity-fixture
   drift that gates compile success). Directory entries are expanded to every
   file beneath them; the sha512 is over file content only (no mtime/mode), in
   deterministic sorted relative-path order.

Content-aware probe with source-bundle awareness (design D10): the manifest at
``/usr/local/libexec/sandbox-ai/dispatcher.manifest.json`` records
``{compiled_sha512, source_bundle_sha512, compile_timestamp}``. SKIP
(``ALREADY_CORRECT``) **only** when the manifest's ``compiled_sha512`` equals
the on-disk binary's sha512 AND the manifest's ``source_bundle_sha512`` equals
a freshly-computed source-bundle sha512. Either mismatch (or absent
manifest/binary) → recompile + install + rewrite manifest. This handles wheel
upgrades correctly without recompiling on every no-drift re-run.

Manifest home — host plane, not per-operator (F-021). The dispatcher binary is
**convergent shared host state** (identical for every operator), so its manifest
is host-level too: it lives **alongside the binary** under
``/usr/local/libexec/sandbox-ai/`` (root-owned, mode ``0644``), NOT under
``<sandbox_ai_home()>/state/``. Resolving ``sandbox_ai_home()`` in this
root-running phase would target ``/root/.sandbox-ai`` (``$HOME=/root`` under
sudo), where the operator's ``sandbox doctor`` could never read it — the
manifest is the one artifact only setup can produce, so it MUST land on a
host path every operator can see. ``dispatcher_sha_drift`` imports
``manifest_path`` from this module (single source); the move is therefore a
one-line change here.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import subprocess
import tempfile
from importlib.resources import files as _resource_files
from pathlib import Path
from typing import TYPE_CHECKING

from core.dispatch import DISPATCH_SOURCE_ENTRIES, compile_dispatcher
from core.exceptions import SandboxExecutionError
from core.host_config import DockerExecutionMode
from core.setup.phase_runner import Identity, Phase, PhaseResult

__all__ = [
    "file_sha512",
    "manifest_path",
    "read_manifest",
    "source_bundle_sha512",
]

if TYPE_CHECKING:
    from importlib.resources.abc import Traversable

    from core.json_types import JsonValue
    from core.setup.phase_runner import SetupContext

# The reserved, root-owned, non-PATH install target (design D4/D6).
_TARGET = Path("/usr/local/libexec/sandbox-ai/dispatch")
_STAGING = Path("/usr/local/libexec/sandbox-ai/.dispatch.staging")


def manifest_path() -> Path:
    """Resolve the host-plane manifest path (alongside the binary, F-021).

    Derived from ``_TARGET.parent`` so the manifest is always the binary's
    sibling — ``/usr/local/libexec/sandbox-ai/dispatcher.manifest.json`` — and
    so the test seam that redirects ``_TARGET`` redirects the manifest with it.
    """
    return _TARGET.parent / "dispatcher.manifest.json"


def _collect_files(node: Traversable, rel: str, into: dict[str, bytes]) -> None:
    """Recursively collect ``rel -> content`` for a resource file or directory.

    Directory entries (``vendor``, ``fixtures``) are expanded to every file
    beneath them; the relative path key is the source-tree-relative path so the
    hash is stable across the source tree and an installed wheel.
    """
    if node.is_dir():
        for child in node.iterdir():
            _collect_files(child, f"{rel}/{child.name}", into)
    else:
        into[rel] = node.read_bytes()


def source_bundle_sha512() -> str:
    """sha512 of the C-001 compile-input file set (content-only, sorted paths).

    The file set is **derived from** ``core.dispatch.DISPATCH_SOURCE_ENTRIES``
    — not a hardcoded subset — so the drift coverage automatically tracks
    C-001's compile inputs if that tuple ever changes. The hash is over file
    content concatenated in deterministic alphabetical relative-path order; no
    metadata (mtime, mode) participates.
    """
    dispatch_root = _resource_files("templates").joinpath("dispatch")
    files: dict[str, bytes] = {}
    for entry in DISPATCH_SOURCE_ENTRIES:
        _collect_files(dispatch_root.joinpath(entry), entry, files)
    digest = hashlib.sha512()
    for rel in sorted(files):
        digest.update(files[rel])
    return digest.hexdigest()


def file_sha512(path: Path) -> str | None:
    """Streamed sha512 of ``path`` (lowercase hex); ``None`` if absent."""
    if not path.is_file():
        return None
    digest = hashlib.sha512()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_manifest() -> dict[str, str] | None:
    """Read the dispatcher manifest; ``None`` if absent or unreadable JSON."""
    path = manifest_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    parsed: JsonValue = json.loads(text)
    if not isinstance(parsed, dict):
        return None
    return {k: v for k, v in parsed.items() if isinstance(v, str)}


def _probe(_ctx: SetupContext) -> tuple[PhaseResult, str]:
    """Content-aware probe: manifest compiled+source shas vs observed reality."""
    manifest = read_manifest()
    if manifest is None:
        return (
            PhaseResult.MISSING,
            "dispatcher manifest absent; will compile + install + record",
        )
    binary_sha = file_sha512(_TARGET)
    if binary_sha is None:
        return (
            PhaseResult.MISSING,
            f"{_TARGET} absent though manifest exists; will recompile",
        )
    current_source = source_bundle_sha512()
    if (
        manifest.get("compiled_sha512") == binary_sha
        and manifest.get("source_bundle_sha512") == current_source
    ):
        return (
            PhaseResult.ALREADY_CORRECT,
            "dispatcher binary + source bundle match the manifest",
        )
    return (
        PhaseResult.DRIFT,
        "dispatcher binary or source bundle drifted from the manifest; "
        "will recompile + reinstall + update manifest",
    )


def _write_manifest(compiled_sha: str, source_sha: str) -> None:
    """Write the manifest (mode 0644, root:root) — three keys exactly.

    Host-plane artifact (F-021): world-readable so every operator's
    ``sandbox doctor`` can read it, root-owned so only setup writes it. An
    explicit ``chown`` keeps it root-owned even on a re-run that truncates a
    pre-existing file.
    """
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "compiled_sha512": compiled_sha,
        "source_bundle_sha512": source_sha,
        "compile_timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode())
    finally:
        os.close(fd)
    os.chmod(path, 0o644)
    os.chown(path, 0, 0)


def _install_compiled(staging: Path) -> None:
    """chmod 0755 + chown root:root + atomic replace onto target + chattr +i.

    An existing immutable target is unsealed before the replace so a re-compile
    on a wheel upgrade can land the new binary.
    """
    os.chmod(staging, 0o755)
    os.chown(staging, 0, 0)
    if _TARGET.exists():
        subprocess.run(["chattr", "-i", str(_TARGET)], check=True)
    os.replace(staging, _TARGET)
    subprocess.run(["chattr", "+i", str(_TARGET)], check=True)


def _act(ctx: SetupContext) -> str:
    """Compile (2-arg ``compile_dispatcher``) → install → write manifest.

    ``compile_dispatcher`` owns its internal build dir and the ``pipe_cmd``
    crossing; this phase only supplies the root-owned staging ``output_path``
    and performs the root-side install + manifest write.
    """
    host_config = ctx.host_config
    _TARGET.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(_TARGET.parent), prefix=".dispatch.compile."
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        compile_dispatcher(str(tmp_path), host_config)
        os.replace(tmp_path, _STAGING)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    try:
        _install_compiled(_STAGING)
    finally:
        if _STAGING.exists():
            _STAGING.unlink()

    compiled_sha = file_sha512(_TARGET)
    if compiled_sha is None:
        raise SandboxExecutionError(
            f"[FATAL] Sandbox Execution Fault: dispatcher missing at {_TARGET} "
            f"after install; refusing to record manifest."
        )
    source_sha = source_bundle_sha512()
    _write_manifest(compiled_sha, source_sha)
    return f"dispatcher compiled + installed at {_TARGET}; manifest recorded"


def _reverify(_ctx: SetupContext) -> bool:
    """Confirm the manifest now matches the on-disk binary + source bundle."""
    manifest = read_manifest()
    if manifest is None:
        return False
    binary_sha = file_sha512(_TARGET)
    if binary_sha is None:
        return False
    return (
        manifest.get("compiled_sha512") == binary_sha
        and manifest.get("source_bundle_sha512") == source_bundle_sha512()
    )


PHASE = Phase(
    id="l65",
    name="dispatcher install (offline reproducible compile)",
    identity=Identity.ROOT,
    probe=_probe,
    act=_act,
    reverify=_reverify,
    depends_on=("l6a",),
    # fork B (E-005/F-027): operator-rootless bypasses the dispatcher entirely,
    # so no dispatcher binary is ever compiled or installed in that mode.
    applies_in=frozenset({DockerExecutionMode.SEPARATE_USER}),
)
