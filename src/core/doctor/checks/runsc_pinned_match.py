"""runsc pinned-match doctor check (spec "runsc Pinned Match Check").

Read-only verification that ``/usr/local/libexec/sandbox-ai/runsc`` matches
``BINARY_REGISTRY["runsc"].sha512``. Routes through
``core.binary_install.verify_only`` (read-only; no network) so the doctor check
and setup's L6a install phase share the single drift-classification logic.

Verdicts (spec):

- ``match`` → PASS with the truncated installed sha;
- ``absent`` → SKIP with the install-setup remediation;
- ``drift`` → WARN with both shas and the ``--update-runsc`` remediation.
"""

from __future__ import annotations

from core import binary_install
from core.doctor.types import CheckResult
from core.host_config import MachinectlAuth, minimal_host_config

# Truncation length for sha512 values shown in operator-facing detail strings.
_SHA_PREFIX = 16


def check_runsc_pinned_match(
    user: str, distro: str | None, auth_mode: MachinectlAuth = MachinectlAuth.SUDO
) -> CheckResult:
    """Verify the installed runsc sha512 matches the pinned registry value.

    Invokes ``core.binary_install.verify_only`` (read-only, no network). The
    reserved install path is root-owned and host-independent; ``host_config``
    is threaded only for caller uniformity.
    """
    del distro
    host_config = minimal_host_config(user, auth_mode)
    result = binary_install.verify_only("runsc", host_config)

    if result.status == "match":
        installed = result.installed_sha or ""
        return CheckResult(
            status="pass",
            name="runsc pinned match",
            detail=f"runsc matches pinned sha ({installed[:_SHA_PREFIX]}…)",
        )

    if result.status == "absent":
        return CheckResult(
            status="skip",
            name="runsc pinned match",
            detail="runsc not installed; run 'sudo sandbox setup' to install",
            remediation="run 'sudo sandbox setup' to install runsc",
        )

    installed = result.installed_sha or ""
    return CheckResult(
        status="warn",
        name="runsc pinned match",
        detail=(
            f"runsc drift: installed sha {installed[:_SHA_PREFIX]}…, "
            f"pinned sha {result.pinned_sha[:_SHA_PREFIX]}…. "
            f"Run 'sudo sandbox setup --update-runsc' to apply."
        ),
        remediation="run 'sudo sandbox setup --update-runsc' to apply the pinned version",
    )


__all__ = ["check_runsc_pinned_match"]
