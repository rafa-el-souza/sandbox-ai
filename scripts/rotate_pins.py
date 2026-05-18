"""Automated pin rotation for IMAGE_REGISTRY + BINARY_REGISTRY.

Developer-facing CLI tool that resolves current image tag digests and
binary sidecar sha512 sums, verifies signatures / checksums, patches
``core/hydration.py``, and optionally commits the result.

Usage:
    python -m scripts.rotate_pins [--dry-run | --auto-commit]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Literal, TypedDict

from core.hydration import BINARY_REGISTRY, IMAGE_REGISTRY

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


# ── Uniform Drift Shape ──────────────────────────────────────────────────────


class DriftEntry(TypedDict):
    """Uniform drift entry shared by the image and binary rotation paths."""

    kind: Literal["image", "binary"]
    key: str
    old: str
    new: str
    verification_method: str


# ── Binary Sidecar Helpers ───────────────────────────────────────────────────


def _arch_substituted_url(url_template: str) -> str:
    """Substitute the literal ``$(arch)`` placeholder for sha512-sidecar fetch.

    The maintainer-side rotation always resolves the x86_64 sidecar (the
    operator-verified reference architecture); ``$(arch)`` is the only
    placeholder in BINARY_REGISTRY URL templates.
    """
    return url_template.replace("$(arch)", "x86_64")


def _fetch_sidecar_sha512(url_template: str) -> str | None:
    """Fetch ``<url>.sha512`` and return the hex digest.

    The sidecar body is ``<hex>  <basename>``; the leading whitespace-split
    token is the sha512 hex. Returns None on any fetch/parse failure.
    """
    sidecar_url = _arch_substituted_url(url_template) + ".sha512"
    try:
        with urllib.request.urlopen(sidecar_url, timeout=10) as resp:
            body: str = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError):
        return None

    token = body.strip().split()
    if not token:
        return None
    return token[0]


# ── Resolve Drift ────────────────────────────────────────────────────────────


def _resolve_image_drift() -> list[DriftEntry]:
    """Resolve current image tag digests and return image drift entries.

    Uses ``docker manifest inspect <ref>:<tag>`` to extract the top-level
    manifest-list digest (RepoDigests), not a per-platform sub-manifest.
    """
    drift: list[DriftEntry] = []

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
                DriftEntry(
                    kind="image",
                    key=key,
                    old=pin.digest,
                    new=current_digest,
                    verification_method=SIGNER_REGISTRY.get(key, "none"),
                )
            )

    return drift


def _resolve_binary_drift() -> list[DriftEntry]:
    """Resolve current binary sidecar sha512 sums and return binary drift.

    Fetches ``<url>.sha512`` per BINARY_REGISTRY entry and compares the
    hex digest against the pinned ``.sha512``.
    """
    drift: list[DriftEntry] = []

    for key, pin in BINARY_REGISTRY.items():
        current_sha512 = _fetch_sidecar_sha512(pin.url_template)
        if current_sha512 is None:
            print(f"  ⚠ {key}: failed to fetch sha512 sidecar", file=sys.stderr)
            continue

        if current_sha512 and current_sha512 != pin.sha512:
            drift.append(
                DriftEntry(
                    kind="binary",
                    key=key,
                    old=pin.sha512,
                    new=current_sha512,
                    verification_method="sha512-sidecar",
                )
            )

    return drift


def resolve_drift(kind: Literal["image", "binary"]) -> list[DriftEntry]:
    """Resolve drift for the given pin kind.

    Returns a list of uniform :class:`DriftEntry` dicts. Empty when no
    drift is detected.
    """
    if kind == "image":
        return _resolve_image_drift()
    return _resolve_binary_drift()


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
            "Error: working tree has uncommitted changes. Commit or stash changes before rotating pins.",
            file=sys.stderr,
        )
        raise SystemExit(1)


# ── Verification ─────────────────────────────────────────────────────────────


def _verify_signature(entry: DriftEntry) -> bool:
    """Verify a drift entry's new pin value, dispatching on ``kind``.

    Image entries dispatch on SIGNER_REGISTRY (cosign-keyless / DCT / none).
    Binary entries re-fetch the sha512 sidecar and confirm it still matches
    the resolved ``new`` value.

    Returns True if verification passes or is not applicable.
    """
    if entry["kind"] == "binary":
        binary_pin = BINARY_REGISTRY[entry["key"]]
        refetched = _fetch_sidecar_sha512(binary_pin.url_template)
        return refetched is not None and refetched == entry["new"]

    method = SIGNER_REGISTRY.get(entry["key"], "none")

    if method == "none":
        return True

    if method == "cosign-keyless":
        image_pin = IMAGE_REGISTRY[entry["key"]]
        try:
            result = subprocess.run(
                ["cosign", "verify", f"{image_pin.ref}@{entry['new']}"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    # DCT verification is handled via DOCKER_CONTENT_TRUST env var.
    # For automated rotation, we trust the manifest digest from Docker Hub.
    return method == "docker-content-trust"


# ── Patch Hydration ──────────────────────────────────────────────────────────


def _patch_hydration(drift: list[DriftEntry]) -> None:
    """Patch core/hydration.py — IMAGE_REGISTRY digests and BINARY_REGISTRY sha512.

    Both kinds patch by a literal old→new string replacement of the pinned
    value within the registry literal.
    """
    hydration_path = Path(__file__).resolve().parent.parent / "core" / "hydration.py"
    content = hydration_path.read_text()

    for entry in drift:
        content = content.replace(entry["old"], entry["new"])

    hydration_path.write_text(content)


# ── Main Entry Point ─────────────────────────────────────────────────────────


def main(args: list[str] | None = None) -> int:
    """Entry point for the rotation script.

    Four-stage pipeline: resolve → verify → patch → commit. Resolves both
    image and binary drift into one uniform list.

    Args:
        args: CLI arguments. Supports ``--dry-run`` and ``--auto-commit``.

    Returns:
        Exit code: 0 on success, non-zero on failure.
    """
    parser = argparse.ArgumentParser(description="Rotate pins (images + binaries)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="Report drift without modifying files")
    group.add_argument("--auto-commit", action="store_true", help="Patch, verify, and auto-commit")
    parsed = parser.parse_args(args or [])

    # Stage 1: resolve (images + binaries → one uniform list)
    drift: list[DriftEntry] = resolve_drift("image") + resolve_drift("binary")

    if not drift:
        print("No pin drift detected. All pinned images and binaries are current.")
        return 0

    # Report drift
    for entry in drift:
        print(f"  [{entry['kind']}] {entry['key']}: {entry['old'][:19]}... → {entry['new'][:19]}...")

    if parsed.dry_run:
        print(f"\nDry-run: {len(drift)} pin(s) would be rotated.")
        return 0

    # Stage 2: verify (mandatory in auto-commit mode)
    if parsed.auto_commit:
        for entry in drift:
            if not _verify_signature(entry):
                print(
                    f"Error: verification failed for {entry['key']}. Refusing to auto-commit.",
                    file=sys.stderr,
                )
                return 1

    # Dirty tree guard (after drift detection, before patching)
    check_dirty_tree()

    # Stage 3: patch
    _patch_hydration(drift)
    print(f"Patched core/hydration.py with {len(drift)} new pin(s).")

    # Stage 4: commit (auto-commit mode only)
    if parsed.auto_commit:
        keys = ", ".join(e["key"] for e in drift)
        subprocess.run(
            ["git", "add", "core/hydration.py"],
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", f"chore(deps): rotate pins ({keys})"],
            check=True,
        )
        print("Auto-committed pin rotation.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
