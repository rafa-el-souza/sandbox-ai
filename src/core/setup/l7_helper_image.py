"""L7 — helper image pre-pull (content-aware on the pinned digest).

Pre-pulls the disposable-helper image so the first ``sandbox start`` does not
pay the pull latency. The image is the digest-pinned
``IMAGE_REGISTRY["busybox_musl"].pinned`` (``busybox@sha256:…``). The pull
crosses into the sandbox user via ``machinectl_cmd`` (identity ``SANDBOX``) —
the rootless daemon installed by L5 owns the image cache.

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
from core.host_config import machinectl_cmd
from core.hydration import IMAGE_REGISTRY
from core.setup.phase_runner import Identity, Phase, PhaseResult

if TYPE_CHECKING:
    from core.host_config import HostConfig
    from core.setup.phase_runner import SetupContext

_HELPER_PIN = IMAGE_REGISTRY["busybox_musl"]
_HELPER_REF = _HELPER_PIN.pinned
_HELPER_TAGGED = _HELPER_PIN.tagged


def _crossed(host_config: HostConfig, inner: str) -> list[str]:
    """Build the ``machinectl`` crossing argv for an inner ``bash -c`` string."""
    return [
        *machinectl_cmd(
            host_config.host.docker_unprivileged_user,
            host_config.host.machinectl_authentication,
        ),
        "/bin/bash",
        "-c",
        inner,
    ]


def _ref_present(host_config: HostConfig, ref: str) -> bool:
    """``True`` iff ``docker image inspect <ref>`` succeeds (crossed)."""
    cmd = _crossed(host_config, f"docker image inspect {ref}")
    try:
        Executor().run(cmd, sentinel=True)
    except SandboxExecutionError:
        return False
    return True


def _digest_present(host_config: HostConfig) -> bool:
    """``True`` iff the *pinned digest* is in the sandbox user's image store."""
    return _ref_present(host_config, _HELPER_REF)


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
    host_config = ctx.host_config
    if _digest_present(host_config):
        return (
            PhaseResult.ALREADY_CORRECT,
            f"helper image {_HELPER_REF} already cached",
        )
    if _ref_present(host_config, _HELPER_TAGGED):
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
    cmd = _crossed(ctx.host_config, f"docker pull {_HELPER_REF}")
    Executor().run(cmd, sentinel=True)
    return f"helper image {_HELPER_REF} pulled"


def _reverify(ctx: SetupContext) -> bool:
    """Confirm the pinned digest is now present in the local image store."""
    return _digest_present(ctx.host_config)


PHASE = Phase(
    id="l7",
    name="helper image pre-pull (busybox-musl pinned digest)",
    identity=Identity.SANDBOX,
    probe=_probe,
    act=_act,
    reverify=_reverify,
    depends_on=("l65",),
)
