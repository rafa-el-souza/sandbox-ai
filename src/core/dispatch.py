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
same target argv from a shared JSON fixture so they stay in lockstep
(``src/templates/dispatch/fixtures/target_argv_cases.json``).

Milestone 2 status: the :class:`Op` enum + :class:`OpSpec` wiring, the per-op
validators, and the per-op target-argv builders are real. :func:`invoke` is
still a scaffold stub (its body lands in a later milestone); its *signature*
is load-bearing for Milestones 2-7 and is fixed deliberately. No caller is
wired this milestone.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from core.compose import compose_project_name
from core.helper_container import _hardened_docker_run
from core.hydration import IMAGE_REGISTRY, InstanceConfig
from core.registry import InstanceRegistry

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


class DispatchValidationError(ValueError):
    """A per-op argument validator rejected its input.

    Raised by the validators in this module before the privilege boundary is
    crossed. The message names the rejected argument and the rule it violated
    (spec "Per-Op Argument Validation"). The Go dispatcher binary trusts these
    validators and does NOT re-run them (design D4).
    """


# ─── Shared argument-shape predicates ───────────────────────────────────────
#
# The instance-name rules mirror ``sandbox init``'s validator
# (``cli.main._validate_name`` — regex ``^[a-z0-9][a-z0-9_-]*$``, no leading
# ``-``/``_``, ≤30 chars, not a reserved name) per the
# ``instance-workspace-model`` capability's instance-name validation. ``core``
# must not import ``cli``; the rules are simple constants and are mirrored here
# faithfully. A future change that moves the validator into ``core`` should
# collapse these to a single shared definition.

_INSTANCE_NAME_REGEX = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_INSTANCE_NAME_MAX = 30
_RESERVED_INSTANCE_NAMES: frozenset[str] = frozenset(
    {
        "_backups",
        "default",
        "all",
        "none",
        "system",
        "isolated",
        "core_proxy",
        "dns",
        "egress",
        "ipc",
    }
)

_MODE_OCTAL_REGEX = re.compile(r"^[0-7]{4}$")
_DOCKER_INFO_PRESETS: dict[str, str] = {
    "security-options": "{{.SecurityOptions}}",
    "runtimes": "{{json .Runtimes}}",
}
_IMAGE_REF_REGEX = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$")


def _require_instance_name(value: str) -> None:
    """Validate ``value`` against the ``sandbox init`` instance-name rules."""
    if not value:
        raise DispatchValidationError("instance name must not be empty")
    if len(value) > _INSTANCE_NAME_MAX:
        raise DispatchValidationError(
            f"instance name {value!r} exceeds {_INSTANCE_NAME_MAX}-character cap"
        )
    if value.startswith(("-", "_")):
        raise DispatchValidationError(f"instance name {value!r} must not start with '-' or '_'")
    if not _INSTANCE_NAME_REGEX.match(value):
        raise DispatchValidationError(
            f"instance name {value!r} contains invalid characters; use [a-z0-9_-]"
        )
    if value in _RESERVED_INSTANCE_NAMES:
        raise DispatchValidationError(f"instance name {value!r} is reserved")


def _require_no_path_metachars(value: str, *, kind: str) -> None:
    """Reject a file/leaf name containing ``/``, ``..``, NUL, or newline."""
    if "/" in value:
        raise DispatchValidationError(f"{kind} {value!r} must not contain '/'")
    if value == ".." or ".." in value.split("/"):
        raise DispatchValidationError(f"{kind} {value!r} must not contain '..'")
    if "\x00" in value:
        raise DispatchValidationError(f"{kind} {value!r} must not contain NUL")
    if "\n" in value:
        raise DispatchValidationError(f"{kind} {value!r} must not contain a newline")


def _require_absolute_parent(value: str) -> None:
    """Reject a parent path that is not absolute or contains traversal/NUL/newline."""
    if not value.startswith("/"):
        raise DispatchValidationError(f"parent path {value!r} must be absolute")
    if ".." in value.split("/"):
        raise DispatchValidationError(f"parent path {value!r} must not contain '..' components")
    if "\x00" in value:
        raise DispatchValidationError(f"parent path {value!r} must not contain NUL")
    if "\n" in value:
        raise DispatchValidationError(f"parent path {value!r} must not contain a newline")


