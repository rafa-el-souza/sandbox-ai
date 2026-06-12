# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""L2a — systemd ``Delegate=yes`` drop-in for the sandbox user's manager.

Sub-phase between L2 and L5 (spec "Phase Execution Order"). It owns one
on-disk file, narrow-scoped to the sandbox user's per-user systemd manager:

- ``/etc/systemd/system/user-<sandbox-uid>.service.d/sandbox-ai-delegate.conf``
  (mode 0644, root:root) — ``Delegate=yes``, narrow-scoped to the sandbox
  user's manager (NOT the template-wide ``user@.service.d/`` — design "Why
  narrow Delegate=yes scope"). Followed by ``systemctl daemon-reload``.

**L2a is separate-user only** (``applies_in`` excludes operator-rootless,
D5a/O3): the ``Delegate=yes`` drop-in is a host-root mutation, so in
operator-rootless (where the apply pass runs unprivileged as the operator) it is
owned by the ``host_batch`` ``DELEGATE`` item + ``_bootstrap-host`` escalation
(``host_batch._apply_delegate`` reuses :func:`render_delegate_dropin`, targeting
``user-<operator-uid>.service.d/``). The runner reports the phase ``skipped
(operator-rootless)`` in both passes, joining L1/L2 and the M2 crossing-only
phases.

**Why this is its own phase, after L2 (load-bearing — do NOT move it before
L2).** The drop-in path is uid-scoped: it embeds the sandbox user's host uid,
which only exists once L2's ``useradd`` has created the user. This phase's
``depends_on=("l2",)`` guarantees the user exists before the probe resolves
its uid; ``l5`` depends on this phase so the single linear chain
``l0→l1→l2→l2a→l5→…`` is preserved (L2a is guaranteed after L2 — the user
exists — and before L5 — rootless dockerd needs cgroup delegation). The probe
*additionally* uses the shared :func:`probe_sandbox_pw_or_missing` guard so a
not-yet-created user is the ``MISSING`` signal rather than a crash escaping
through the plan/apply passes (defensive even though ``depends_on=("l2",)``).

Content-aware probe (design D10): the expected file *body* is rendered from
the current source (the sandbox uid decides the drop-in path). The probe
byte-compares rendered-expected against the on-disk file: user not yet created
→ ``MISSING``; file absent → ``MISSING``; present-but-different → ``DRIFT``;
matching → ``ALREADY_CORRECT``.
"""

from __future__ import annotations

import os
import pwd
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from core.host_config import DockerExecutionMode
from core.setup.phase_runner import (
    Identity,
    Phase,
    PhaseResult,
    probe_sandbox_pw_or_missing,
    resolve_sandbox_pw,
)

if TYPE_CHECKING:
    from core.host_config import HostConfig
    from core.setup.phase_runner import SetupContext

_MANAGED_HEADER = "# sandbox-ai managed — do not edit; rerun 'sudo sandbox setup'"

_SYSTEMD_SYSTEM = Path("/etc/systemd/system")


def _delegate_dropin_path_for_uid(uid: int) -> Path:
    """The narrow-scoped Delegate drop-in path for sandbox-user ``uid``."""
    return _SYSTEMD_SYSTEM / f"user-{uid}.service.d" / "sandbox-ai-delegate.conf"


def _delegate_dropin_path(host_config: HostConfig) -> Path:
    """The Delegate drop-in path; raises if the sandbox user does not exist.

    ``act``/``reverify`` use this directly — by then ``depends_on=("l2",)``
    guarantees the user exists, and a residual ``KeyError`` is correctly a
    runner-classified FAIL. The probe uses the guarded path instead (see
    :func:`probe_sandbox_pw_or_missing`).
    """
    uid = resolve_sandbox_pw(host_config).pw_uid
    return _delegate_dropin_path_for_uid(uid)


def render_delegate_dropin() -> str:
    """Render the expected ``Delegate=yes`` drop-in body."""
    return f"{_MANAGED_HEADER}\n[Service]\nDelegate=yes\n"


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


def _probe(ctx: SetupContext) -> tuple[PhaseResult, str]:
    """Content-aware L2a probe (see module docstring).

    Uses the shared sandbox-user guard: a not-yet-created user is the
    ``MISSING`` signal (L2 creates it), never a crash escaping the plan/apply
    passes (the content-aware-probe contract / B1 class).
    """
    pw = probe_sandbox_pw_or_missing(ctx.host_config)
    if not isinstance(pw, pwd.struct_passwd):
        return pw

    dropin = _delegate_dropin_path_for_uid(pw.pw_uid)
    observed = _read(dropin)
    expected = render_delegate_dropin()
    if observed is None:
        return PhaseResult.MISSING, f"{dropin} absent"
    if observed != expected:
        return PhaseResult.DRIFT, f"{dropin} content differs from source"
    return PhaseResult.ALREADY_CORRECT, "Delegate drop-in matches source"


def _write_root_file(path: Path, body: str, mode: int) -> None:
    """Write ``body`` to ``path`` root:root at ``mode`` (parent dirs 0755)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    os.chmod(path, mode)
    os.chown(path, 0, 0)


def _act(ctx: SetupContext) -> str:
    """Write the Delegate drop-in and reload systemd."""
    dropin = _delegate_dropin_path(ctx.host_config)
    _write_root_file(dropin, render_delegate_dropin(), 0o644)
    subprocess.run(
        ["systemctl", "daemon-reload"],
        capture_output=True,
        text=True,
        check=True,
    )
    return f"wrote {dropin}; daemon-reload"


def _reverify(ctx: SetupContext) -> bool:
    """L2a converged iff the drop-in now byte-matches the rendered source."""
    return (
        _read(_delegate_dropin_path(ctx.host_config))
        == render_delegate_dropin()
    )


PHASE = Phase(
    id="l2a",
    name="systemd Delegate=yes drop-in",
    identity=Identity.ROOT,
    probe=_probe,
    act=_act,
    reverify=_reverify,
    depends_on=("l2",),
    # separate-user only. The Delegate drop-in is a host-root mutation; in
    # operator-rootless it is owned by the ``host_batch`` ``DELEGATE`` item +
    # ``_bootstrap-host`` escalation (design D5a / O3). Gated OUT (reported
    # ``skipped`` in both passes), mirroring L1/L2 and the M2 crossing-only phases.
    applies_in=frozenset({DockerExecutionMode.SEPARATE_USER}),
)

__all__ = [
    "PHASE",
    "render_delegate_dropin",
]
