# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""ActionContext — per-invocation plumbing passed to ``Action.execute``.

Bundles state that is uniform across a phase invocation:

- ``host_user`` — the unprivileged systemd user that owns the docker
  daemon (the daemon owner, setup-determined per execution mode).
- ``docker_execution_mode`` — the resolved
  :class:`~core.host_config.DockerExecutionMode` (separate-user / operator-rootless),
  flat plumbing. The helper-container Actions
  (``HelperMkdirChownAction`` / ``HelperCpChownAction``) forward it to the
  helper primitive so that in ``operator-rootless`` mode the helper ``docker
  run`` op executes as a local subprocess with no ``machinectl`` crossing.
- ``executor`` — the sterile-subprocess executor; the only sanctioned
  way to shell out from an Action's ``.execute()``.
- ``instance_dir`` — the per-instance directory the action is operating
  on (already absolute by the time a plan is built).
- ``host_config`` — the resolved per-host :class:`~core.host_config.HostConfig`,
  OPTIONAL (default ``None``). Only the compose-up construction site
  (``_phase_compose_up``) supplies it: :class:`~core.actions.ComposeUpAction`
  routes through ``core.dispatch.invoke``/``build_invocation``, which need the
  full ``HostConfig`` (operator-side compose-state resolution). Every other
  ``ActionContext`` construction site leaves it unset, so adding this field
  ripples nowhere beyond compose-up.

Frozen so an ``ActionContext`` can be shared safely across the Actions
of a phase without risk of mutation. Field count is intentionally
small — per-Action state lives on the Action.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.host_config import DEFAULT_PROVISIONING_MODE, DockerExecutionMode

if TYPE_CHECKING:
    from pathlib import Path

    from core.executor import Executor
    from core.host_config import HostConfig


@dataclass(frozen=True)
class ActionContext:
    """Frozen bundle of per-phase execution plumbing."""

    host_user: str
    executor: Executor
    instance_dir: Path
    host_config: HostConfig | None = None
    docker_execution_mode: DockerExecutionMode = DEFAULT_PROVISIONING_MODE