def _require_octal_mode(value: str) -> None:
    """Reject a mode that is not a 4-digit octal between ``0000`` and ``7777``."""
    if not _MODE_OCTAL_REGEX.match(value):
        raise DispatchValidationError(
            f"mode {value!r} must be a 4-digit octal between 0000 and 7777"
        )


def _require_nonneg_int(value: str, *, kind: str) -> None:
    """Reject a uid/gid that is not a decimal non-negative integer."""
    if not value.isdigit():
        raise DispatchValidationError(f"{kind} {value!r} must be a decimal non-negative integer")


# ─── Per-op validators ──────────────────────────────────────────────────────


def _validate_nullary(args: Sequence[str]) -> None:
    if args:
        raise DispatchValidationError(f"op takes no arguments; got {list(args)!r}")


def _validate_one_instance(args: Sequence[str]) -> None:
    if len(args) != 1:
        raise DispatchValidationError(
            f"op takes exactly one <instance-name> argument; got {list(args)!r}"
        )
    _require_instance_name(args[0])


def _validate_compose_down(args: Sequence[str]) -> None:
    if not 1 <= len(args) <= 2:
        raise DispatchValidationError(
            f"compose-down takes <instance-name> and an optional --volumes; got {list(args)!r}"
        )
    _require_instance_name(args[0])
    if len(args) == 2 and args[1] != "--volumes":
        raise DispatchValidationError(
            f"compose-down's optional second arg must be the literal '--volumes'; got {args[1]!r}"
        )


def _validate_docker_info(args: Sequence[str]) -> None:
    if len(args) != 1:
        raise DispatchValidationError(
            f"docker-info takes exactly one <format-preset> argument; got {list(args)!r}"
        )
    if args[0] not in _DOCKER_INFO_PRESETS:
        raise DispatchValidationError(
            f"docker-info preset {args[0]!r} is unknown; only "
            f"{sorted(_DOCKER_INFO_PRESETS)!r} are accepted"
        )


def _validate_docker_manifest_inspect(args: Sequence[str]) -> None:
    if len(args) != 1:
        raise DispatchValidationError(
            f"docker-manifest-inspect takes exactly one <image-ref> argument; got {list(args)!r}"
        )
    if not _IMAGE_REF_REGEX.match(args[0]):
        raise DispatchValidationError(
            f"image ref {args[0]!r} must match <name>@sha256:<64-hex> "
            "(a bare digest has no <name>@ prefix)"
        )


def _validate_helper_chown_files(args: Sequence[str]) -> None:
    if len(args) < 5:
        raise DispatchValidationError(
            "helper-chown-files takes <parent> <mode> <uid> <gid> <file...> "
            f"(>=5 args); got {list(args)!r}"
        )
    parent, mode, uid, gid, *files = args
    _require_absolute_parent(parent)
    _require_octal_mode(mode)
    _require_nonneg_int(uid, kind="uid")
    _require_nonneg_int(gid, kind="gid")
    for name in files:
        _require_no_path_metachars(name, kind="file name")


def _validate_helper_mkdir_chown_dirs(args: Sequence[str]) -> None:
    if len(args) < 4:
        raise DispatchValidationError(
            "helper-mkdir-chown-dirs takes <parent> <uid> <gid> <leaf...> "
            f"(>=4 args); got {list(args)!r}"
        )
    parent, uid, gid, *leaves = args
    _require_absolute_parent(parent)
    _require_nonneg_int(uid, kind="uid")
    _require_nonneg_int(gid, kind="gid")
    for name in leaves:
        _require_no_path_metachars(name, kind="leaf name")


# ─── Compose hydration resolution ───────────────────────────────────────────
#
# Mirrors the source resolution path: registry lookup -> instance_dir ->
# InstanceConfig.from_toml(sandbox.toml) -> compose-file list +
# compose_project_name + .sandbox.env. This is the same chain
# ``cli.main._lookup_instance_or_exit`` / ``_load_config`` /
# ``_build_compose_files`` walk; ``core`` must not import ``cli`` so the
# compose-file list construction (byte-faithful to ``_build_compose_files``)
# is mirrored here.


