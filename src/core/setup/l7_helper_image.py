# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""L7 — helper image pre-pull (content-aware on the pinned digest).

Pre-pulls the disposable-helper image so the first ``sandbox start`` does not
pay the pull latency. The image is the digest-pinned
``IMAGE_REGISTRY["busybox_musl"].pinned`` (``busybox@sha256:…``). The pull
crosses to the daemon owner via :func:`daemon_owner_crossing` — ``machinectl_cmd``
into the sandbox user in separate-user (identity ``SANDBOX``, sentinel on, since
``machinectl shell`` masks the inner exit), an empty LOCAL prefix in
operator-rootless (sentinel off; setup runs as the operator, so the pull is a
plain local ``docker`` subprocess in the operator's session). The rootless daemon
installed by L5 (owned by the daemon owner) owns the image cache.

Content-aware probe (design D10): the converged state is *the pinned digest is
present in the sandbox user's local image store*. ``docker image inspect`` on
the exact ``ref@sha256:…`` succeeds iff that digest is cached; the probe
crosses it and branches on the recovered inner exit. Present →
``ALREADY_CORRECT``; absent → ``MISSING`` (the act pulls it). A bare
file/tag-exists probe is insufficient: a tag could point at a different digest
after an upstream rotation, so the probe pins the *digest*, not a tag.
``docker pull`` on an already-cached digest is itself a no-op, so the act is
idempotent regardless.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.exceptions import SandboxExecutionError
from core.executor import Executor
from core.host_config import is_operator_rootless
from core.hydration import IMAGE_REGISTRY
from core.setup.phase_runner import (
    Identity,
    Phase,
    PhaseResult,
    daemon_owner_crossing,
)

if TYPE_CHECKING:
    from core.setup.phase_runner import SetupContext

_HELPER_PIN = IMAGE_REGISTRY["busybox_musl"]
_HELPER_REF = _HELPER_PIN.pinned
_HELPER_TAGGED = _HELPER_PIN.tagged


def _crossed(ctx: SetupContext, inner: str) -> list[str]:
    """Build the daemon-owner crossing argv for an inner ``bash -c`` string.

    ``machinectl_cmd`` into the sandbox user in separate-user; an empty LOCAL
    prefix in operator-rootless (:func:`daemon_owner_crossing`).
    """
    return [*daemon_owner_crossing(ctx), "/bin/bash", "-c", inner]


def _ref_present(ctx: SetupContext, ref: str) -> bool:
    """``True`` iff ``docker image inspect <ref>`` succeeds (crossed).

    Sentinel is on in separate-user (``machinectl shell`` masks the inner exit)
    and off in operator-rootless (a local command's exit is not masked).
    """
    cmd = _crossed(ctx, f"docker image inspect {ref}")
    try:
        Executor().run(cmd, sentinel=not is_operator_rootless(ctx.host_config))
    except SandboxExecutionError:
        return False
    return True


def _digest_present(ctx: SetupContext) -> bool:
    """``True`` iff the *pinned digest* is in the daemon owner's image store."""
    return _ref_present(ctx, _HELPER_REF)


def _probe(ctx: SetupContext) -> tuple[PhaseResult, str]:
    """Content-aware probe on the pinned helper-image **digest**.

    The phase's identity is the *digest*, not the tag. Three observable
    states:

    - pinned digest present → ``ALREADY_CORRECT``;
    - pinned digest absent but the ``busybox:<tag>`` tag resolves locally to
      *some other* digest (an upstream rotation moved the tag) → ``DRIFT``
      (the act re-pulls the pinned digest — this is the content-aware signal,
      not a bare presence check);
    - no matching image at all → ``MISSING``.
    """
    if _digest_present(ctx):
        return (
            PhaseResult.ALREADY_CORRECT,
            f"helper image {_HELPER_REF} already cached",
        )
    if _ref_present(ctx, _HELPER_TAGGED):
        return (
            PhaseResult.DRIFT,
            f"{_HELPER_TAGGED} present but not at the pinned digest "
            f"{_HELPER_REF}; will re-pull the pinned digest",
        )
    return (
        PhaseResult.MISSING,
        f"helper image {_HELPER_REF} not cached; will pull",
    )


def _act(ctx: SetupContext) -> str:
    """``docker pull`` the pinned digest (idempotent on an already-cached one)."""
    cmd = _crossed(ctx, f"docker pull {_HELPER_REF}")
    Executor().run(cmd, sentinel=not is_operator_rootless(ctx.host_config))
    return f"helper image {_HELPER_REF} pulled"


def _reverify(ctx: SetupContext) -> bool:
    """Confirm the pinned digest is now present in the local image store."""
    return _digest_present(ctx)


PHASE = Phase(
    id="l7",
    name="helper image pre-pull (busybox-musl pinned digest)",
    identity=Identity.SANDBOX,
    probe=_probe,
    act=_act,
    reverify=_reverify,
    depends_on=("l65",),
)
