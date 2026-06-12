# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Dispatcher sha-drift doctor check (spec "Dispatcher Sha Drift Check").

Compares the on-disk dispatcher binary against the manifest written by setup's
L6.5 phase at ``/usr/local/libexec/sandbox-ai/dispatcher.manifest.json`` (the
host-plane path alongside the binary, F-021; schema: ``compiled_sha512`` /
``source_bundle_sha512`` / ``compile_timestamp`` — "Dispatcher Manifest Schema"
requirement). The path is imported (``_manifest_path``) from L6.5 — single
source — so this check and the setup phase can never disagree on its location.

Two independent integrity dimensions:

1. **Binary integrity** — sha512 of ``/usr/local/libexec/sandbox-ai/dispatch``
   == ``manifest.compiled_sha512``.
2. **Source freshness** — the current source-bundle sha512 ==
   ``manifest.source_bundle_sha512``.

The source-bundle sha512 is computed **by reusing setup L6.5's single-source
helper** ``core.setup.l65_dispatcher._source_bundle_sha512`` (orchestrator
decision 1 / F-011). That helper derives the file set from
``core.dispatch._DISPATCH_SOURCE_ENTRIES`` (currently ``main.go``,
``main_test.go``, ``go.mod``, ``go.sum``, ``vendor``, ``fixtures``) — NOT the
narrower ``{go.mod, go.sum, main.go, vendor/**}`` subset, which omits
``main_test.go``/``fixtures/`` and would let a Python↔Go parity-fixture change
move compile success without tripping the drift hash. Reusing the exact helper
guarantees the doctor check and L6.5 can never disagree on what "the source
bundle" is.

Verdicts (spec):

- both match → PASS (truncated shas + compile timestamp);
- binary OR manifest absent → SKIP (install-setup hint);
- binary sha ≠ ``compiled_sha512`` → WARN (tamper / hand-replacement hint);
- source sha ≠ ``source_bundle_sha512`` → WARN (wheel-upgrade hint).
"""

from __future__ import annotations

from pathlib import Path

from core.dispatch import _DISPATCH_BINARY
from core.doctor.types import CheckResult

# Reuse — never re-implement — setup L6.5's single-source manifest helpers
# (orchestrator decision 1 / F-011: a divergent file set here = false WARN).
from core.setup.l65_dispatcher import _file_sha512, _manifest_path, _read_manifest, _source_bundle_sha512

# Truncation length for sha512 values shown in operator-facing detail strings.
_SHA_PREFIX = 16


def check_dispatcher_sha_drift(user: str, distro: str | None) -> CheckResult:
    """Compare the on-disk dispatcher binary + source bundle against the manifest.

    Pure read-only filesystem probe. The source-bundle sha512 is computed via
    the reused L6.5 helper so the doctor check and the setup phase cannot
    disagree on the compile-input file set.
    """
    del user, distro

    manifest = _read_manifest()
    if manifest is None:
        return CheckResult(
            status="skip",
            name="dispatcher sha drift",
            detail=f"dispatcher manifest absent at {_manifest_path()}",
            remediation="run 'sudo sandbox setup' to install the dispatcher",
        )

    binary_sha = _file_sha512(Path(_DISPATCH_BINARY))
    if binary_sha is None:
        return CheckResult(
            status="skip",
            name="dispatcher sha drift",
            detail=f"dispatcher binary absent at {_DISPATCH_BINARY}",
            remediation="run 'sudo sandbox setup' to install the dispatcher",
        )

    recorded_compiled = manifest.get("compiled_sha512")
    if recorded_compiled != binary_sha:
        return CheckResult(
            status="warn",
            name="dispatcher sha drift",
            detail=(
                f"dispatcher binary differs from setup's recorded sha "
                f"(recorded {str(recorded_compiled)[:_SHA_PREFIX]}…, "
                f"on-disk {binary_sha[:_SHA_PREFIX]}…). Re-run "
                f"'sudo sandbox setup' to refresh, or investigate tampering."
            ),
            remediation="run 'sudo sandbox setup' to refresh the dispatcher",
        )

    current_source = _source_bundle_sha512()
    recorded_source = manifest.get("source_bundle_sha512")
    if recorded_source != current_source:
        return CheckResult(
            status="warn",
            name="dispatcher sha drift",
            detail=(
                f"dispatcher binary was compiled from an older source bundle "
                f"(wheel upgrade since last setup: recorded "
                f"{str(recorded_source)[:_SHA_PREFIX]}…, current "
                f"{current_source[:_SHA_PREFIX]}…). Re-run 'sudo sandbox setup' "
                f"to recompile against current source."
            ),
            remediation="run 'sudo sandbox setup' to recompile against current source",
        )

    timestamp = manifest.get("compile_timestamp", "unknown")
    return CheckResult(
        status="pass",
        name="dispatcher sha drift",
        detail=(
            f"dispatcher binary + source bundle match the manifest "
            f"(compiled {binary_sha[:_SHA_PREFIX]}…, "
            f"source {current_source[:_SHA_PREFIX]}…, compiled at {timestamp})"
        ),
    )


__all__ = ["check_dispatcher_sha_drift"]