def _resolve_compose_state(inst: str) -> tuple[str, str, str]:
    """Return ``(project_name, compose_files_str, env_file)`` for ``inst``.

    Raises:
        DispatchValidationError: the instance is not registered.
    """
    entry = InstanceRegistry().get(inst)
    if entry is None:
        raise DispatchValidationError(f"no sandbox instance named {inst!r} is registered")
    instance_dir = entry.instance_dir
    config = InstanceConfig.from_toml(os.path.join(instance_dir, "sandbox.toml"))
    files = ["-f", os.path.join(instance_dir, "docker", "compose.yml")]
    if config.components_db_postgres.enabled:
        files.extend(["-f", os.path.join(instance_dir, "docker", "extras", "db-postgres.yml")])
    if config.components.mcp_firecrawl:
        files.extend(["-f", os.path.join(instance_dir, "docker", "extras", "mcp-firecrawl.yml")])
    project_name = compose_project_name(inst)
    env_file = os.path.join(instance_dir, ".sandbox.env")
    return project_name, " ".join(files), env_file


# ─── Per-op target-argv builders ────────────────────────────────────────────


def _bash_c(inner: str) -> list[str]:
    return ["/bin/bash", "-c", inner]


def _build_auth_probe(args: Sequence[str], host_config: HostConfig) -> list[str]:
    return _bash_c("echo ok")


def _build_compose_ls(args: Sequence[str], host_config: HostConfig) -> list[str]:
    return _bash_c("docker compose ls --format json --all")


def _build_docker_version(args: Sequence[str], host_config: HostConfig) -> list[str]:
    return _bash_c("docker version --format '{{.Server.Version}}'")


def _build_docker_info(args: Sequence[str], host_config: HostConfig) -> list[str]:
    fmt = _DOCKER_INFO_PRESETS[args[0]]
    return _bash_c(f"docker info --format '{fmt}'")


def _build_docker_manifest_inspect(args: Sequence[str], host_config: HostConfig) -> list[str]:
    return _bash_c(f"docker manifest inspect {args[0]}")


def _build_compose_up(args: Sequence[str], host_config: HostConfig) -> list[str]:
    project_name, files_str, env_file = _resolve_compose_state(args[0])
    return _bash_c(
        f"TERM=dumb NO_COLOR=1 BUILDKIT_PROGRESS=plain "
        f"COMPOSE_PROJECT_NAME={project_name} docker compose {files_str} "
        f"--ansi never --env-file {env_file} up -d --build --wait"
    )


def _build_compose_down(args: Sequence[str], host_config: HostConfig) -> list[str]:
    project_name, files_str, env_file = _resolve_compose_state(args[0])
    v_flag = " -v" if len(args) == 2 else ""
    return _bash_c(
        f"TERM=dumb NO_COLOR=1 BUILDKIT_PROGRESS=plain "
        f"COMPOSE_PROJECT_NAME={project_name} docker compose {files_str} "
        f"--ansi never --env-file {env_file} down{v_flag}"
    )


def _build_compose_ps(args: Sequence[str], host_config: HostConfig) -> list[str]:
    project_name, files_str, env_file = _resolve_compose_state(args[0])
    return _bash_c(
        f"TERM=dumb NO_COLOR=1 BUILDKIT_PROGRESS=plain COMPOSE_PROJECT_NAME={project_name} "
        f"docker compose {files_str} "
        f"--env-file {env_file} "
        f"--ansi never ps --format json"
    )


def _build_helper_chown_files(args: Sequence[str], host_config: HostConfig) -> list[str]:
    parent, mode, uid, gid, *files = args
    image = IMAGE_REGISTRY["busybox_musl"].pinned
    mode_octal = format(int(mode, 8), "04o")
    quoted_names = " ".join(shlex.quote(f) for f in files)
    inner = (
        f"set -e; for f in {quoted_names}; do "
        f'cp /p/"$f" /tmp/"$f" && '
        f'unlink /p/"$f" && '
        f'cp /tmp/"$f" /p/"$f" && '
        f'chmod {mode_octal} /p/"$f" && '
        f'chown {uid}:{gid} /p/"$f"; '
        "done"
    )
    return _bash_c(_hardened_docker_run(image, parent, inner))


