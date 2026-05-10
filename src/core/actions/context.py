"""ActionContext — per-invocation plumbing passed to ``Action.execute``.

Bundles state that is uniform across a phase invocation:

- ``host_user`` — the unprivileged systemd user that owns the docker
  daemon (``[host].docker_unprivileged_user`` from ``sandbox-ai.toml``).
- ``auth`` — the machinectl auth mode (sudo / polkit) read from the
  same per-host config file.
- ``executor`` — the sterile-subprocess executor; the only sanctioned
  way to shell out from an Action's ``.execute()``.
- ``instance_dir`` — the per-instance directory the action is operating
  on (already absolute by the time a plan is built).

Frozen so an ``ActionContext`` can be shared safely across the Actions
of a phase without risk of mutation. Field count is intentionally
≤5 — per-Action state lives on the Action.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from core.executor import Executor
    from core.host_config import MachinectlAuth


@dataclass(frozen=True)
class ActionContext:
    """Frozen bundle of per-phase execution plumbing."""

    host_user: str
    auth: MachinectlAuth
    executor: Executor
    instance_dir: Path
