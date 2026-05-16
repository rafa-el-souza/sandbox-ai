"""Typed dispatcher orchestration: op enum, per-op validators, target-argv builders.

This module is the Python side of the runtime dispatcher (OpenSpec change
``runtime-dispatcher`` / C-001). The orchestrator narrows its privilege grant
from "arbitrary bash as the sandbox user" to a fixed enumeration of typed ops.
Each op carries:

- a per-op argument validator (rejects malformed args before the boundary is
  crossed), and
- a target-argv builder (translates validated args into the docker invocation
  the Go dispatcher binary spawns via process replacement).

The Go binary at ``/usr/local/libexec/sandbox-ai/dispatch`` is the *executor*;
it trusts the validation performed here (design D4). Both sides construct the
same target argv from a shared JSON fixture so they stay in lockstep.

Scaffold status (Milestone 1): the :class:`Op` enum and the :class:`OpSpec`
wiring are real. Every validator body, every target-argv builder body, and
:func:`invoke` raise :class:`NotImplementedError` — Milestone 2 implements the
validators/builders and Milestone 3 implements the Go dispatcher. No caller is
wired this milestone; the :func:`invoke` *signature* is load-bearing for
Milestones 2-7 and is fixed here deliberately.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import subprocess

    from core.host_config import HostConfig


class Op(StrEnum):
    """The ten typed ops the dispatcher accepts as ``argv[1]``.

    The enum *value* is the wire name (hyphenated) passed to the dispatcher
    binary; the member identifier is the Python-legal upper-snake form. The
    surface is byte-faithful to the existing ``machinectl_cmd(...)`` callsites
    it replaces (spec "Typed Op Surface").
    """

    AUTH_PROBE = "auth-probe"
    COMPOSE_UP = "compose-up"
    COMPOSE_DOWN = "compose-down"
    COMPOSE_PS = "compose-ps"
    COMPOSE_LS = "compose-ls"
    DOCKER_VERSION = "docker-version"
    DOCKER_INFO = "docker-info"
    DOCKER_MANIFEST_INSPECT = "docker-manifest-inspect"
    HELPER_CHOWN_FILES = "helper-chown-files"
    HELPER_MKDIR_CHOWN_DIRS = "helper-mkdir-chown-dirs"


# A validator inspects the op's args and raises on malformed input; it returns
# nothing on success.
Validator = Callable[[Sequence[str]], None]

# A target-argv builder translates validated args into the argv the dispatcher
# binary spawns (always ``["/bin/bash", "-c", "<cmd>"]`` in practice). It takes
# the op's args plus the resolved host config (for instance-derived state) and
# returns the target argv.
TargetArgvBuilder = Callable[[Sequence[str], "HostConfig"], list[str]]


@dataclass(frozen=True)
class OpSpec:
    """Immutable per-op specification.

    Attributes:
        name: The op's wire name (matches an :class:`Op` value).
        min_args: Minimum number of positional args the op accepts.
        max_args: Maximum number of positional args, or ``None`` for unbounded
            (variadic ops such as ``helper-chown-files``).
        validate: Per-op argument validator; raises on malformed args.
        build_target_argv: Per-op target-argv builder.
    """

    name: str
    min_args: int
    max_args: int | None
    validate: Validator
    build_target_argv: TargetArgvBuilder


def _unimplemented_validator(op: Op) -> Validator:
    """Return a validator stub that raises :class:`NotImplementedError`.

    Milestone 2 replaces each op's stub with a real per-op validator.
    """

    def _validate(args: Sequence[str]) -> None:
        raise NotImplementedError(
            f"validator for op {op.value!r} is a Milestone-2 stub; args={list(args)!r}"
        )

    return _validate


def _unimplemented_builder(op: Op) -> TargetArgvBuilder:
    """Return a target-argv builder stub that raises :class:`NotImplementedError`.

    Milestone 2 replaces each op's stub with a real per-op target-argv builder.
    """

    def _build(args: Sequence[str], host_config: HostConfig) -> list[str]:
        raise NotImplementedError(
            f"target-argv builder for op {op.value!r} is a Milestone-2 stub; args={list(args)!r}"
        )

    return _build


# Wire every Op to an OpSpec whose validator/builder are Milestone-2 stubs.
# min_args/max_args are placeholder bounds (0/None) at scaffold time; Milestone
# 2 sets the real per-op bounds alongside the real validators.
OP_SPECS: dict[Op, OpSpec] = {
    op: OpSpec(
        name=op.value,
        min_args=0,
        max_args=None,
        validate=_unimplemented_validator(op),
        build_target_argv=_unimplemented_builder(op),
    )
    for op in Op
}


def invoke(
    op: Op | str,
    args: list[str],
    host_config: HostConfig,
    *,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Validate ``op``/``args`` and run the dispatcher across the privilege boundary.

    Contract (load-bearing — Milestones 2-7 depend on this signature):

    - ``op``: an :class:`Op` enum member, or its ``str`` wire value (e.g.
      ``Op.COMPOSE_UP`` or ``"compose-up"``). Both forms are accepted; an
      unknown value is an error.
    - ``args``: the op's positional args as a list of ``str`` (already the raw
      strings the dispatcher will receive; per-op validation happens here
      before the boundary is crossed).
    - ``host_config``: the resolved :class:`~core.host_config.HostConfig`,
      supplying the unprivileged docker user and machinectl auth mode used to
      build the boundary-crossing prefix, plus instance-derived state for
      compose ops' target-argv construction.
    - ``timeout``: keyword-only; forwarded to the underlying subprocess. ``None``
      (the default) means no timeout.

    Returns the completed :class:`subprocess.CompletedProcess` (bytes streams)
    of the boundary-crossing invocation.

    Scaffold status: this body raises :class:`NotImplementedError`. The real
    implementation (validate -> build target argv -> wrap in the
    ``machinectl_cmd`` + ``bash -c`` shape -> run) lands in a later milestone;
    no caller is wired this milestone.
    """
    raise NotImplementedError(
        "core.dispatch.invoke is a scaffold stub; the validate->build->run body "
        f"lands in a later milestone (op={op!r}, args={args!r}, "
        f"host_config={host_config!r}, timeout={timeout!r})"
    )
