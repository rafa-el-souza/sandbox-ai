# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Supply-chain doctor check: image digest pin verification."""

from __future__ import annotations

import json

from core import dispatch
from core.doctor.types import CheckResult
from core.host_config import (
    DEFAULT_PROVISIONING_MODE,
    DockerExecutionMode,
    MachinectlAuth,
    minimal_host_config,
)


def check_image_digests(
    user: str,
    distro: str | None,
    mode: DockerExecutionMode = DEFAULT_PROVISIONING_MODE,
) -> CheckResult:
    """Check that all IMAGE_REGISTRY digests are resolvable against container registries.

    Iterates IMAGE_REGISTRY and runs ``docker manifest inspect <ref>@<digest>``
    through the root-owned dispatcher boundary for each entry. Returns PASS if all resolve, FAIL if any are
    stale, or SKIP if the registry is unreachable (timeout/network error).

    Additionally checks for tag drift (upstream tag re-pushed with a different
    digest) and reports it as informational detail.
    """
    from core.hydration import IMAGE_REGISTRY

    stale: list[str] = []
    drift: list[str] = []

    host_config = minimal_host_config(user, MachinectlAuth.SUDO, mode)
    for key, pin in IMAGE_REGISTRY.items():
        pinned_outcome = dispatch.probe(
            "docker-manifest-inspect", [pin.pinned], host_config, timeout=2
        )
        if pinned_outcome.timed_out:
            return CheckResult(
                status="skip",
                name="image digests",
                detail="Registry unreachable (timeout during manifest inspection)",
            )
        if not pinned_outcome.ok:
            stale.append(key)
            continue

        # Best-effort upstream tag-drift detection; ``pin.tagged`` is an
        # ``IMAGE_REGISTRY`` member (design Q7) so it validates and routes
        # through the typed ``docker-manifest-inspect`` op.
        tag_outcome = dispatch.probe(
            "docker-manifest-inspect", [pin.tagged], host_config, timeout=2
        )
        if tag_outcome.ok:
            try:
                manifest = json.loads(tag_outcome.stdout.strip())
                tag_digest = manifest.get("digest", "")
                if tag_digest and tag_digest != pin.digest:
                    drift.append(key)
            except json.JSONDecodeError:
                pass

    if stale:
        return CheckResult(
            status="fail",
            name="image digests",
            detail=f"Stale digests: {', '.join(stale)}",
            remediation="Run scripts/rotate_pins.py to update pinned digests",
        )

    count = len(IMAGE_REGISTRY)
    detail = f"All {count} pinned digests verified"
    if drift:
        detail += f" (tag drift detected: {', '.join(drift)})"

    return CheckResult(
        status="pass",
        name="image digests",
        detail=detail,
    )


__all__ = ["check_image_digests"]
