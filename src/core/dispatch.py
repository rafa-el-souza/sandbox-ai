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

Contract: :func:`invoke` validates the op, expands the Q6 compose wire form
(or passes deterministic args verbatim), crosses the privilege boundary via
``machinectl_cmd`` + ``bash -c``, and runs the result through the sterile
:class:`core.executor.Executor`. It returns ``subprocess.CompletedProcess[str]``
(the Executor is the only sanctioned execution path and yields text-mode
streams) and keeps the raise-on-failure contract — a non-zero exit raises
:class:`~core.exceptions.SandboxExecutionError`. ``docker-manifest-inspect``
validates by ``IMAGE_REGISTRY`` membership (``{pin.pinned}`` union
``{pin.tagged}``, computed once at module load; design Q7) so the supply-chain
tag-drift call routes through the typed op. A sibling :func:`probe` returns a
typed :class:`ProbeOutcome` for probe-style callers (doctor checks, the cli
``auth-probe`` preflight) and is the SINGLE site the ``SandboxExecutionError``
/ ``__cause__`` timeout discrimination lives (design Q8); :func:`invoke` is the
SINGLE site that crosses the boundary.
"""

from __future__ import annotations

import base64
import gzip
import io
import os
import re
import shlex
import subprocess
import tarfile
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from importlib.resources import files as _resource_files
from typing import TYPE_CHECKING

from core.compose import compose_project_name
from core.exceptions import SandboxExecutionError
from core.executor import Executor
from core.helper_container import _hardened_docker_run
from core.host_config import machinectl_cmd, pipe_cmd
from core.hydration import IMAGE_REGISTRY, InstanceConfig
from core.registry import InstanceRegistry

if TYPE_CHECKING:
    from importlib.resources.abc import Traversable

    from core.host_config import HostConfig

_DISPATCH_BINARY = "/usr/local/libexec/sandbox-ai/dispatch"

# The Go dispatcher source tar'd into the crossed payload and compiled
# offline. ``vendor`` / ``fixtures`` are directories; the rest are files. The
# producer tars exactly these so the build context is hermetic (no stray host
# files).
_DISPATCH_SOURCE_ENTRIES = ("main.go", "main_test.go", "go.mod", "go.sum", "vendor", "fixtures")

# In-container build dir (the bind-mount target). The host build directory is
# an ephemeral per-call ``mktemp -d`` under the lingering daemon user's
# per-user runtime dir ``/run/user/$(id -u)`` (resolved INSIDE the crossing —
# never named host-side); the container always sees it at this fixed path so ``-trimpath`` keeps the
# build location out of the binary (reproducibility is location-neutral).
# ``go test ./...`` runs the Python<->Go fixture-parity suite BEFORE
# ``go build`` in the SAME ``docker run`` (spec C-e enforcement): a fixture
# mismatch fails ``go test`` -> the ``&&`` short-circuits -> no binary is
# produced, and the non-zero exit propagates out of :func:`compile_dispatcher`
# as a hard failure (no binary is placed at ``output_path``).
_BUILD_MOUNT_DST = "/build"
_COMPILE_INNER = (
    "go test ./... && "
    "go build -trimpath -ldflags '-s -w' -o /build/dispatch ."
)


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


@dataclass(frozen=True)
class ProbeOutcome:
    """Typed result of a non-raising :func:`probe` call (design Q8).

    Probe-style callers (every doctor check; the cli ``auth-probe`` preflight)
    must branch on success/failure/timeout and emit ``pass``/``fail``/``skip``
    verdicts rather than crash on an *expected* failure. :func:`invoke` keeps
    its raise-on-failure contract verbatim (helper/compose depend on it);
    :func:`probe` wraps it and encapsulates — in this ONE place — the
    ``SandboxExecutionError`` catch and the ``__cause__`` timeout
    discrimination.

    Attributes:
        ok: ``True`` iff the op completed successfully (exit 0).
        timed_out: ``True`` iff the failure was a subprocess timeout
            (``exc.__cause__`` is a :class:`subprocess.TimeoutExpired`);
            always ``False`` when ``ok``.
        stdout: The op's captured stdout on success; ``""`` on any failure
            (the sterile ``Executor`` does not surface stdout through the
            raised exception).
        message: ``""`` on success; otherwise the failure text — the
            ``str(SandboxExecutionError)`` the sterile ``Executor`` raised
            (carrying its informative exit-status / ``OSError`` context).
            Probe-style callers interpolate this into their operator-facing
            ``detail`` so the pre-refactor diagnostic fidelity is preserved.
    """

    ok: bool
    timed_out: bool
    stdout: str
    message: str


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

# Q7 — ``docker-manifest-inspect``'s legitimate argument domain is exactly the
# set of refs that appear in ``IMAGE_REGISTRY``: every entry's digest ref
# (``pin.pinned`` — ``<ref>@sha256:<hex>``, the stale-digest-detection call)
# AND its tag ref (``pin.tagged`` — ``<ref>:<tag>``, the best-effort tag-drift
# call). Validation is by *set membership*, not by docker-reference grammar:
# the op exists solely to inspect registry refs, so that set IS its domain (a
# new registry entry auto-extends it; zero grammar surface to get subtly
# wrong). Computed ONCE at module load from the already-imported
# ``IMAGE_REGISTRY`` (see design "Resolved Design Questions" Q7).
_MANIFEST_INSPECT_REFS: frozenset[str] = frozenset(
    {pin.pinned for pin in IMAGE_REGISTRY.values()}
    | {pin.tagged for pin in IMAGE_REGISTRY.values()}
)


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
    if args[0] not in _MANIFEST_INSPECT_REFS:
        raise DispatchValidationError(
            f"image ref {args[0]!r} is not a member of IMAGE_REGISTRY "
            "(accepted: each registry entry's pinned <ref>@sha256:<hex> or "
            "tagged <ref>:<tag> form; a bare digest or arbitrary non-registry "
            "ref is rejected)"
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


def _resolve_compose_state(inst: str) -> tuple[str, list[str], str]:
    """Return ``(project_name, compose_file_paths, env_file)`` for ``inst``.

    This is the single operator-side compose-state resolver (anti-hack rule 4 —
    no parallel resolver exists). It returns the compose-file paths as a list
    (not a pre-joined ``-f f1 -f f2`` string) so the Q6 wire-expansion producer
    (:func:`_expand_compose_wire`) can emit one ``--compose-file`` flag per
    path; the pure wire-keyed builder (:func:`_build_compose_wire_argv`) is the
    only place ``-f`` joining happens, so the Python builder and the Go
    assembly stay byte-identical via the shared fixture.

    Raises:
        DispatchValidationError: the instance is not registered.
    """
    entry = InstanceRegistry().get(inst)
    if entry is None:
        raise DispatchValidationError(f"no sandbox instance named {inst!r} is registered")
    instance_dir = entry.instance_dir
    config = InstanceConfig.from_toml(os.path.join(instance_dir, "sandbox.toml"))
    files = [os.path.join(instance_dir, "docker", "compose.yml")]
    if config.components_db_postgres.enabled:
        files.append(os.path.join(instance_dir, "docker", "extras", "db-postgres.yml"))
    if config.components.mcp_firecrawl:
        files.append(os.path.join(instance_dir, "docker", "extras", "mcp-firecrawl.yml"))
    project_name = compose_project_name(inst)
    env_file = os.path.join(instance_dir, ".sandbox.env")
    return project_name, files, env_file


# ─── Q6 compose-op wire expansion + pure wire-keyed builder ─────────────────
#
# Q6 splits the compose path so compose target-argv is a *pure function of the
# wire inputs* (and therefore lives in the one shared fixture alongside the
# seven deterministic ops):
#
#   (a) ``_expand_compose_wire`` — the wire-expansion *producer*. It takes the
#       typed args ``[<inst>]`` (+ ``["--volumes"]`` for a compose-down
#       destroy), resolves dev-context state via the single resolver
#       ``_resolve_compose_state``, and emits the named-flag wire form
#       ``[<inst>, "--project", P, "--env-file", E, "--compose-file", f1, …]``
#       (+ trailing ``"--volumes"``). Used only by :func:`invoke`.
#   (b) ``_build_compose_wire_argv`` — the *pure* wire-keyed target-argv
#       builder. It parses the named flags (no state resolution) and assembles
#       the bash string with an op-hardcoded verb. This is what
#       ``target_argv_cases.json`` pins for the compose ops and what the Go
#       ``main_test.go`` must match byte-for-byte.

# The compose verb is op-hardcoded HERE (and identically in Go) — it is NEVER
# taken from the wire. ``--volumes`` only flips compose-down's verb to
# ``down -v``; it can never select a different docker compose subcommand.
_COMPOSE_VERB: dict[str, str] = {
    "compose-up": "up -d --build --wait",
    "compose-down": "down",
    "compose-ps": "ps --format json",
}


def _expand_compose_wire(op: str, args: Sequence[str]) -> list[str]:
    """Expand typed compose args to the Q6 named-flag wire form.

    ``args`` is the validated typed form: ``[<inst>]`` for compose-up/compose-ps,
    ``[<inst>]`` or ``[<inst>, "--volumes"]`` for compose-down. Returns
    ``[<inst>, "--project", P, "--env-file", E, "--compose-file", f1, …]`` with a
    trailing ``"--volumes"`` iff the compose-down destroy path requested it.
    """
    inst = args[0]
    project_name, compose_files, env_file = _resolve_compose_state(inst)
    wire = [inst, "--project", project_name, "--env-file", env_file]
    for f in compose_files:
        wire.extend(["--compose-file", f])
    if op == Op.COMPOSE_DOWN.value and len(args) == 2:
        wire.append("--volumes")
    return wire


def _parse_compose_wire(op: str, wire: Sequence[str]) -> tuple[str, str, list[str], bool]:
    """Parse the post-expansion compose wire form (mirrors the Go parser).

    Returns ``(project, env_file, compose_files, volumes)``. Raises
    :class:`DispatchValidationError` for a missing/duplicated/illegal flag — the
    same rejections the Go binary performs (kept in lockstep deliberately so the
    fixture exercises one shared shape).
    """
    if not wire:
        raise DispatchValidationError(f"{op}: missing <instance> in wire form")
    rest = list(wire[1:])
    project: str | None = None
    env_file: str | None = None
    compose_files: list[str] = []
    volumes = False
    i = 0
    while i < len(rest):
        flag = rest[i]
        if flag == "--volumes":
            if op != Op.COMPOSE_DOWN.value:
                raise DispatchValidationError(f"{op}: --volumes is only valid for compose-down")
            if volumes:
                raise DispatchValidationError(f"{op}: --volumes given more than once")
            volumes = True
            i += 1
            continue
        if i + 1 >= len(rest):
            raise DispatchValidationError(f"{op}: flag {flag!r} is missing its value")
        value = rest[i + 1]
        if flag == "--project":
            if project is not None:
                raise DispatchValidationError(f"{op}: --project given more than once")
            project = value
        elif flag == "--env-file":
            if env_file is not None:
                raise DispatchValidationError(f"{op}: --env-file given more than once")
            env_file = value
        elif flag == "--compose-file":
            compose_files.append(value)
        else:
            raise DispatchValidationError(f"{op}: unrecognized flag {flag!r}")
        i += 2
    if project is None:
        raise DispatchValidationError(f"{op}: --project is required exactly once")
    if env_file is None:
        raise DispatchValidationError(f"{op}: --env-file is required exactly once")
    if not compose_files:
        raise DispatchValidationError(f"{op}: at least one --compose-file is required")
    return project, env_file, compose_files, volumes


def _build_compose_wire_argv(op: str, wire: Sequence[str]) -> list[str]:
    """Pure wire-keyed compose target-argv builder (byte-identical to Go).

    Consumes the post-expansion wire form (``<inst> --project P --env-file E
    --compose-file f1 …``) and assembles the ``bash -c`` string with the
    op-hardcoded verb. No dev-context state is resolved here — that already
    happened operator-side in :func:`_expand_compose_wire`.
    """
    project, env_file, compose_files, volumes = _parse_compose_wire(op, wire)
    files_str = " ".join(f"-f {f}" for f in compose_files)
    verb = _COMPOSE_VERB[op]
    if op == Op.COMPOSE_DOWN.value and volumes:
        verb = "down -v"
    env_prefix = (
        f"TERM=dumb NO_COLOR=1 BUILDKIT_PROGRESS=plain COMPOSE_PROJECT_NAME={project}"
    )
    if op == Op.COMPOSE_PS.value:
        inner = (
            f"{env_prefix} docker compose {files_str} "
            f"--env-file {env_file} --ansi never {verb}"
        )
    else:
        inner = (
            f"{env_prefix} docker compose {files_str} "
            f"--ansi never --env-file {env_file} {verb}"
        )
    return _bash_c(inner)


def _build_compose_up(args: Sequence[str], host_config: HostConfig) -> list[str]:
    return _build_compose_wire_argv(Op.COMPOSE_UP.value, args)


def _build_compose_down(args: Sequence[str], host_config: HostConfig) -> list[str]:
    return _build_compose_wire_argv(Op.COMPOSE_DOWN.value, args)


def _build_compose_ps(args: Sequence[str], host_config: HostConfig) -> list[str]:
    return _build_compose_wire_argv(Op.COMPOSE_PS.value, args)


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


def build_invocation(
    op: Op | str,
    args: Sequence[str],
    host_config: HostConfig,
) -> list[str]:
    """Build the boundary-crossing argv for ``op``/``args`` WITHOUT executing it.

    This is the single command-construction seam (anti-hack rules 4 + 7): it
    performs everything :func:`invoke` does *except* the
    :class:`~core.executor.Executor` run — per-op validation, the Q6 compose
    wire-expansion / deterministic passthrough, the ``dispatch <op> <wire>``
    inner string, and the ``machinectl_cmd`` + ``bash -c`` crossing. :func:`invoke`
    is exactly ``Executor().run(build_invocation(...), sentinel=True,
    timeout=...)``; the
    ``sandbox start --dry-run`` preview and :class:`core.actions.ComposeUpAction`
    derive their displayed/executed command from this same function so no
    parallel argv/inner construction exists anywhere.

    Returns the argv list ``[*machinectl_cmd(user, auth), "/bin/bash", "-c",
    "<dispatch-binary> <op> <shlex.join(wire_args)>"]`` (design D2 — the outer
    argv shape is preserved so operators' existing sudoers rule still matches).

    Raises:
        DispatchValidationError: the typed args are malformed for ``op`` (raised
            before any boundary-crossing argv is built).
        ValueError: ``op`` is not a known :class:`Op`.
    """
    resolved = Op(op)
    op_value = resolved.value
    validate_args(resolved, args)
    wire_args = (
        _expand_compose_wire(op_value, args)
        if op_value in _COMPOSE_VERB
        else list(args)
    )
    inner = f"{_DISPATCH_BINARY} {op_value} {shlex.join(wire_args)}".rstrip()
    return [
        *machinectl_cmd(
            host_config.host.docker_unprivileged_user,
            host_config.host.machinectl_authentication,
        ),
        "/bin/bash",
        "-c",
        inner,
    ]


# Layer map: invoke() = Executor().run(build_invocation(...), sentinel=True);
# build_invocation() = machinectl-crossed argv; build_target_argv() = the
# inner dispatcher-spawned argv.
def invoke(
    op: Op | str,
    args: list[str],
    host_config: HostConfig,
    *,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Validate ``op``/``args`` and run the dispatcher across the privilege boundary.

    Contract (load-bearing — the orchestrator callers and the Go dispatcher
    depend on this signature):

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

    Returns the completed :class:`subprocess.CompletedProcess` of the
    boundary-crossing invocation (produced by the sterile
    :class:`core.executor.Executor`).

    The crossing runs with the sterile ``Executor``'s ``sentinel=True``
    mechanism: ``machinectl shell`` does NOT propagate the inner
    ``/bin/bash -c`` payload's exit code (it exits 0 even when that payload
    fails), so the Executor injects ``…; echo __SANDBOX_EXIT_<tok>_$?``,
    parses the real in-container exit, and raises
    :class:`~core.exceptions.SandboxExecutionError` on a non-zero inner
    exit. Without this every dispatched op's failure would be silently
    masked as success — :func:`probe` (which wraps :func:`invoke` and
    catches that error) and the doctor verdicts depend on the recovered
    inner exit being faithful.

    Flow (Q6):

    1. Resolve + validate the *typed* args (the unchanged "Per-Op Argument
       Validation" surface).
    2. For the three compose ops, expand the typed args to the Q6 named-flag
       wire form via :func:`_expand_compose_wire` (the single operator-side
       resolver ``_resolve_compose_state``). The deterministic ops pass their
       typed args through verbatim.
    3. Cross the privilege boundary in the backward-compatible shape
       ``[*machinectl_cmd(user, auth), "/bin/bash", "-c",
       "<dispatch-binary> <op> <shlex.join(wire_args)>"]`` (design D2 — the
       outer argv shape is preserved so operators' existing sudoers rule still
       matches).

    The ``--check`` short-circuit is purely a Go concern (it never reaches
    Python); :func:`invoke` does not special-case it.

    The command construction (validate -> Q6 wire-expand / passthrough ->
    inner -> crossed argv) lives in :func:`build_invocation`; :func:`invoke`
    is exactly that argv handed to the sterile :class:`~core.executor.Executor`
    with ``sentinel=True`` (anti-hack rules 4 + 7 — one seam, no parallel
    construction).
    """
    return Executor().run(
        build_invocation(op, args, host_config), sentinel=True, timeout=timeout
    )


def probe(
    op: Op | str,
    args: list[str],
    host_config: HostConfig,
    *,
    timeout: float | None = None,
) -> ProbeOutcome:
    """Run ``op``/``args`` like :func:`invoke`, but return a typed outcome.

    Q8 entry point for *probe-style* callers (doctor checks; the cli
    ``auth-probe`` preflight) that must branch on success/failure/timeout and
    emit ``pass``/``fail``/``skip`` verdicts instead of crashing on an
    *expected* failure. :func:`invoke` keeps its raise-on-failure contract
    verbatim (helper/compose are unaffected — zero blast radius on that path);
    this is the SINGLE place the ``SandboxExecutionError`` catch and the
    ``exc.__cause__`` timeout discrimination live (the sterile ``Executor``
    chains the originating :class:`subprocess.TimeoutExpired` via
    ``raise ... from e``).

    Returns:
        :class:`ProbeOutcome` — ``ok`` + the success stdout (``message`` is
        ``""``), or ``ok=False`` with ``timed_out`` reflecting whether the
        underlying failure was a subprocess timeout and ``message`` carrying
        the ``SandboxExecutionError`` failure text (the sterile ``Executor``'s
        informative exit-status / ``OSError`` context) for probe-style callers
        to surface in their operator-facing ``detail``.
    """
    try:
        cp = invoke(op, args, host_config, timeout=timeout)
    except SandboxExecutionError as exc:
        return ProbeOutcome(
            ok=False,
            timed_out=isinstance(exc.__cause__, subprocess.TimeoutExpired),
            stdout="",
            message=str(exc),
        )
    return ProbeOutcome(ok=True, timed_out=False, stdout=cp.stdout, message="")


# ─── Offline reproducible compile recipe ────────────────────────────────────
#
# The dispatcher binary is compiled per-host at setup-install-time (sister
# change ``sandbox-setup``'s L6.5 phase) inside the pinned ``golang:1.23-alpine``
# image, offline (``--network none``), against vendored deps. There is no host
# Go toolchain, so this docker container is the ONLY place ``main_test.go``'s
# Python<->Go target-argv fixture-parity suite runs: the recipe runs
# ``go test ./...`` BEFORE ``go build`` in the SAME invocation (spec
# "Target Argv Construction Per Op" C-e + "Offline Reproducible Compile
# Recipe"). A fixture drift fails ``go test`` -> the ``&&`` short-circuits ->
# ``go build`` never runs -> no ``/build/dispatch`` is produced, the non-zero
# container exit propagates as :class:`~core.exceptions.SandboxExecutionError`,
# and no binary is ever placed at ``output_path``. Two *successful* compiles of
# the same source against the same pinned image are byte-identical
# (``-trimpath`` strips embedded paths; ``go test`` does not write the output).
#
# Build-dir transport (Finding L — ratified handoff §11). The host NEVER names
# or stages a build directory: the rootless docker daemon runs in the
# claude-sandbox session whose ``user@.service`` has ``PrivateTmp=`` (so dev's
# ``/tmp`` *and* ``/var/tmp`` are invisible to it), and granting the daemon an
# ACL onto a shared operator-tree path would leak a persistent privilege grant.
# Instead the source is *embedded* in the crossed payload and the binary is
# *captured* back over stdout, so neither caller (root ``sandbox setup`` L6.5
# nor the dev integration test) ever touches the build dir:
#
#   * source-in:  the host tars ``_DISPATCH_SOURCE_ENTRIES`` (gzip-9) and
#     base64-w0-encodes it (~20 KB, 6x under Linux ``MAX_ARG_STRLEN`` 131072);
#     the literal is interpolated into the ``bash -c`` payload.
#   * build dir:  the crossed script, running AS claude-sandbox, derives
#     ``RD="/run/user/$(id -u)"`` and does
#     ``DIR="$(mktemp -d "$RD/sandbox-ai-build-XXXXXX")"`` —
#     a per-call tmpfs dir under ``/run/user/<sb-uid>`` (0700, owner-only;
#     ancestors ``/run`` + ``/run/user`` are 0755 → ZERO operator-tree ACLs).
#     ``/run/user/<uid>`` is the lingering daemon user's per-user runtime dir,
#     created by ``systemd-logind`` independent of any login session, so it is
#     reachable under the PAM-skipping ``pipe_cmd`` crossing where
#     ``$XDG_RUNTIME_DIR`` is unset; linger is therefore an architectural
#     prerequisite (sister-change ``sandbox-setup`` L5 hard-requires
#     ``loginctl enable-linger`` for rootless dockerd, so no linger ⇒ no
#     ``/run/user/<uid>`` ⇒ no daemon ⇒ no compile). A fail-closed
#     ``[ -d "$RD" ]`` guard makes an absent runtime dir exit non-zero.
#   * cleanup:    ``trap 'rm -rf "$DIR"' EXIT`` is armed immediately, BEFORE
#     any work, so it fires on success AND every failure path; tmpfs
#     evaporation is only a SIGKILL-before-trap backstop. ``Executor`` wraps
#     the payload as ``{ <inner>; }; echo __SANDBOX_EXIT_..._$?`` so the trap
#     fires AFTER stdout is captured and the sentinel echoed — the binary is
#     captured before cleanup, no race.
#   * binary-out: docker/go chatter is redirected to stderr (``1>&2``) so
#     stdout carries ONLY ``base64 -w0 "$DIR/dispatch"`` (no ``\n``/``\r`` →
#     PTY-``onlcr``-safe). The host ``.strip()``s + ``base64.b64decode``s
#     ``result.stdout`` and writes ``output_path`` (mode 0755) ONLY after the
#     crossing exits 0; any failure raises ``SandboxExecutionError`` before
#     the write, so ``output_path`` is untouched on every failure path.
#
# Reproducibility is location-neutral: the container always bind-mounts the
# ephemeral dir at the fixed ``_BUILD_MOUNT_DST`` and ``-trimpath`` strips
# module paths, so two compiles into two distinct ``mktemp`` dirs are
# byte-identical.


def _dispatch_source_b64() -> str:
    """Return a gzip-9 + base64-w0 tar of the Go dispatcher source tree.

    Tars ``src/templates/dispatch/{main.go, main_test.go, go.mod, go.sum,
    vendor/, fixtures/}`` from the shipped ``templates`` package (resolved via
    :func:`importlib.resources.files`, so it works from both the source tree
    and an installed wheel). The resources may be a real directory OR an
    :mod:`importlib.resources` traversable inside a wheel; either way they are
    materialised into a throwaway :class:`tempfile.TemporaryDirectory` purely
    to build the tar bytes (this temp dir is NOT the build dir — it is never
    bind-mounted and has no daemon-reachability constraint) and discarded.

    The tar is built deterministically (sorted member order, fixed mtime /
    mode / uid / gid) and gzipped with an explicit ``mtime=0`` — the gzip
    *stream header* carries its own modification-time field that the per-member
    :func:`_deterministic_tarinfo` does NOT normalise, so the tar is produced
    uncompressed and then gzipped separately with ``mtime=0`` (a plain
    ``tarfile.open(mode="w:gz")`` would stamp wall-clock time into that header
    and break byte-determinism). The embedded payload therefore contributes
    nothing host- or time-specific to reproducibility. Returns an ASCII string
    containing only ``[A-Za-z0-9+/=]`` — safe to interpolate into a
    single-quoted shell literal.
    """
    dispatch_root = _resource_files("templates").joinpath("dispatch")
    with tempfile.TemporaryDirectory() as staging:
        for entry in _DISPATCH_SOURCE_ENTRIES:
            _stage_resource(dispatch_root.joinpath(entry), os.path.join(staging, entry))
        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w") as tar:
            for entry in sorted(_DISPATCH_SOURCE_ENTRIES):
                tar.add(
                    os.path.join(staging, entry),
                    arcname=entry,
                    recursive=True,
                    filter=_deterministic_tarinfo,
                )
    gz_buf = io.BytesIO()
    with gzip.GzipFile(fileobj=gz_buf, mode="wb", compresslevel=9, mtime=0) as gz:
        gz.write(tar_buf.getvalue())
    return base64.b64encode(gz_buf.getvalue()).decode("ascii")


def _deterministic_tarinfo(ti: tarfile.TarInfo) -> tarfile.TarInfo:
    """Normalise a :class:`tarfile.TarInfo` so the tar bytes are reproducible.

    Strips host- and time-specific metadata (mtime, owner ids/names, and the
    on-disk mode) so the embedded source payload is a pure function of file
    *content* + layout — never the building host or the moment of build.
    """
    ti.mtime = 0
    ti.uid = 0
    ti.gid = 0
    ti.uname = ""
    ti.gname = ""
    ti.mode = 0o755 if ti.isdir() else 0o644
    return ti


def _stage_resource(src: Traversable, dst: str) -> None:
    """Recursively copy a traversable resource (file or directory) to ``dst``."""
    if src.is_dir():
        os.makedirs(dst, exist_ok=True)
        for child in src.iterdir():
            _stage_resource(child, os.path.join(dst, child.name))
    else:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as fh:
            fh.write(src.read_bytes())


def _compile_payload(image: str, source_b64: str) -> str:
    """Build the ``bash -c`` payload run AS claude-sandbox across the crossing.

    The payload (Finding L — ratified handoff §11):

    1. ``RD="/run/user/$(id -u)"``; a fail-closed ``[ -d "$RD" ]`` guard;
       ``DIR="$(mktemp -d "$RD/sandbox-ai-build-XXXXXX")"`` — a per-call tmpfs
       build dir under the lingering daemon user's per-user runtime dir,
       created by ``systemd-logind`` independent of any login session, so it
       is reachable under the PAM-skipping ``pipe_cmd`` crossing where
       ``$XDG_RUNTIME_DIR`` is unset; linger is therefore an architectural
       prerequisite (sister-change ``sandbox-setup`` L5). The ``[ -d "$RD" ]``
       guard makes an absent runtime dir fail loudly (non-zero exit).
    2. ``trap 'rm -rf "$DIR"' EXIT`` armed BEFORE any work → fires on success
       AND every failure path.
    3. decode the embedded source: ``printf %s '<b64>' | base64 -d |
       tar -xz -C "$DIR"`` (the literal is single-quoted; it contains only
       ``[A-Za-z0-9+/=]`` so it has no shell metacharacters).
    4. ``docker run`` the unchanged offline recipe with bind-src ``"$DIR"``,
       with ALL docker/go stdout redirected to stderr (``1>&2``) so build
       chatter never pollutes the captured stream. Because the crossing is
       :func:`~core.host_config.pipe_cmd` (a real byte pipe, no PTY where
       stdout ≡ stderr), this ``1>&2`` separation is genuine — go/docker
       chatter lands on a *distinct* stderr stream and never interleaves with
       the binary on stdout.
    5. on success emit ONLY the binary to stdout: ``base64 -w0 "$DIR/dispatch"``.
       The crossing carries this as a raw byte frame over ``pipe_cmd`` (no PTY
       → no ``onlcr`` ``\\n``→``\\r\\n`` rewrite, unbounded stream); no exit
       sentinel is echoed (``pipe_cmd`` propagates the inner exit, so
       :class:`~core.executor.Executor` runs with the default ``sentinel=False``
       and ``check=True`` raises on any non-zero exit).

    ``GOFLAGS=-mod=vendor`` is delivered via ``docker run --env`` so it is set
    INSIDE the build container (a host-side prefix would only set it on the
    host docker *client* and never reach the in-container build); ``--env``
    does not affect ``--network none``. This is the single source of truth for
    the setting (it is not also set host-side).
    """
    docker_run = (
        "docker run --rm "
        "--network none "
        "--env GOFLAGS=-mod=vendor "
        f'--mount type=bind,src="$DIR",dst={_BUILD_MOUNT_DST} '
        f"--workdir {_BUILD_MOUNT_DST} "
        f"{shlex.quote(image)} "
        f"/bin/sh -c {shlex.quote(_COMPILE_INNER)}"
    )
    return (
        'RD="/run/user/$(id -u)"; '
        '[ -d "$RD" ] || { echo "sandbox-ai: per-user runtime dir $RD absent '
        '(is the daemon user lingering? sister-change L5 enables linger)" 1>&2; exit 1; }; '
        'DIR="$(mktemp -d "$RD/sandbox-ai-build-XXXXXX")"; '
        "trap 'rm -rf \"$DIR\"' EXIT; "
        f"printf %s '{source_b64}' | base64 -d | tar -xz -C \"$DIR\" 1>&2; "
        f"{docker_run} 1>&2; "
        'base64 -w0 "$DIR/dispatch"'
    )


def compile_dispatcher(
    output_path: str,
    host_config: HostConfig,
) -> None:
    """Compile the Go dispatcher offline, reproducibly, across the boundary.

    The host embeds the dispatcher source (gzip+base64 tar) in a single
    ``bash -c`` payload crossed via :func:`~core.host_config.pipe_cmd` — NOT
    :func:`~core.host_config.machinectl_cmd`. The 10 runtime ops
    (:func:`invoke`/:func:`probe`) cross via ``machinectl_cmd`` because they
    carry small text results; this compile recipe carries a multi-MB **binary
    frame** (the built dispatcher binary, base64'd on stdout), so it MUST use
    the byte-pipe primitive: ``machinectl_cmd`` allocates a PTY where
    ``stdout ≡ stderr`` and whose ``onlcr`` line discipline would corrupt the
    stream, while ``pipe_cmd`` is a real byte pipe with distinct stdout/stderr
    and no ``onlcr`` (see :func:`~core.host_config.pipe_cmd` for the underlying
    transport). This is the correct application of the CLAUDE.md
    byte-pipe-for-binary-frames doctrine, not a violation of the "dispatcher is
    minimal / machinectl for ops" stance. ``pipe_cmd`` is auth-mode-independent
    (its ``manage-units`` polkit action is the only authorization layer); the
    per-host ``machinectl_authentication`` setting is unused on this path. The PAM-skip
    trade-off is acceptable here per the boundary-primitive doctrine: this is a
    fixed, audited, one-shot build path with a session-bounded lifetime.

    Running AS the unprivileged docker user, that payload ``mktemp -d``'s an
    ephemeral build dir under ``/run/user/$(id -u)`` — the lingering daemon
    user's per-user runtime dir, created by ``systemd-logind`` independent of
    any login session, so it is reachable under the PAM-skipping ``pipe_cmd``
    crossing where ``$XDG_RUNTIME_DIR`` is unset (linger is therefore an
    architectural prerequisite, sister-change ``sandbox-setup`` L5) — (tmpfs,
    claude-sandbox-owned, ZERO operator-tree ACLs), arms a ``trap … EXIT``
    cleanup, unpacks the
    source, and in ONE ``docker run --rm --network none`` invocation inside the
    digest-pinned ``IMAGE_REGISTRY["golang_alpine"]`` image with
    ``GOFLAGS=-mod=vendor`` runs::

        go test ./... && go build -trimpath -ldflags '-s -w' -o /build/dispatch .

    ``go test ./...`` (the ``main_test.go`` Python<->Go fixture-parity suite)
    runs BEFORE ``go build`` in the same container (spec C-e): a fixture drift
    fails ``go test``, the ``&&`` short-circuits, ``go build`` never runs, and
    no ``/build/dispatch`` is produced. All docker/go chatter is redirected to
    stderr — and because ``pipe_cmd`` keeps stderr genuinely distinct from
    stdout, the only thing on stdout is the final ``base64 -w0`` of the built
    binary. The crossing runs through the sterile
    :class:`~core.executor.Executor` with the default ``sentinel=False``:
    ``pipe_cmd`` propagates the inner ``/bin/bash -c`` exit, so the Executor's
    ``check=True`` raises :class:`~core.exceptions.SandboxExecutionError` on any
    non-zero exit (absent ``/run/user/$(id -u)`` via the ``[ -d "$RD" ]``
    guard, ``go test`` fixture drift, ``go build`` failure, container start
    failure, or timeout) WITHOUT
    needing a sentinel echo. On exit 0 the host strips + base64-decodes
    ``result.stdout`` (the byte-pipe crossing emits no stdout banner, and there
    is no PTY so no ``\\r``/``onlcr`` — ``.strip()`` handles any trailing
    newline) and writes it to ``output_path`` mode ``0o755``; any failure raises BEFORE
    the write, so ``output_path`` is untouched on every failure path. The
    ephemeral build dir self-cleans (``trap``) on success AND failure — no
    operator-tree residue, no ACL to revoke.

    Args:
        output_path: Host path the freshly-built binary is written to (mode
            ``0o755``) on success. Untouched on any failure. The build dir is
            derived inside the crossing — callers never supply or see it.
        host_config: Resolved :class:`~core.host_config.HostConfig` supplying
            the unprivileged docker user for the byte-pipe boundary crossing.
            The ``machinectl_authentication`` field is unused on this path
            (``pipe_cmd`` is auth-mode-independent); the parameter is retained
            for signature symmetry with :func:`invoke`.

    Raises:
        SandboxExecutionError: ``go test`` failed (fixture drift / build
            failure), the container could not start, it timed out, or
            ``/run/user/$(id -u)`` was absent (the ``[ -d "$RD" ]`` guard
            fired). No binary is placed at
            ``output_path`` in any failure case.
    """
    image = IMAGE_REGISTRY["golang_alpine"].pinned
    payload = _compile_payload(image, _dispatch_source_b64())
    cmd = [
        *pipe_cmd(host_config.host.docker_unprivileged_user),
        "/bin/bash",
        "-c",
        payload,
    ]
    # ``pipe_cmd`` propagates the inner ``/bin/bash -c`` exit code (its
    # byte-pipe transport, unlike machinectl's PTY), so the Executor's
    # default ``check=True`` raises
    # SandboxExecutionError on a non-zero ``go test`` (fixture drift), ``go
    # build``, container-start failure, timeout, or absent
    # ``/run/user/$(id -u)`` (the ``[ -d "$RD" ]`` guard)
    # WITHOUT a sentinel. The decode+write below runs ONLY on a clean return,
    # so no binary is placed at ``output_path`` on any failure path.
    result = Executor().run(cmd)
    binary = base64.b64decode(result.stdout.strip())
    with open(output_path, "wb") as fh:
        fh.write(binary)
    os.chmod(output_path, 0o755)
