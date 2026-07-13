# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""L6a — runsc install (shape #3: install-if-absent, drift-no-overwrite).

runsc is its **own** phase, distinct from L2's host prerequisites (R1): it has
an upstream-pinned lifecycle (``BINARY_REGISTRY["runsc"]``) unlike the
host-prereq mutations. It runs after L6, before L6.5 — independent of the
sandbox user / dockerd (the reserved install path is root-owned and
host-independent), identity ``ROOT``.

Shape #3 (the "gVisor Runsc Drift and Update Offering" contract):

- absent at ``/usr/local/libexec/sandbox-ai/runsc`` → ``MISSING``; act
  installs the pinned binary;
- present + sha512 == ``BINARY_REGISTRY["runsc"].sha512`` →
  ``ALREADY_CORRECT``;
- present + sha512 drift → **do NOT overwrite**. The probe returns ``DRIFT``
  with a detail naming both shas + the ``sudo sandbox setup --update-runsc``
  remediation, and ``act`` (still drift-skipping by default) is a no-op that
  re-emits that finalization-summary line. ``--update-runsc`` re-runs this
  phase with the module-level force toggle set, which overrides the drift-skip
  via ``install_pinned(..., force=True)``.

All install mechanics (download + sha512 verify + atomic install + ``chattr
+i``) live in ``core.binary_install``; this phase only owns the probe/act
policy.

**L6a is separate-user only** (``applies_in`` excludes operator-rootless,
D5a/O3): the root-owned runsc install is a host-root mutation, so in
operator-rootless (where the apply pass runs unprivileged as the operator) it is
owned by the ``host_batch`` ``RUNSC`` item + ``_bootstrap-host`` escalation
(``host_batch._apply_runsc`` drives the same ``core.binary_install.install_pinned``
mechanism). The runner reports the phase ``skipped (operator-rootless)`` in both
passes, joining L1/L2/L2a and the M2 crossing-only phases. (``--update-runsc``:
on a separate-user host this phase runs as today. operator-rootless has NO L6a
subset to run — instead the runsc lifecycle (install AND pin-update) is owned by
the ``host_batch`` ``RUNSC`` item: the classifier selects it whenever the on-disk
sha is absent or drifts, and ``host_batch._apply_runsc`` installs with
``force=True`` (unsealing the immutable target on a drift re-install). So a normal
``sandbox setup`` re-run converges runsc to the pin under the one escalation, and
``--update-runsc`` routes to that op-rootless body — no privileged operator-side
L6a step is needed or possible.)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.binary_install import SHA_DISPLAY_PREFIX, detect_drift, install_pinned
from core.host_config import DockerExecutionMode
from core.setup.phase_runner import Identity, Phase, PhaseResult

if TYPE_CHECKING:
    from core.setup.phase_runner import SetupContext

_BINARY_NAME = "runsc"


class _ForceState:
    """Process-local toggle: ``--update-runsc`` overrides the drift-skip.

    A tiny mutable holder rather than a module global rebind so the CLI
    surface (Group 8) can flip it (``set_force_update(True)``) before invoking
    only this phase, and the default — drift is *reported, not overwritten* —
    is preserved for the normal ceremony.
    """

    enabled: bool = False


_FORCE = _ForceState()


def set_force_update(enabled: bool) -> None:
    """Set the ``--update-runsc`` force toggle (CLI surface, Group 8)."""
    _FORCE.enabled = enabled


def _probe(ctx: SetupContext) -> tuple[PhaseResult, str]:
    """Content-aware probe via ``core.binary_install.detect_drift``."""
    drift = detect_drift(_BINARY_NAME, ctx.host_config)
    if drift.status == "absent":
        return (
            PhaseResult.MISSING,
            "runsc absent at /usr/local/libexec/sandbox-ai/runsc; will install",
        )
    if drift.status == "match":
        return (
            PhaseResult.ALREADY_CORRECT,
            "runsc present and matches the pinned sha512",
        )
    installed = drift.installed_sha or "?"
    detail = (
        f"runsc version drift: installed sha {installed[:SHA_DISPLAY_PREFIX]}…, "
        f"pinned sha {drift.pinned_sha[:SHA_DISPLAY_PREFIX]}…. "
        "To update: sudo sandbox setup --update-runsc"
    )
    if _FORCE.enabled:
        return PhaseResult.DRIFT, f"{detail} (--update-runsc: will overwrite)"
    return PhaseResult.DRIFT, detail


def _act(ctx: SetupContext) -> str:
    """Install the pinned binary; drift-skip unless ``--update-runsc`` is set.

    On a drift with no force, this is deliberately a no-op (the drift is
    surfaced to the finalization summary by the probe detail) so a re-run does
    NOT silently overwrite a hand-rolled runsc. With force set, the existing
    immutable target is unsealed + replaced by ``install_pinned(force=True)``.
    """
    host_config = ctx.host_config
    drift = detect_drift(_BINARY_NAME, host_config)
    if drift.status == "drift" and not _FORCE.enabled:
        return (
            f"runsc drift left in place (installed sha {drift.installed_sha}, "
            f"pinned sha {drift.pinned_sha}); run --update-runsc to overwrite"
        )
    install_pinned(_BINARY_NAME, host_config, force=_FORCE.enabled)
    return "runsc installed at /usr/local/libexec/sandbox-ai/runsc"


def _reverify(ctx: SetupContext) -> bool:
    """Confirm the on-disk runsc now matches the pinned sha512.

    On a default drift-skip ``act`` (force off) the binary still mismatches —
    reverify returns ``False`` so the runner classifies the phase a FAIL with
    the drift detail rather than a silent pass. ``--update-runsc`` makes the
    install happen, so reverify then sees ``match``.
    """
    return detect_drift(_BINARY_NAME, ctx.host_config).status == "match"


PHASE = Phase(
    id="l6a",
    name="runsc install (gVisor pinned binary)",
    identity=Identity.ROOT,
    probe=_probe,
    act=_act,
    reverify=_reverify,
    depends_on=("l6",),
    # separate-user only. The root-owned runsc install is a host-root mutation;
    # in operator-rootless it is owned by the ``host_batch`` ``RUNSC`` item +
    # ``_bootstrap-host`` escalation (design D5a / O3). Gated OUT (reported
    # ``skipped`` in both passes), mirroring L1/L2/L2a and the M2 crossing-only
    # phases.
    applies_in=frozenset({DockerExecutionMode.SEPARATE_USER}),
)
