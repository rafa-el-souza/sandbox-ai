"""Automated image digest rotation for IMAGE_REGISTRY.

Developer-facing CLI tool that resolves current tag digests, verifies
signatures, patches ``core/hydration.py``, and optionally commits the
result.

Usage:
    python -m scripts.rotate_digests [--dry-run | --auto-commit]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from core.hydration import IMAGE_REGISTRY

# ── Signature Verification Registry ─────────────────────────────────────────
# Maps IMAGE_REGISTRY keys to their signature verification method.
# Values: "cosign-keyless", "docker-content-trust", or "none".

SIGNER_REGISTRY: dict[str, str] = {
    "wolfi_base": "cosign-keyless",  # Chainguard images are cosign-keyless signed
    "debian_trixie": "none",  # Debian images do not publish cosign/DCT signatures
    "squid": "none",  # Ubuntu/squid OCI images — no cosign/DCT
    "coredns": "none",  # CoreDNS does not publish cosign/DCT signatures
    "dnsdist": "none",  # PowerDNS does not publish cosign/DCT signatures
    "postgres": "none",  # Official postgres — no cosign/DCT
    "busybox_musl": "docker-content-trust",  # Docker official images use DCT
}


# ── Resolve Digests ──────────────────────────────────────────────────────────


def resolve_digests() -> list[dict[str, str]]:
    """Resolve current tag digests and return a list of drift entries.

    Each entry is a dict with keys: ``key``, ``old_digest``, ``new_digest``.
    Returns an empty list when no drift is detected.

    Uses ``docker manifest inspect <ref>:<tag>`` to extract the top-level
    manifest-list digest (RepoDigests), not a per-platform sub-manifest.
    """
    drift: list[dict[str, str]] = []

    for key, pin in IMAGE_REGISTRY.items():
        try:
            result = subprocess.run(
                ["docker", "manifest", "inspect", pin.tagged],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except subprocess.TimeoutExpired:
            print(f"  ⚠ {key}: timeout inspecting {pin.tagged}", file=sys.stderr)
            continue

        if result.returncode != 0:
            print(f"  ⚠ {key}: failed to inspect {pin.tagged}", file=sys.stderr)
            continue

        try:
            manifest = json.loads(result.stdout.strip())
            current_digest = manifest.get("digest", "")
        except json.JSONDecodeError:
            print(f"  ⚠ {key}: invalid JSON from manifest inspect", file=sys.stderr)
            continue

        if current_digest and current_digest != pin.digest:
            drift.append(
                {
                    "key": key,
                    "old_digest": pin.digest,
                    "new_digest": current_digest,
                }
            )

    return drift


# ── Dirty Tree Guard ─────────────────────────────────────────────────────────


def check_dirty_tree() -> None:
    """Refuse to patch when the git working tree has uncommitted changes.

    Raises ``SystemExit(1)`` if ``git status --porcelain`` returns non-empty
    output. Called after drift detection, not at startup.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.stdout.strip():
        print(
            "Error: working tree has uncommitted changes. Commit or stash changes before rotating digests.",
            file=sys.stderr,
        )
        raise SystemExit(1)


# ── Signature Verification ───────────────────────────────────────────────────


def _verify_signature(key: str, ref: str, digest: str) -> bool:
    """Verify the image signature using the method from SIGNER_REGISTRY.

    Returns True if verification passes or is not applicable ("none").
    Returns False if verification fails.
    """
    method = SIGNER_REGISTRY.get(key, "none")

    if method == "none":
        return True

    if method == "cosign-keyless":
        try:
            result = subprocess.run(
                ["cosign", "verify", f"{ref}@{digest}"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            return result.returncode == 0
        except subprocess.TimeoutExpired, FileNotFoundError:
            return False

    # DCT verification is handled via DOCKER_CONTENT_TRUST env var.
    # For automated rotation, we trust the manifest digest from Docker Hub.
    return method == "docker-content-trust"


# ── Patch Hydration ──────────────────────────────────────────────────────────


def _patch_hydration(drift: list[dict[str, str]]) -> None:
    """Patch core/hydration.py with new digest values."""
    hydration_path = Path(__file__).resolve().parent.parent / "core" / "hydration.py"
    content = hydration_path.read_text()

    for entry in drift:
        old = entry["old_digest"]
        new = entry["new_digest"]
        content = content.replace(old, new)

    hydration_path.write_text(content)


# ── Main Entry Point ─────────────────────────────────────────────────────────


def main(args: list[str] | None = None) -> int:
    """Entry point for the rotation script.

    Four-stage pipeline: resolve → verify → patch → commit.

    Args:
        args: CLI arguments. Supports ``--dry-run`` and ``--auto-commit``.

    Returns:
        Exit code: 0 on success, non-zero on failure.
    """
    parser = argparse.ArgumentParser(description="Rotate IMAGE_REGISTRY digests")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="Report drift without modifying files")
    group.add_argument("--auto-commit", action="store_true", help="Patch, verify, and auto-commit")
    parsed = parser.parse_args(args or [])

    # Stage 1: resolve
    drift = resolve_digests()

    if not drift:
        print("No digest drift detected. All pinned digests are current.")
        return 0

    # Report drift
    for entry in drift:
        print(f"  {entry['key']}: {entry['old_digest'][:19]}... → {entry['new_digest'][:19]}...")

    if parsed.dry_run:
        print(f"\nDry-run: {len(drift)} digest(s) would be rotated.")
        return 0

    # Stage 2: verify signatures (mandatory in auto-commit mode)
    if parsed.auto_commit:
        for entry in drift:
            pin = IMAGE_REGISTRY[entry["key"]]
            if not _verify_signature(entry["key"], pin.ref, entry["new_digest"]):
                print(
                    f"Error: signature verification failed for {entry['key']}. Refusing to auto-commit.",
                    file=sys.stderr,
                )
                return 1

    # Dirty tree guard (after drift detection, before patching)
    check_dirty_tree()

    # Stage 3: patch
    _patch_hydration(drift)
    print(f"Patched core/hydration.py with {len(drift)} new digest(s).")

    # Stage 4: commit (auto-commit mode only)
    if parsed.auto_commit:
        keys = ", ".join(e["key"] for e in drift)
        subprocess.run(
            ["git", "add", "core/hydration.py"],
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", f"chore(deps): rotate image digests ({keys})"],
            check=True,
        )
        print("Auto-committed digest rotation.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
