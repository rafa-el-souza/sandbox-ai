"""Unit tests for core.binary_install (operator-side pinned-binary install).

No production callers exist yet (L6a / --update-runsc / doctor wire this in
later setup groups), so every branch is exercised here directly — the repo
enforces 100% coverage on src/core/.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from core import binary_install
from core.binary_install import (
    BinarySha512MismatchError,
    DriftResult,
    VerifyResult,
    detect_drift,
    install_pinned,
    verify_only,
)
from core.host_config import MachinectlAuth, minimal_host_config
from core.hydration import BINARY_REGISTRY

if TYPE_CHECKING:
    from core.host_config import HostConfig

_NAME = "runsc"


@pytest.fixture
def hc() -> HostConfig:
    return minimal_host_config("alice", MachinectlAuth.SUDO)


@pytest.fixture
def reserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the reserved install dir into tmp and stub chattr/chown.

    chattr and os.chown both require root; in unit context they are no-ops
    whose invocation is asserted via a recorder.
    """
    monkeypatch.setattr(binary_install, "RESERVED_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def chattr_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    def fake_chattr(flag: str, path: Path) -> None:
        calls.append((flag, str(path)))

    monkeypatch.setattr(binary_install, "_chattr", fake_chattr)
    return calls


@pytest.fixture
def chown_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fake_chown(path: Path) -> None:
        calls.append(str(path))

    monkeypatch.setattr(binary_install, "_chown_root", fake_chown)
    return calls


def _pinned_sha() -> str:
    return BINARY_REGISTRY[_NAME].sha512


def _sha512(data: bytes) -> str:
    return hashlib.sha512(data).hexdigest()


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


def _patch_urlopen(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> list[str]:
    seen: list[str] = []

    def fake_urlopen(url: str) -> _FakeResponse:
        seen.append(url)
        return _FakeResponse(payload)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return seen


# ─── detect_drift / verify_only ──────────────────────────────────────────────


def test_detect_drift_absent(reserved: Path, hc: HostConfig) -> None:
    result = detect_drift(_NAME, hc)
    assert result == DriftResult(status="absent", installed_sha=None, pinned_sha=_pinned_sha())


def test_detect_drift_match(reserved: Path, hc: HostConfig) -> None:
    payload = b"pinned-binary-content"
    target = reserved / _NAME
    target.write_bytes(payload)
    pinned = _sha512(payload)
    # Force the pin to equal the on-disk content for the match branch.
    object.__setattr__(BINARY_REGISTRY[_NAME], "sha512", pinned)
    try:
        result = detect_drift(_NAME, hc)
    finally:
        pass
    assert result.status == "match"
    assert result.installed_sha == pinned
    assert result.pinned_sha == pinned


def test_detect_drift_drift(reserved: Path, hc: HostConfig) -> None:
    target = reserved / _NAME
    target.write_bytes(b"stale-content")
    result = detect_drift(_NAME, hc)
    assert result.status == "drift"
    assert result.installed_sha == _sha512(b"stale-content")
    assert result.pinned_sha == _pinned_sha()
    assert result.installed_sha != result.pinned_sha


def test_verify_only_is_readonly_and_typed(reserved: Path, hc: HostConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    # No network: any urlopen attempt would raise.
    def boom(_url: str) -> None:
        raise AssertionError("verify_only must not touch the network")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    target = reserved / _NAME
    target.write_bytes(b"stale-content")
    result = verify_only(_NAME, hc)
    assert isinstance(result, VerifyResult)
    assert result.status == "drift"
    assert result.installed_sha == _sha512(b"stale-content")
    assert result.pinned_sha == _pinned_sha()


def test_verify_only_absent(reserved: Path, hc: HostConfig) -> None:
    assert verify_only(_NAME, hc) == VerifyResult(status="absent", installed_sha=None, pinned_sha=_pinned_sha())


# ─── install_pinned ──────────────────────────────────────────────────────────


def test_install_pinned_fresh(
    reserved: Path,
    hc: HostConfig,
    monkeypatch: pytest.MonkeyPatch,
    chattr_calls: list[tuple[str, str]],
    chown_calls: list[str],
) -> None:
    payload = b"the-real-runsc-bytes"
    object.__setattr__(BINARY_REGISTRY[_NAME], "sha512", _sha512(payload))
    seen = _patch_urlopen(monkeypatch, payload)

    install_pinned(_NAME, hc)

    target = reserved / _NAME
    assert target.read_bytes() == payload
    assert "$(arch)" not in seen[0]
    assert chown_calls == [str(reserved / f".{_NAME}.staging")]
    assert chattr_calls == [("+i", str(target))]
    # staging cleaned up
    assert not (reserved / f".{_NAME}.staging").exists()


def test_install_pinned_sha_mismatch_refuses(
    reserved: Path,
    hc: HostConfig,
    monkeypatch: pytest.MonkeyPatch,
    chattr_calls: list[tuple[str, str]],
    chown_calls: list[str],
) -> None:
    object.__setattr__(BINARY_REGISTRY[_NAME], "sha512", "f" * 128)
    _patch_urlopen(monkeypatch, b"corrupted-or-tampered")

    with pytest.raises(BinarySha512MismatchError, match="sha512 mismatch"):
        install_pinned(_NAME, hc)

    # No install side effects whatsoever.
    assert not (reserved / _NAME).exists()
    assert not (reserved / f".{_NAME}.staging").exists()
    assert chattr_calls == []
    assert chown_calls == []
    # download tmpfile cleaned up
    assert list(reserved.glob(f".{_NAME}.dl.*")) == []


def test_install_pinned_force_overwrites_immutable_target(
    reserved: Path,
    hc: HostConfig,
    monkeypatch: pytest.MonkeyPatch,
    chattr_calls: list[tuple[str, str]],
    chown_calls: list[str],
) -> None:
    target = reserved / _NAME
    target.write_bytes(b"old-pinned-binary")
    payload = b"new-pinned-binary"
    object.__setattr__(BINARY_REGISTRY[_NAME], "sha512", _sha512(payload))
    _patch_urlopen(monkeypatch, payload)

    install_pinned(_NAME, hc, force=True)

    assert target.read_bytes() == payload
    # -i unseals the existing target before rename, +i re-seals after.
    assert chattr_calls == [("-i", str(target)), ("+i", str(target))]


def test_install_pinned_non_force_does_not_unseal(
    reserved: Path,
    hc: HostConfig,
    monkeypatch: pytest.MonkeyPatch,
    chattr_calls: list[tuple[str, str]],
    chown_calls: list[str],
) -> None:
    target = reserved / _NAME
    target.write_bytes(b"old")
    payload = b"replacement"
    object.__setattr__(BINARY_REGISTRY[_NAME], "sha512", _sha512(payload))
    _patch_urlopen(monkeypatch, payload)

    install_pinned(_NAME, hc, force=False)

    assert target.read_bytes() == payload
    # No -i: non-force never unseals (rename still replaces in unit context).
    assert chattr_calls == [("+i", str(target))]


def test_install_pinned_force_no_existing_target_skips_unseal(
    reserved: Path,
    hc: HostConfig,
    monkeypatch: pytest.MonkeyPatch,
    chattr_calls: list[tuple[str, str]],
    chown_calls: list[str],
) -> None:
    payload = b"first-install"
    object.__setattr__(BINARY_REGISTRY[_NAME], "sha512", _sha512(payload))
    _patch_urlopen(monkeypatch, payload)

    install_pinned(_NAME, hc, force=True)

    # force=True but target absent → no -i, only +i.
    assert chattr_calls == [("+i", reserved.joinpath(_NAME).__str__())]


def test_install_pinned_cleans_staging_on_post_replace_failure(
    reserved: Path,
    hc: HostConfig,
    monkeypatch: pytest.MonkeyPatch,
    chattr_calls: list[tuple[str, str]],
    chown_calls: list[str],
) -> None:
    payload = b"binary"
    object.__setattr__(BINARY_REGISTRY[_NAME], "sha512", _sha512(payload))
    _patch_urlopen(monkeypatch, payload)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("chmod failed after staging created")

    monkeypatch.setattr("core.binary_install.os.chmod", boom)

    with pytest.raises(OSError, match="chmod failed"):
        install_pinned(_NAME, hc)

    # staging existed (os.replace ran) → the finally block unlinks it.
    assert not (reserved / f".{_NAME}.staging").exists()
    assert not (reserved / _NAME).exists()


def test_chattr_invokes_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], bool]] = []

    def fake_run(cmd: list[str], *, check: bool) -> None:
        calls.append((cmd, check))

    monkeypatch.setattr("core.binary_install.subprocess.run", fake_run)
    binary_install._chattr("+i", Path("/usr/local/libexec/sandbox-ai/runsc"))
    assert calls == [(["chattr", "+i", "/usr/local/libexec/sandbox-ai/runsc"], True)]


def test_chown_root_invokes_oschown(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Path, int, int]] = []

    def fake_chown(path: Path, uid: int, gid: int) -> None:
        calls.append((path, uid, gid))

    monkeypatch.setattr("core.binary_install.os.chown", fake_chown)
    binary_install._chown_root(Path("/usr/local/libexec/sandbox-ai/runsc"))
    assert calls == [(Path("/usr/local/libexec/sandbox-ai/runsc"), 0, 0)]


@pytest.fixture(autouse=True)
def _restore_pin() -> object:
    """Restore the registry sha512 after tests that mutate it in place."""
    original = BINARY_REGISTRY[_NAME].sha512
    yield
    object.__setattr__(BINARY_REGISTRY[_NAME], "sha512", original)
