"""Tests for scripts/rotate_digests.py — Group 8.T RED.

Validates the automated image digest rotation script against the
spec requirements: SIGNER_REGISTRY, resolve_digests(), check_dirty_tree(),
and main() with --dry-run and --auto-commit modes.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import patch

import pytest


class TestRotateDigestsModule:
    """8.T(a): Module structure — callables and attributes exist."""

    def test_has_main(self) -> None:
        from scripts.rotate_digests import main

        assert callable(main)

    def test_has_signer_registry(self) -> None:
        from scripts.rotate_digests import SIGNER_REGISTRY

        assert isinstance(SIGNER_REGISTRY, dict)

    def test_has_resolve_digests(self) -> None:
        from scripts.rotate_digests import resolve_digests

        assert callable(resolve_digests)

    def test_has_check_dirty_tree(self) -> None:
        from scripts.rotate_digests import check_dirty_tree

        assert callable(check_dirty_tree)


class TestResolveDigests:
    """8.T(b,c): resolve_digests() drift detection."""

    def test_no_drift_returns_empty(self) -> None:
        """(b) When current tag digest matches pinned, drift list is empty."""
        from core.hydration import IMAGE_REGISTRY
        from scripts.rotate_digests import resolve_digests

        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            cmd = args[0] if args else kwargs.get("args", [])
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
            # Return matching digest for all tag probes — match on full tagged ref
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
            drift = resolve_digests()
            assert drift == []

    def test_drift_detected(self) -> None:
        """(c) When current tag digest differs from pinned, drift list contains entry."""
        from core.hydration import IMAGE_REGISTRY
        from scripts.rotate_digests import resolve_digests

        keys = list(IMAGE_REGISTRY.keys())
        new_digest = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            cmd = args[0] if args else kwargs.get("args", [])
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
            # Return different digest for the first key
            if IMAGE_REGISTRY[keys[0]].tagged in cmd_str:
                return subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=f'{{"digest": "{new_digest}"}}',
                    stderr="",
                )
            # Return matching digest for others
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
            drift = resolve_digests()
            assert len(drift) >= 1
            entry = drift[0]
            assert "old_digest" in entry or "old" in str(entry).lower()
            assert "new_digest" in entry or "new" in str(entry).lower()


class TestCheckDirtyTree:
    """8.T(d,e): check_dirty_tree() git status guard."""

    def test_dirty_tree_raises_system_exit(self) -> None:
        """(d) Non-empty git status raises SystemExit."""
        from scripts.rotate_digests import check_dirty_tree

        mock_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=" M core/hydration.py\n",
            stderr="",
        )
        with pytest.raises(SystemExit), patch("subprocess.run", return_value=mock_result):
            check_dirty_tree()

    def test_clean_tree_returns_none(self) -> None:
        """(e) Empty git status returns None."""
        from scripts.rotate_digests import check_dirty_tree

        mock_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )
        with patch("subprocess.run", return_value=mock_result):
            check_dirty_tree()  # should not raise


class TestSignerRegistry:
    """8.T(f): SIGNER_REGISTRY structure validation."""

    def test_signer_registry_values_valid(self) -> None:
        """(f) All SIGNER_REGISTRY values must be valid verification methods."""
        from scripts.rotate_digests import SIGNER_REGISTRY

        valid_methods = {"cosign-keyless", "docker-content-trust", "none"}
        assert isinstance(SIGNER_REGISTRY, dict)
        assert len(SIGNER_REGISTRY) == 7, f"Expected 7 entries, got {len(SIGNER_REGISTRY)}"
        for key, method in SIGNER_REGISTRY.items():
            assert isinstance(key, str)
            assert isinstance(method, str)
            assert method in valid_methods, f"{key}: {method!r} not in {valid_methods}"


class TestMainDryRun:
    """8.T(g): main(["--dry-run"]) with drift — no file writes."""

    def test_dry_run_with_drift_returns_0(self) -> None:
        """(g) Dry-run reports drift but writes no files and returns 0."""
        from scripts.rotate_digests import main

        drift_entry = {"key": "coredns", "old_digest": "sha256:aaa", "new_digest": "sha256:bbb"}

        with (
            patch("scripts.rotate_digests.resolve_digests", return_value=[drift_entry]),
            patch("builtins.open", side_effect=AssertionError("should not write files")),
        ):
            # main should NOT call open() to write anything
            result = main(["--dry-run"])
            assert result == 0


class TestMainAutoCommitRefuses:
    """8.T(h): main(["--auto-commit"]) with verification failure."""

    def test_auto_commit_refuses_on_verification_failure(self) -> None:
        """(h) Auto-commit exits non-zero when cosign verify fails."""
        from scripts.rotate_digests import main

        drift_entry = {"key": "wolfi_base", "old_digest": "sha256:aaa", "new_digest": "sha256:bbb"}

        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            cmd = args[0] if args else kwargs.get("args", [])
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
            # git status clean
            if "status" in cmd_str and "porcelain" in cmd_str:
                return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            # cosign verify fails
            if "cosign" in cmd_str or "verify" in cmd_str:
                return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="verification failed")
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with (
            patch("scripts.rotate_digests.resolve_digests", return_value=[drift_entry]),
            patch("subprocess.run", side_effect=fake_run),
        ):
            result = main(["--auto-commit"])
            assert result != 0