def _build_helper_mkdir_chown_dirs(args: Sequence[str], host_config: HostConfig) -> list[str]:
    parent, uid, gid, *leaves = args
    image = IMAGE_REGISTRY["busybox_musl"].pinned
    quoted_leaves = " ".join(shlex.quote(leaf) for leaf in leaves)
    inner = (
        f"set -e; for d in {quoted_leaves}; do "
        f'mkdir -p /p/"$d" && chown {uid}:{gid} /p/"$d"; '
        "done"
    )
    return _bash_c(_hardened_docker_run(image, parent, inner))


# Wire every Op to an OpSpec with real per-op bounds, validator, and builder.
OP_SPECS: dict[Op, OpSpec] = {
    Op.AUTH_PROBE: OpSpec(
        name=Op.AUTH_PROBE.value,
        min_args=0,
        max_args=0,
        validate=_validate_nullary,
        build_target_argv=_build_auth_probe,
    ),
    Op.COMPOSE_UP: OpSpec(
        name=Op.COMPOSE_UP.value,
        min_args=1,
        max_args=1,
        validate=_validate_one_instance,
        build_target_argv=_build_compose_up,
    ),
    Op.COMPOSE_DOWN: OpSpec(
        name=Op.COMPOSE_DOWN.value,
        min_args=1,
        max_args=2,
        validate=_validate_compose_down,
        build_target_argv=_build_compose_down,
    ),
    Op.COMPOSE_PS: OpSpec(
        name=Op.COMPOSE_PS.value,
        min_args=1,
        max_args=1,
        validate=_validate_one_instance,
        build_target_argv=_build_compose_ps,
    ),
    Op.COMPOSE_LS: OpSpec(
        name=Op.COMPOSE_LS.value,
        min_args=0,
        max_args=0,
        validate=_validate_nullary,
        build_target_argv=_build_compose_ls,
    ),
    Op.DOCKER_VERSION: OpSpec(
        name=Op.DOCKER_VERSION.value,
        min_args=0,
        max_args=0,
        validate=_validate_nullary,
        build_target_argv=_build_docker_version,
    ),
    Op.DOCKER_INFO: OpSpec(
        name=Op.DOCKER_INFO.value,
        min_args=1,
        max_args=1,
        validate=_validate_docker_info,
        build_target_argv=_build_docker_info,
    ),
    Op.DOCKER_MANIFEST_INSPECT: OpSpec(
        name=Op.DOCKER_MANIFEST_INSPECT.value,
        min_args=1,
        max_args=1,
        validate=_validate_docker_manifest_inspect,
        build_target_argv=_build_docker_manifest_inspect,
    ),
    Op.HELPER_CHOWN_FILES: OpSpec(
        name=Op.HELPER_CHOWN_FILES.value,
        min_args=5,
        max_args=None,
        validate=_validate_helper_chown_files,
        build_target_argv=_build_helper_chown_files,
    ),
    Op.HELPER_MKDIR_CHOWN_DIRS: OpSpec(
        name=Op.HELPER_MKDIR_CHOWN_DIRS.value,
        min_args=4,
        max_args=None,
        validate=_validate_helper_mkdir_chown_dirs,
        build_target_argv=_build_helper_mkdir_chown_dirs,
    ),
}


def validate_args(op: Op | str, args: Sequence[str]) -> None:
    """Validate ``args`` for ``op`` using the op's per-op validator.

    Accepts an :class:`Op` member or its ``str`` wire value. Raises
    :class:`DispatchValidationError` for malformed args (the message names the
    rejected argument and the rule it violated) and :class:`ValueError` for an
    unknown op. The dispatcher binary trusts this validation (design D4).
    """
    resolved = Op(op)
    OP_SPECS[resolved].validate(args)


def build_target_argv(op: Op | str, args: Sequence[str], host_config: HostConfig) -> list[str]:
    """Build the target argv for ``op``/``args`` (validators NOT re-run here).

    Callers MUST :func:`validate_args` first; this function assumes validated
    input and mirrors the source builders byte-for-byte. The shared fixture
    ``src/templates/dispatch/fixtures/target_argv_cases.json`` pins the
    expected output so the Python builder and the Go ``main_test.go`` stay in
    lockstep (spec "Target Argv Construction Per Op").
    """
    resolved = Op(op)
    return OP_SPECS[resolved].build_target_argv(args, host_config)


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
