# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for scripts/rotate_pins.py — polymorphic image+binary pin rotation.

Validates the maintainer-side rotation script:

  * Regression-import guard — the module must import without ``SyntaxError``
    (the pre-3.2 tree had a Python-2 ``except A, B:`` clause that made the
    module unimportable; this test is the proof it is fixed).
  * SIGNER_REGISTRY structure.
  * ``resolve_drift("image")`` drift detection (migrated from the old
    pre-rename image-digest resolution cases, adapted to the uniform
    ``DriftEntry`` shape).
  * ``resolve_drift("binary")`` sha512-sidecar drift detection.
  * ``_verify_signature`` polymorphic dispatch on ``kind``.
  * ``check_dirty_tree`` git status guard.
  * ``main`` --dry-run / --auto-commit behavior.
"""

from __future__ import annotations

import importlib
import io
import subprocess
from typing import Any
from unittest.mock import patch

import pytest


class TestRegressionImport:
    """The module must import cleanly — proves the Python-2 except is gone."""

    def test_import_raises_no_syntax_error(self) -> None:
        # importlib.import_module re-imports/compiles the module; a
        # `except A, B:` clause would raise SyntaxError here.
        mod = importlib.import_module("scripts.rotate_pins")
        assert mod is not None

    def test_has_main(self) -> None:
        from scripts.rotate_pins import main

        assert callable(main)

    def test_has_resolve_drift(self) -> None:
        from scripts.rotate_pins import resolve_drift

        assert callable(resolve_drift)

    def test_has_check_dirty_tree(self) -> None:
        from scripts.rotate_pins import check_dirty_tree

        assert callable(check_dirty_tree)

    def test_has_signer_registry(self) -> None:
        from scripts.rotate_pins import SIGNER_REGISTRY

        assert isinstance(SIGNER_REGISTRY, dict)


class TestSignerRegistry:
    """SIGNER_REGISTRY structure validation (migrated 8.T(f))."""

    def test_signer_registry_values_valid(self) -> None:
        from scripts.rotate_pins import SIGNER_REGISTRY

        valid_methods = {"cosign-keyless", "docker-content-trust", "none"}
        assert isinstance(SIGNER_REGISTRY, dict)
        assert len(SIGNER_REGISTRY) == 7, f"Expected 7 entries, got {len(SIGNER_REGISTRY)}"
        for key, method in SIGNER_REGISTRY.items():
            assert isinstance(key, str)
            assert isinstance(method, str)
            assert method in valid_methods, f"{key}: {method!r} not in {valid_methods}"


class TestResolveImageDrift:
    """resolve_drift("image") — migrated from old resolve_digests cases."""

    def test_no_drift_returns_empty(self) -> None:
        """When current tag digest matches pinned, image drift is empty."""
        from core.hydration import IMAGE_REGISTRY
        from scripts.rotate_pins import resolve_drift

        def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            cmd_str = " ".join(cmd)
            for _key, pin in IMAGE_REGISTRY.items():
                if pin.tagged in cmd_str:
                    return subprocess.CompletedProcess(
                        args=[],
                        returncode=0,
                        stdout=f'{{"digest": "{pin.digest}"}}',
                        stderr="",
                    )
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            drift = resolve_drift("image")
            assert drift == []

    def test_drift_detected(self) -> None:
        """When current tag digest differs, a uniform DriftEntry is emitted."""
        from core.hydration import IMAGE_REGISTRY
        from scripts.rotate_pins import resolve_drift

        keys = list(IMAGE_REGISTRY.keys())
        new_digest = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

        def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            cmd_str = " ".join(cmd)
            if IMAGE_REGISTRY[keys[0]].tagged in cmd_str:
                return subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=f'{{"digest": "{new_digest}"}}',
                    stderr="",
                )
            for key in keys[1:]:
                pin = IMAGE_REGISTRY[key]
                if pin.tagged in cmd_str:
                    return subprocess.CompletedProcess(
                        args=[],
                        returncode=0,
                        stdout=f'{{"digest": "{pin.digest}"}}',
                        stderr="",
                    )
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            drift = resolve_drift("image")
            assert len(drift) >= 1
            entry = drift[0]
            assert entry["kind"] == "image"
            assert entry["old"] == IMAGE_REGISTRY[keys[0]].digest
            assert entry["new"] == new_digest
            assert "old" in entry
            assert "new" in entry
            assert "verification_method" in entry

    def test_inspect_failure_skips_entry(self) -> None:
        """A non-zero docker manifest inspect skips that entry (no drift)."""
        from scripts.rotate_pins import resolve_drift

        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")

        with patch("subprocess.run", side_effect=fake_run):
            assert resolve_drift("image") == []

    def test_timeout_skips_entry(self) -> None:
        """A docker manifest inspect timeout skips that entry."""
        from scripts.rotate_pins import resolve_drift

        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd="docker", timeout=10)

        with patch("subprocess.run", side_effect=fake_run):
            assert resolve_drift("image") == []

    def test_invalid_json_skips_entry(self) -> None:
        """Invalid JSON from manifest inspect skips that entry."""
        from scripts.rotate_pins import resolve_drift

        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            assert resolve_drift("image") == []


class _FakeResponse:
    """Minimal context-manager stand-in for urllib.request.urlopen()."""

    def __init__(self, body: bytes) -> None:
        self._buf = io.BytesIO(body)

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._buf.read()


class TestResolveBinaryDrift:
    """resolve_drift("binary") — sha512-sidecar drift detection."""

    def test_no_drift_returns_empty(self) -> None:
        """Sidecar sha512 matching the pin yields no binary drift."""
        from core.hydration import BINARY_REGISTRY
        from scripts.rotate_pins import resolve_drift

        pin = BINARY_REGISTRY["runsc"]
        body = f"{pin.sha512}  runsc\n".encode()

        with patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
            assert resolve_drift("binary") == []

    def test_drift_detected(self) -> None:
        """A differing sidecar sha512 emits a uniform binary DriftEntry."""
        from core.hydration import BINARY_REGISTRY
        from scripts.rotate_pins import resolve_drift

        pin = BINARY_REGISTRY["runsc"]
        new_sha = "f" * 128
        body = f"{new_sha}  runsc\n".encode()

        with patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
            drift = resolve_drift("binary")

        assert len(drift) == 1
        entry = drift[0]
        assert entry["kind"] == "binary"
        assert entry["key"] == "runsc"
        assert entry["old"] == pin.sha512
        assert entry["new"] == new_sha
        assert entry["verification_method"] == "sha512-sidecar"

    def test_fetch_failure_skips_entry(self) -> None:
        """A failed sidecar fetch skips the entry (no drift, no crash)."""
        import urllib.error

        from scripts.rotate_pins import resolve_drift

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
            assert resolve_drift("binary") == []

    def test_empty_sidecar_skips_entry(self) -> None:
        """An empty sidecar body yields no parsable digest → skipped."""
        from scripts.rotate_pins import resolve_drift

        with patch("urllib.request.urlopen", return_value=_FakeResponse(b"")):
            assert resolve_drift("binary") == []


class TestVerifySignatureDispatch:
    """_verify_signature dispatches on DriftEntry['kind']."""

    def test_binary_match_passes(self) -> None:
        from core.hydration import BINARY_REGISTRY
        from scripts.rotate_pins import DriftEntry, _verify_signature

        new_sha = "a" * 128
        entry: DriftEntry = {
            "kind": "binary",
            "key": "runsc",
            "old": BINARY_REGISTRY["runsc"].sha512,
            "new": new_sha,
            "verification_method": "sha512-sidecar",
        }
        body = f"{new_sha}  runsc\n".encode()
        with patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
            assert _verify_signature(entry) is True

    def test_binary_mismatch_fails(self) -> None:
        from scripts.rotate_pins import DriftEntry, _verify_signature

        entry: DriftEntry = {
            "kind": "binary",
            "key": "runsc",
            "old": "old",
            "new": "a" * 128,
            "verification_method": "sha512-sidecar",
        }
        body = b"deadbeef  runsc\n"
        with patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
            assert _verify_signature(entry) is False

    def test_image_none_method_passes(self) -> None:
        from scripts.rotate_pins import DriftEntry, _verify_signature

        entry: DriftEntry = {
            "kind": "image",
            "key": "coredns",  # SIGNER_REGISTRY → "none"
            "old": "sha256:aaa",
            "new": "sha256:bbb",
            "verification_method": "none",
        }
        assert _verify_signature(entry) is True

    def test_image_cosign_keyless_returncode_zero_passes(self) -> None:
        from scripts.rotate_pins import DriftEntry, _verify_signature

        entry: DriftEntry = {
            "kind": "image",
            "key": "wolfi_base",  # SIGNER_REGISTRY → "cosign-keyless"
            "old": "sha256:aaa",
            "new": "sha256:bbb",
            "verification_method": "cosign-keyless",
        }

        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=ok):
            assert _verify_signature(entry) is True

    def test_image_cosign_keyless_returncode_nonzero_fails(self) -> None:
        from scripts.rotate_pins import DriftEntry, _verify_signature

        entry: DriftEntry = {
            "kind": "image",
            "key": "wolfi_base",
            "old": "sha256:aaa",
            "new": "sha256:bbb",
            "verification_method": "cosign-keyless",
        }
        fail = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="bad")
        with patch("subprocess.run", return_value=fail):
            assert _verify_signature(entry) is False

    def test_image_cosign_missing_binary_fails(self) -> None:
        """cosign not installed → FileNotFoundError caught → verification fails."""
        from scripts.rotate_pins import DriftEntry, _verify_signature

        entry: DriftEntry = {
            "kind": "image",
            "key": "wolfi_base",
            "old": "sha256:aaa",
            "new": "sha256:bbb",
            "verification_method": "cosign-keyless",
        }
        with patch("subprocess.run", side_effect=FileNotFoundError("cosign")):
            assert _verify_signature(entry) is False

    def test_image_dct_method_passes(self) -> None:
        from scripts.rotate_pins import DriftEntry, _verify_signature

        entry: DriftEntry = {
            "kind": "image",
            "key": "busybox_musl",  # SIGNER_REGISTRY → "docker-content-trust"
            "old": "sha256:aaa",
            "new": "sha256:bbb",
            "verification_method": "docker-content-trust",
        }
        assert _verify_signature(entry) is True


class TestCheckDirtyTree:
    """check_dirty_tree() git status guard (migrated 8.T(d,e))."""

    def test_dirty_tree_raises_system_exit(self) -> None:
        from scripts.rotate_pins import check_dirty_tree

        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=" M core/hydration.py\n", stderr=""
        )
        with pytest.raises(SystemExit), patch("subprocess.run", return_value=mock_result):
            check_dirty_tree()

    def test_clean_tree_returns_none(self) -> None:
        from scripts.rotate_pins import check_dirty_tree

        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            check_dirty_tree()  # should not raise


class TestMainDryRun:
    """main(["--dry-run"]) with drift — no file writes (migrated 8.T(g))."""

    def test_dry_run_with_drift_returns_0(self) -> None:
        from scripts.rotate_pins import DriftEntry, main

        drift_entry: DriftEntry = {
            "kind": "image",
            "key": "coredns",
            "old": "sha256:aaa",
            "new": "sha256:bbb",
            "verification_method": "none",
        }

        def fake_resolve(kind: str) -> list[DriftEntry]:
            return [drift_entry] if kind == "image" else []

        with (
            patch("scripts.rotate_pins.resolve_drift", side_effect=fake_resolve),
            patch("builtins.open", side_effect=AssertionError("should not write files")),
        ):
            result = main(["--dry-run"])
            assert result == 0

    def test_no_drift_returns_0(self) -> None:
        from scripts.rotate_pins import main

        with patch("scripts.rotate_pins.resolve_drift", return_value=[]):
            assert main([]) == 0


class TestMainAutoCommit:
    """main(["--auto-commit"]) verification + commit gating (migrated 8.T(h))."""

    def test_auto_commit_refuses_on_verification_failure(self) -> None:
        from scripts.rotate_pins import DriftEntry, main

        drift_entry: DriftEntry = {
            "kind": "image",
            "key": "wolfi_base",
            "old": "sha256:aaa",
            "new": "sha256:bbb",
            "verification_method": "cosign-keyless",
        }

        def fake_resolve(kind: str) -> list[DriftEntry]:
            return [drift_entry] if kind == "image" else []

        def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            cmd_str = " ".join(cmd)
            if "status" in cmd_str and "porcelain" in cmd_str:
                return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            if "cosign" in cmd_str or "verify" in cmd_str:
                return subprocess.CompletedProcess(
                    args=[], returncode=1, stdout="", stderr="verification failed"
                )
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with (
            patch("scripts.rotate_pins.resolve_drift", side_effect=fake_resolve),
            patch("subprocess.run", side_effect=fake_run),
        ):
            result = main(["--auto-commit"])
            assert result != 0

    def test_auto_commit_success_patches_and_commits(self) -> None:
        from scripts.rotate_pins import DriftEntry, main

        drift_entry: DriftEntry = {
            "kind": "image",
            "key": "coredns",  # "none" verification → passes
            "old": "sha256:aaa",
            "new": "sha256:bbb",
            "verification_method": "none",
        }

        def fake_resolve(kind: str) -> list[DriftEntry]:
            return [drift_entry] if kind == "image" else []

        run_calls: list[list[str]] = []

        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            cmd = args[0] if args else kwargs.get("args", [])
            run_calls.append(list(cmd))
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with (
            patch("scripts.rotate_pins.resolve_drift", side_effect=fake_resolve),
            patch("scripts.rotate_pins._patch_hydration") as patch_mock,
            patch("subprocess.run", side_effect=fake_run),
        ):
            result = main(["--auto-commit"])

        assert result == 0
        patch_mock.assert_called_once()
        commit_cmds = [c for c in run_calls if "commit" in c]
        assert commit_cmds, "expected a git commit invocation"
        commit_msg = commit_cmds[0][commit_cmds[0].index("-m") + 1]
        assert commit_msg == "chore(deps): rotate pins (coredns)"
