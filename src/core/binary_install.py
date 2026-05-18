"""Operator-side pinned-binary install + drift detection.

Consumed by setup's L6a phase, the ``sandbox setup --update-runsc`` flag, and
doctor's ``runsc_pinned_match`` check. The maintainer-side rotation logic lives
separately in ``scripts/rotate_pins.py`` (not packaged); only ``BINARY_REGISTRY``
(in ``core.hydration``) is shared between the two sides — see design D8.

Trust model: pinned binaries land in the reserved, root-owned, non-PATH
``/usr/local/libexec/sandbox-ai/`` directory (design D4/D6). Install is atomic
(stage → ``os.rename`` on one filesystem) and the target is sealed with
``chattr +i`` (the F-003 compensating control). A sha512 mismatch on download is
a security boundary: install is refused, never warned.
"""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from core.exceptions import SandboxExecutionError
from core.hydration import BINARY_REGISTRY

if TYPE_CHECKING:
    from core.host_config import HostConfig

# Reserved, root-owned, non-PATH install directory (design D4/D6). One
# filesystem, so ``os.rename`` from the sibling staging path is atomic.
RESERVED_DIR = Path("/usr/local/libexec/sandbox-ai")

_ARCH_TOKEN = "$(arch)"

_DriftStatus = Literal["absent", "match", "drift"]


class BinarySha512MismatchError(SandboxExecutionError):
    """A downloaded binary's sha512 did not match the pinned registry value.

    Raised before any install side effect — the download is discarded. This is
    a security refusal, not a recoverable warning.
    """


@dataclass(frozen=True)
class DriftResult:
    """Outcome of a read-only on-disk drift probe (``detect_drift``)."""

    status: _DriftStatus
    installed_sha: str | None
    pinned_sha: str


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of a read-only, no-network verification (``verify_only``).

    Doctor calls this; it MUST NOT touch the network.
    """

    status: _DriftStatus
    installed_sha: str | None
    pinned_sha: str


def _target_path(name: str) -> Path:
    """Reserved install path for a binary (``/usr/local/libexec/sandbox-ai/<name>``)."""
    return RESERVED_DIR / name


def _staging_path(name: str) -> Path:
    """Sibling staging path (same filesystem as the target → atomic rename)."""
    return RESERVED_DIR / f".{name}.staging"


def _pinned_sha(name: str) -> str:
    """The pinned sha512 for ``name`` from the shared registry."""
    return BINARY_REGISTRY[name].sha512


def _resolved_url(name: str) -> str:
    """Resolve the registry URL template, substituting the literal ``$(arch)``.

    ``platform.machine()`` returns ``x86_64`` / ``aarch64`` on Linux, which
    match the gVisor release URL's arch-dir naming exactly. Nothing else is
    substituted — the version is already literal in the template.
    """
    return BINARY_REGISTRY[name].url_template.replace(_ARCH_TOKEN, platform.machine())


def _file_sha512(path: Path) -> str:
    """Streamed sha512 of an on-disk file (lowercase hex)."""
    digest = hashlib.sha512()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _classify(installed_sha: str | None, pinned_sha: str) -> _DriftStatus:
    """Map an observed sha (or absence) against the pinned sha to a status."""
    if installed_sha is None:
        return "absent"
    if installed_sha == pinned_sha:
        return "match"
    return "drift"


def detect_drift(name: str, host_config: HostConfig) -> DriftResult:
    """Read-only filesystem probe: on-disk binary sha512 vs the pinned sha512.

    ``absent`` if the reserved path does not exist; ``match`` / ``drift``
    otherwise. No network, no mutation. ``host_config`` is accepted for caller
    uniformity (the reserved path is root-owned and host-independent).
    """
    del host_config  # reserved path is host-independent; accepted for API uniformity
    pinned = _pinned_sha(name)
    target = _target_path(name)
    installed = _file_sha512(target) if target.is_file() else None
    return DriftResult(status=_classify(installed, pinned), installed_sha=installed, pinned_sha=pinned)


def verify_only(name: str, host_config: HostConfig) -> VerifyResult:
    """Read-only, no-network verification for doctor.

    Identical observation to ``detect_drift`` but a distinct result type so the
    doctor check and the setup phase cannot accidentally cross-wire. No network
    access — doctor must never reach upstream.
    """
    drift = detect_drift(name, host_config)
    return VerifyResult(status=drift.status, installed_sha=drift.installed_sha, pinned_sha=drift.pinned_sha)


def _download_and_verify(name: str, dest: Path) -> None:
    """Stream the pinned URL to ``dest`` and refuse on sha512 mismatch.

    The download is written to ``dest`` then hashed; a mismatch raises
    ``BinarySha512MismatchError`` (the caller discards ``dest``). The hash is
    computed over the full written content.
    """
    url = _resolved_url(name)
    with urllib.request.urlopen(url) as response:
        data = response.read()
    dest.write_bytes(data)
    actual = _file_sha512(dest)
    expected = _pinned_sha(name)
    if actual != expected:
        raise BinarySha512MismatchError(
            f"[FATAL] Sandbox Execution Fault: pinned binary '{name}' sha512 mismatch "
            f"(expected {expected}, got {actual}); refusing to install."
        )


def _chattr(flag: str, path: Path) -> None:
    """Toggle the immutable bit on ``path`` (``chattr +i`` / ``chattr -i``)."""
    subprocess.run(["chattr", flag, str(path)], check=True)


def _chown_root(path: Path) -> None:
    """Chown ``path`` to ``root:root`` (uid 0 / gid 0)."""
    os.chown(path, 0, 0)


def install_pinned(name: str, host_config: HostConfig, *, force: bool = False) -> None:
    """Download, sha512-verify, and atomically install a pinned binary.

    Stages to ``.<name>.staging`` (same filesystem), ``chmod 0755`` +
    ``chown root:root``, then ``os.rename`` to the reserved target (atomic).
    The new target is sealed with ``chattr +i``.

    With ``force=True`` an existing immutable target is unsealed (``chattr -i``)
    before the rename; without it, an existing target is left in place by the
    caller's drift policy — this function still performs the replace when
    invoked (the L6a drift-skip lives in the phase, not here).

    Raises ``BinarySha512MismatchError`` if the download fails verification.
    """
    del host_config  # reserved path is host-independent; accepted for API uniformity
    target = _target_path(name)
    staging = _staging_path(name)

    RESERVED_DIR.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(dir=str(RESERVED_DIR), prefix=f".{name}.dl.")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        _download_and_verify(name, tmp_path)
        os.replace(tmp_path, staging)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    try:
        os.chmod(staging, 0o755)
        _chown_root(staging)

        if force and target.exists():
            _chattr("-i", target)

        os.rename(staging, target)
        _chattr("+i", target)
    finally:
        if staging.exists():
            staging.unlink()
