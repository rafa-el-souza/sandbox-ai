"""Supply-chain doctor check: image digest pin verification."""

from __future__ import annotations

import json
import subprocess

from core.doctor.types import CheckResult
from core.host_config import MachinectlAuth, machinectl_cmd


def check_image_digests(user: str, distro: str | None, auth_mode: MachinectlAuth = MachinectlAuth.SUDO) -> CheckResult:
    """Check that all IMAGE_REGISTRY digests are resolvable against container registries.

    Iterates IMAGE_REGISTRY and runs ``docker manifest inspect <ref>@<digest>``
    via machinectl for each entry. Returns PASS if all resolve, FAIL if any are
    stale, or SKIP if the registry is unreachable (timeout/network error).

    Additionally checks for tag drift (upstream tag re-pushed with a different
    digest) and reports it as informational detail.
    """
    from core.hydration import IMAGE_REGISTRY

    stale: list[str] = []
    drift: list[str] = []

    mc_prefix = machinectl_cmd(user, auth_mode)
    for key, pin in IMAGE_REGISTRY.items():
        try:
            result = subprocess.run(
                [
                    *mc_prefix,
                    "/bin/bash",
                    "-c",
                    f"docker manifest inspect {pin.pinned}",
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return CheckResult(
                status="skip",
                name="image digests",
                detail="Registry unreachable (timeout during manifest inspection)",
            )

        if result.returncode != 0:
            stale.append(key)
            continue

        try:
            tag_result = subprocess.run(
                [
                    *mc_prefix,
                    "/bin/bash",
                    "-c",
                    f"docker manifest inspect {pin.tagged}",
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if tag_result.returncode == 0:
                try:
                    manifest = json.loads(tag_result.stdout.strip())
                    tag_digest = manifest.get("digest", "")
                    if tag_digest and tag_digest != pin.digest:
                        drift.append(key)
                except json.JSONDecodeError:
                    pass
        except subprocess.TimeoutExpired:
            pass  # Tag drift check is best-effort

    if stale:
        return CheckResult(
            status="fail",
            name="image digests",
            detail=f"Stale digests: {', '.join(stale)}",
            remediation="Run scripts/rotate_digests.py to update pinned digests",
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
