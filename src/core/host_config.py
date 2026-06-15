# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Project-wide configuration: the per-host config schema, loader, and machinectl command builder.

Defines the per-host configuration that holds host-level settings (docker
unprivileged user, execution mode, workspace bridge group). The host facts are
setup-determined and sourced from the root-owned setup-state marker (written by
``sudo sandbox setup``) via :meth:`HostConfig.from_marker` — there is no
user-editable host toml. Consumed by CLI commands and the doctor module to
determine the privilege-crossing strategy for machinectl invocations.

Also exposes subuid/subgid resolvers and the workspace bridge group helpers
used by the helper-container ACL/ownership recipes.
"""

import getpass
import grp
import os
import pwd
import re
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, model_validator

from core.exceptions import SandboxExecutionError

__all__ = [
    "DEFAULT_PROVISIONING_MODE",
    "USERNAME_RE",
    "DockerExecutionMode",
    "HostConfig",
    "HostSettings",
    "NoFreeGidInSubgidRangeError",
    "NoSubgidRangeError",
    "NoSubuidRangeError",
    "SubgidOutOfRangeError",
    "SubuidOutOfRangeError",
    "WorkspaceBridgeGroupMissingError",
    "autodetect_workspace_bridge_gid_recommendation",
    "ensure_per_user_state",
    "host_gid_for_in_container",
    "host_id_for_in_container",
    "in_container_gid_for_host_gid",
    "in_container_uid_for_host_uid",
    "ipam_lock_path",
    "is_operator_rootless",
    "machinectl_cmd",
    "minimal_host_config",
    "parse_subgid_for_user",
    "parse_subuid_for_user",
    "pipe_cmd",
    "registry_lock_path",
    "resolve_daemon_owner",
    "resolve_daemon_owner_settings",
    "sandbox_ai_home",
    "state_lock_path",
    "sudo_as_operator",
    "sudo_pipe_cmd",
    "workspace_bridge_gid",
    "workspace_bridge_group_for",
]

# POSIX-portable username grammar (M-1). ``docker_unprivileged_user`` reaches the
# ``--uid={user}`` operand of ``systemd-run``/``pipe_cmd`` and the rendered
# sudoers ``Cmnd_Spec``, so a value carrying a space, an uppercase letter, or a
# sudoers/shell metacharacter (``;``, ``--property=…``) could corrupt the
# emitted rule or the crossing argv. Reject any non-conforming value at the
# Pydantic boundary: lowercase initial letter or underscore, then up to 31 more
# of ``[a-z0-9_-]`` (32-char useradd ceiling).
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def sandbox_ai_home() -> Path:
    """Resolve the per-user sandbox-ai home directory.

    Honors ``SANDBOX_AI_HOME`` (test-isolation override). Otherwise
    returns ``~/.sandbox-ai`` expanded for the current user.
    """
    return Path(os.environ.get("SANDBOX_AI_HOME") or os.path.expanduser("~/.sandbox-ai"))


def state_lock_path() -> Path:
    """Canonical fcntl lock target serializing all per-user state mutations."""
    return sandbox_ai_home() / "state" / "state.lock"


def ipam_lock_path() -> Path:
    """Dedicated fcntl lock target guarding IPAM ledger mutations.

    Distinct from :func:`state_lock_path`: the per-user ``state.lock`` guards
    the provisioning sequence as a whole, while ``ipam.json.lock`` is the
    finer-grained mutation lock acquired internally by :class:`IPAMLedger`.
    Lock acquisition order, when both are held by the same caller, is
    ``state.lock`` outer, ``ipam.json.lock`` inner — never the reverse.
    """
    return sandbox_ai_home() / "state" / "ipam.json.lock"


def registry_lock_path() -> Path:
    """Dedicated fcntl lock target guarding instance registry mutations.

    Distinct from :func:`state_lock_path`: the per-user ``state.lock`` guards
    the provisioning sequence as a whole, while ``instances.json.lock`` is the
    finer-grained mutation lock acquired internally by :class:`InstanceRegistry`.
    Lock acquisition order, when both are held by the same caller, is
    ``state.lock`` outer, ``instances.json.lock`` inner — never the reverse.
    """
    return sandbox_ai_home() / "state" / "instances.json.lock"


def ensure_per_user_state(home: Path) -> None:
    """Create the per-user state tree with mode ``0700``.

    Creates ``<home>/``, ``<home>/config/``, ``<home>/state/``,
    ``<home>/instances/``, and ``<home>/workspaces/``. Idempotent:
    ``exist_ok=True`` suppresses ``FileExistsError`` and does NOT
    modify the mode of any pre-existing directory.
    """
    os.makedirs(home, mode=0o700, exist_ok=True)
    os.makedirs(home / "config", mode=0o700, exist_ok=True)
    os.makedirs(home / "state", mode=0o700, exist_ok=True)
    os.makedirs(home / "instances", mode=0o700, exist_ok=True)
    os.makedirs(home / "workspaces", mode=0o700, exist_ok=True)


class DockerExecutionMode(StrEnum):
    """Selects how Docker runtime ops reach the daemon.

    ``SEPARATE_USER`` is the existing behavior: Docker runs as the dedicated
    ``docker_unprivileged_user`` and every op crosses the ``machinectl``
    privilege boundary. ``OPERATOR_ROOTLESS`` runs rootless Docker as the
    operator's own user, invoking ops as local subprocesses with no boundary
    crossing.
    """

    SEPARATE_USER = "separate-user"
    OPERATOR_ROOTLESS = "operator-rootless"


# The single execution-mode default for the WHOLE system (finding F-051): the mode
# `sandbox setup` provisions when the operator passes no `--docker-execution-mode`
# flag and the marker has no entry, AND the moot in-memory carrier default on
# `HostSettings.docker_execution_mode` / `minimal_host_config` / every mode-param
# default below. One value, one name — referenced, never re-literalised. The
# in-memory carrier value is moot at runtime (`_resolve_full_host_config` always
# overlays the marker-resolved mode), so pointing it here is behavior-preserving; the
# value is single-sourced here so the codebase never carries two opposite-valued
# defaults. The `tests/unit/test_conventions.py::test_no_bare_mode_literal_defaults`
# gate forbids a bare `DockerExecutionMode` member in any param/field default
# position outside this module.
DEFAULT_PROVISIONING_MODE = DockerExecutionMode.OPERATOR_ROOTLESS


class HostSettings(BaseModel):
    """Per-host settings, sourced from the setup-state marker (not a toml)."""

    docker_unprivileged_user: str | None

    @model_validator(mode="after")
    def _validate_docker_unprivileged_user(self) -> HostSettings:
        """Enforce mode-conditional presence + POSIX grammar for the daemon user (M-1).

        ``docker_unprivileged_user`` is required-but-nullable: separate-user MUST
        carry a daemon user (the dedicated unprivileged owner), while
        operator-rootless tolerates ``None`` (the operator IS the owner, resolved
        at runtime via :func:`getpass.getuser`). When present, the value flows
        into the ``--uid={user}`` crossing operand and the sudoers ``Cmnd_Spec``,
        so reject empty / spaces / uppercase / shell- or sudoers-metacharacters
        before it can corrupt a rendered rule.
        """
        if self.docker_unprivileged_user is None:
            if self.docker_execution_mode is DockerExecutionMode.SEPARATE_USER:
                raise ValueError(
                    "docker_unprivileged_user is required in separate-user mode "
                    "(the dedicated unprivileged daemon owner)"
                )
            return self
        if USERNAME_RE.match(self.docker_unprivileged_user) is None:
            raise ValueError(
                f"docker_unprivileged_user {self.docker_unprivileged_user!r} is not a valid POSIX username "
                f"(must match {USERNAME_RE.pattern}: lowercase/underscore start, "
                f"then [a-z0-9_-], max 32 chars — no spaces, uppercase, or "
                f"shell/sudoers metacharacters)"
            )
        return self
    # Setup-determined (D11): the execution mode is NOT a user-editable field — it
    # is recorded in the per-operator setup-state marker by ``sudo sandbox setup``
    # and reaches the runtime via ``HostConfig.from_marker``. The field is
    # populated programmatically (by setup, by ``minimal_host_config``, and by
    # ``from_marker``), never from a host toml.
    docker_execution_mode: DockerExecutionMode = DEFAULT_PROVISIONING_MODE
    workspace_bridge_group: str = "sb-ws"


class HostConfig(BaseModel):
    """Top-level per-host config model, loaded from the setup-state marker."""

    host: HostSettings

    @classmethod
    def from_marker(cls, operator: str) -> HostConfig:
        """Build a HostConfig from the per-operator setup-state marker (D-B).

        The sole loader for the per-host config: the host facts are
        setup-determined and recorded in the root-owned setup-state marker (by
        ``sudo sandbox setup``), never a user-editable toml.

        Maps the marker entry's setup-determined host facts onto
        :class:`HostSettings`: ``mode → docker_execution_mode``,
        ``workspace_bridge_group → workspace_bridge_group``, and
        ``docker_unprivileged_user`` (``None`` for operator-rootless). The
        marker's ``workspace_bridge_gid`` is NOT consumed here — the bridge gid
        is re-resolved at runtime via :func:`grp.getgrnam`.

        Raises:
            ModeMarkerMissing: The marker is absent, has no entry for
                ``operator``, or carries a legacy mode-only record. Fail-closed
                so the caller surfaces "run `sudo sandbox setup` first".
        """
        # function-local import — one-way dep (setup_state imports host_config),
        # so a module-level import here would re-create the cycle.
        from core.setup_state import ModeMarkerMissing, read_entry

        entry = read_entry(operator)
        if entry is None:
            raise ModeMarkerMissing(
                f"no setup-state entry for operator {operator!r}. Run `sudo sandbox setup` first."
            )
        return cls(
            host=HostSettings(
                docker_unprivileged_user=entry.docker_unprivileged_user,  # None for op-rootless
                docker_execution_mode=entry.mode,
                workspace_bridge_group=entry.workspace_bridge_group,
            )
        )


def minimal_host_config(
    user: str, mode: DockerExecutionMode = DEFAULT_PROVISIONING_MODE
) -> HostConfig:
    """Build a HostConfig carrying only the fields the dispatch boundary reads.

    The fields are ``docker_unprivileged_user`` and ``docker_execution_mode``
    (defaulting to ``DEFAULT_PROVISIONING_MODE``).
    """
    return HostConfig(
        host=HostSettings(
            docker_unprivileged_user=user,
            docker_execution_mode=mode,
        )
    )


def is_operator_rootless(host_config: HostConfig) -> bool:
    """True iff host_config selects operator-rootless execution mode."""
    return host_config.host.docker_execution_mode is DockerExecutionMode.OPERATOR_ROOTLESS


def resolve_daemon_owner_settings(host: HostSettings) -> str:
    """Resolve the rootless-daemon owner from :class:`HostSettings` (design D7).

    ``separate-user`` → the configured ``docker_unprivileged_user``;
    ``operator-rootless`` → the **invoking user** (:func:`getpass.getuser`), the
    operator whose own user runs the daemon. The ``operator-rootless`` branch
    NEVER reads ``docker_unprivileged_user`` (it would resolve to the stale
    default ``"sandbox"`` and silently corrupt on-disk ownership). This is the
    single owner-resolution worker — the one sanctioned reader of
    ``docker_unprivileged_user`` for owner purposes — that the internal helpers
    holding only ``HostSettings`` (``workspace_bridge_gid``, hydration's
    bridge-gid translation, the workspace shared-group phase) route through.
    """
    if host.docker_execution_mode is DockerExecutionMode.OPERATOR_ROOTLESS:
        return getpass.getuser()
    if host.docker_unprivileged_user is None:
        raise ValueError("separate-user host config is missing docker_unprivileged_user")
    return host.docker_unprivileged_user


def workspace_bridge_group_for(operator: str, mode: DockerExecutionMode) -> str:
    """Derive the workspace bridge group NAME for ``operator`` under ``mode`` (D-F).

    ``operator-rootless`` → a per-operator name ``f"sb-ws-{operator}"`` (each
    operator is their own single-tenant daemon owner with their own subgid range,
    so the gid is per-operator and a group name maps to exactly one gid in
    ``/etc/group`` — the name must be per-operator too).
    ``separate-user`` → the shared ``"sb-ws"`` (one tenant, one range, one gid).

    This is THE single setup-side derivation of the per-operator bridge name.
    Runtime consumers do NOT call this — they read the name from the
    ``HostSettings.workspace_bridge_group`` FIELD (marker-sourced, Group 7).
    """
    if mode is DockerExecutionMode.OPERATOR_ROOTLESS:
        return f"sb-ws-{operator}"
    return "sb-ws"


def resolve_daemon_owner(host_config: HostConfig) -> str:
    """Resolve the rootless-daemon owner (design D7) — the ``HostConfig`` alias.

    The command-level resolver; delegates to :func:`resolve_daemon_owner_settings`.
    The runtime parallel of setup's ``daemon_owner_user(ctx)``.
    """
    return resolve_daemon_owner_settings(host_config.host)


def machinectl_cmd(user: str) -> list[str]:
    """Build the machinectl shell prefix for the given user.

    Returns:
        ``["sudo", "machinectl", "shell", "<user>@.host"]``.

    Caveat — PTY allocation: ``machinectl shell`` opens a PTY between caller
    and the spawned command. The PTY's ``onlcr`` line discipline rewrites
    every ``\\n`` byte in either direction to ``\\r\\n``. **Captured stdout
    therefore has CRLF line endings**, even when the underlying command
    emits LF. Callers that capture output (``subprocess.run(...,
    capture_output=True)``, shell ``$(... | head -1)``, etc.) MUST strip the
    ``\\r`` (``tr -d '\\r'`` or text-mode decode) before using the value as
    a filename, IP, hostname, or argv element. Passing a ``<value>\\r``
    downstream silently fails. For paths that carry binary frames (SSH,
    gRPC, raw TCP), use :func:`pipe_cmd` instead — it allocates no PTY and
    preserves bytes verbatim.
    """
    return ["sudo", "machinectl", "shell", f"{user}@.host"]


def pipe_cmd(user: str) -> list[str]:
    """Build the byte-pipe primitive for crossing into ``user`` without a PTY.

    Sibling to :func:`machinectl_cmd`. Where ``machinectl_cmd`` allocates a PTY
    (the right shape for interactive handoffs and helper-container ``exec`` paths
    that already speak to a real TTY), ``pipe_cmd`` produces a clean stdio pipe
    suitable for programmatic byte transports — most notably the SSH
    ``ProxyCommand`` path in ``cli-attach``.

    Returns:
        ``["systemd-run", "-q", "--pipe", f"--uid={user}"]``.

    Auth-mode independence: unlike :func:`machinectl_cmd`, no ``auth`` argument
    is accepted. ``systemd-run``'s ``manage-units`` polkit action is the only
    authorization layer; the per-host ``machinectl_authentication`` setting
    does not apply.

    PAM-skip trade-off: ``systemd-run`` does NOT invoke PAM, so policies on
    ``pam_limits.conf`` and similar do not apply to processes started this way.
    Acceptable for our use case — programmatic SSH-byte transport with a
    lifetime bounded by a single attach session — where the call site is
    a fixed, audited orchestrator path rather than a user-typed command.
    """
    return ["systemd-run", "-q", "--pipe", f"--uid={user}"]


def sudo_pipe_cmd(user: str) -> list[str]:
    """Build the *privileged* byte-pipe primitive: ``["sudo", *pipe_cmd(user)]``.

    The per-op-sudoers-authorized sibling of :func:`pipe_cmd`. Where ``pipe_cmd``
    crosses into ``user`` as an already-privileged caller (root setup phases, the
    SSH ``ProxyCommand`` path), ``sudo_pipe_cmd`` prepends ``sudo`` so an
    unprivileged operator can cross via a per-op sudoers rule that authorizes
    exactly this argv (separate-user + SUDO dispatch routing — design D2).

    It **delegates** to :func:`pipe_cmd` and never re-spells the transient-unit
    literal — ``pipe_cmd`` remains the single sanctioned home for that literal
    (design D1, single source of truth), so the convention guard
    ``test_no_raw_systemd_run_outside_pipe_cmd`` needs no extension.

    Takes only ``user`` — there is NO ``auth`` argument: the per-op sudoers rule
    is the sole authorization layer (the per-host ``machinectl_authentication``
    setting does not apply, exactly as for ``pipe_cmd``).

    It MUST never append ``--unit``/``--description``: the rendered argv has to
    stay byte-identical to the per-op sudoers ``Cmnd_Spec`` derived from this same
    function, or sudo will refuse the crossing.

    PAM-skip carries over from :func:`pipe_cmd`: the transient-unit launcher does
    not invoke PAM, so ``pam_limits.conf`` and similar policies do not apply to
    processes started this way (operator-signed-off — design D7).

    Returns:
        ``["sudo", *pipe_cmd(user)]`` — i.e. ``pipe_cmd``'s argv prefixed with
        ``"sudo"``.
    """
    return ["sudo", *pipe_cmd(user)]


def sudo_as_operator(operator: str) -> list[str]:
    """Build a NORMAL-PROCESS drop into ``operator`` (not a transient unit).

    Returns ``["sudo", "-u", operator]``. Unlike :func:`pipe_cmd` (which drops
    via a ``--uid`` transient *service* unit), this drops the current (root)
    process to ``operator`` in an ordinary child process.

    Use this — NOT ``pipe_cmd`` — whenever the command run as the operator is
    itself a **setuid** binary (``sudo``, …). Execing a setuid-root binary from
    inside :func:`pipe_cmd`'s ``--uid`` transient-unit context fails with systemd
    ``EXIT_EXEC`` (203) on a real host (observed: systemd 259 + SELinux), so the
    operator-side ``sudo machinectl …`` verification that L3a performs cannot go
    through ``pipe_cmd``. A normal-process ``sudo -u`` drop execs setuid ``sudo``
    fine, re-runs ``initgroups`` (so a post-``usermod`` group set is reflected,
    the property ``pipe_cmd``'s ``--uid`` was chosen for), and is faithful to how
    the operator invokes the boundary at runtime (their own login process runs
    ``sudo machinectl``, never a transient unit). See finding F-016.

    ``pipe_cmd`` remains correct for plain-binary operator crossings (L8) and is
    *required* for the SSH binary-frame path; this is the setuid-only sibling.
    """
    return ["sudo", "-u", operator]


# ─── Subuid / subgid resolvers ──────────────────────────────────────────────


class NoSubuidRangeError(SandboxExecutionError):
    """Host user has no /etc/subuid entry (or does not exist)."""


class SubuidOutOfRangeError(SandboxExecutionError):
    """In-container uid exceeds the user's allocated subuid range."""


class NoSubgidRangeError(SandboxExecutionError):
    """Host user has no /etc/subgid entry."""


class SubgidOutOfRangeError(SandboxExecutionError):
    """Host gid is not within any of the user's subgid ranges."""


class WorkspaceBridgeGroupMissingError(SandboxExecutionError):
    """The configured workspace bridge group does not exist on the host."""


class NoFreeGidInSubgidRangeError(SandboxExecutionError):
    """Autodetect found no available host gid in the user's subgid range."""


def _parse_subid_file(path: Path, host_user: str) -> list[tuple[int, int]]:
    try:
        content = path.read_text()
    except FileNotFoundError:
        return []
    ranges: list[tuple[int, int]] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) != 3:
            continue
        user, start_s, count_s = parts
        if user != host_user:
            continue
        try:
            ranges.append((int(start_s), int(count_s)))
        except ValueError:
            continue
    return ranges


# Module-level path constants — overridable in tests via monkeypatch.
# Production reads /etc/subuid and /etc/subgid; redirecting these constants
# is the precise way to test the parser without patching the global ``Path``.
_SUBUID_PATH: Path = Path("/etc/subuid")
_SUBGID_PATH: Path = Path("/etc/subgid")


def parse_subuid_for_user(host_user: str) -> list[tuple[int, int]]:
    """Return ``[(first_allocated, count), ...]`` for ``host_user`` from /etc/subuid.

    Multi-line entries are returned in file order. Empty list means the user
    has no entry (or /etc/subuid does not exist).
    """
    return _parse_subid_file(_SUBUID_PATH, host_user)


def parse_subgid_for_user(host_user: str) -> list[tuple[int, int]]:
    """Return ``[(first_allocated, count), ...]`` for ``host_user`` from /etc/subgid."""
    return _parse_subid_file(_SUBGID_PATH, host_user)


def host_id_for_in_container(N: int, host_user: str) -> int:
    """Forward map: in-container uid ``N`` → host uid for ``host_user``.

    For ``N == 0`` returns the user's primary uid via ``pwd.getpwnam``.
    For ``N >= 1`` walks the parsed ``/etc/subuid`` ranges in file order;
    range *i* covers in-container uids ``1..count_i`` after consuming all
    prior ranges' counts. Returns ``first_allocated + offset_within_range``.

    Raises:
        NoSubuidRangeError: ``host_user`` has no /etc/subuid entry (or does not exist).
        SubuidOutOfRangeError: ``N`` exceeds the total subuid space allocated to the user.
    """
    if N == 0:
        try:
            return pwd.getpwnam(host_user).pw_uid
        except KeyError as exc:
            raise NoSubuidRangeError(f"User {host_user!r} does not exist on this host") from exc
    if N < 0:
        raise SubuidOutOfRangeError(f"In-container uid must be non-negative, got {N}")
    ranges = parse_subuid_for_user(host_user)
    if not ranges:
        raise NoSubuidRangeError(f"User {host_user!r} has no /etc/subuid entry; rootless docker may not be configured")
    remaining = N
    for first, count in ranges:
        if remaining <= count:
            return first + remaining - 1
        remaining -= count
    raise SubuidOutOfRangeError(f"In-container uid {N} exceeds the subuid range allocated to {host_user!r}")


def host_gid_for_in_container(N: int, host_user: str) -> int:
    """Forward map for gids; see :func:`host_id_for_in_container`."""
    if N == 0:
        try:
            return pwd.getpwnam(host_user).pw_gid
        except KeyError as exc:
            raise NoSubgidRangeError(f"User {host_user!r} does not exist on this host") from exc
    if N < 0:
        raise SubgidOutOfRangeError(f"In-container gid must be non-negative, got {N}")
    ranges = parse_subgid_for_user(host_user)
    if not ranges:
        raise NoSubgidRangeError(f"User {host_user!r} has no /etc/subgid entry; rootless docker may not be configured")
    remaining = N
    for first, count in ranges:
        if remaining <= count:
            return first + remaining - 1
        remaining -= count
    raise SubgidOutOfRangeError(f"In-container gid {N} exceeds the subgid range allocated to {host_user!r}")


def in_container_gid_for_host_gid(host_gid: int, host_user: str) -> int:
    """Inverse map: host gid → in-container gid for use in ``--group-add``.

    Raises:
        NoSubgidRangeError: ``host_user`` has no /etc/subgid entry.
        SubgidOutOfRangeError: ``host_gid`` is not in any range allocated to the user.
    """
    ranges = parse_subgid_for_user(host_user)
    if not ranges:
        raise NoSubgidRangeError(f"User {host_user!r} has no /etc/subgid entry; cannot map host gid {host_gid}")
    accumulated = 0
    for first, count in ranges:
        if first <= host_gid < first + count:
            return accumulated + (host_gid - first) + 1
        accumulated += count
    raise SubgidOutOfRangeError(f"Host gid {host_gid} is not within any /etc/subgid range allocated to {host_user!r}")


def in_container_uid_for_host_uid(host_uid: int, host_user: str) -> int:
    """Inverse map: host uid → in-container uid.

    Structural inverse of :func:`host_id_for_in_container`, parallel to
    :func:`in_container_gid_for_host_gid`. Walks ``/etc/subuid`` ranges in
    file order; for the matching range returns
    ``accumulated_offset + (host_uid - first_allocated) + 1``, where
    ``accumulated_offset`` sums prior ranges' ``count``.

    Asymmetry note: :func:`host_id_for_in_container` returns ``host_user``'s
    primary uid for ``N == 0``. The inverse intentionally does NOT special-case
    ``host_uid == pwd.getpwnam(host_user).pw_uid``: the daemon user's primary
    uid lies outside the subuid range and raises :class:`SubuidOutOfRangeError`.
    Helper callers chown to subuid-range values, never to the daemon's primary
    uid; surfacing the mismatch is preferable to silently returning ``0``
    (which would chown to in-container root).

    Raises:
        NoSubuidRangeError: ``host_user`` has no /etc/subuid entry.
        SubuidOutOfRangeError: ``host_uid`` is not in any allocated range.
    """
    ranges = parse_subuid_for_user(host_user)
    if not ranges:
        raise NoSubuidRangeError(f"User {host_user!r} has no /etc/subuid entry; cannot map host uid {host_uid}")
    accumulated = 0
    for first, count in ranges:
        if first <= host_uid < first + count:
            return accumulated + (host_uid - first) + 1
        accumulated += count
    raise SubuidOutOfRangeError(f"Host uid {host_uid} is not within any /etc/subuid range allocated to {host_user!r}")


def workspace_bridge_gid(host: HostSettings) -> int:
    """Resolve ``host.workspace_bridge_group`` to its host gid.

    Validates that the gid is within the daemon user's subgid range via
    :func:`in_container_gid_for_host_gid` so callers can rely on the gid
    being usable as a ``--group-add`` argument.

    Raises:
        WorkspaceBridgeGroupMissingError: The configured group does not exist.
        SubgidOutOfRangeError: The group's gid is not in the daemon's subgid range.
        NoSubgidRangeError: The daemon user has no /etc/subgid entry.
    """
    try:
        gid = grp.getgrnam(host.workspace_bridge_group).gr_gid
    except KeyError as exc:
        raise WorkspaceBridgeGroupMissingError(
            f"group {host.workspace_bridge_group!r} does not exist on this host; "
            f"run `sandbox doctor` for setup commands"
        ) from exc
    in_container_gid_for_host_gid(gid, resolve_daemon_owner_settings(host))
    return gid


def autodetect_workspace_bridge_gid_recommendation(host_user: str, in_container_min: int = 1000) -> int:
    """Pick the lowest available host gid suitable for the workspace bridge group.

    Iterates the user's /etc/subgid ranges; selects the first host gid whose
    in-container mapping is ``>= in_container_min`` (default 1000, biases away
    from system groups) and whose host gid is not already used by an existing
    group entry (via :func:`grp.getgrall`).

    Pure function — does not mutate the host. Used by ``sandbox doctor`` to
    suggest a ``groupadd -g <gid>`` command.

    Raises:
        NoSubgidRangeError: Host user has no /etc/subgid entry.
        NoFreeGidInSubgidRangeError: All in-range gids are taken.
    """
    ranges = parse_subgid_for_user(host_user)
    if not ranges:
        raise NoSubgidRangeError(
            f"operator {host_user!r} has no /etc/subgid range for the workspace "
            f"bridge group; run 'sudo sandbox setup' to allocate one."
        )
    used_gids = {g.gr_gid for g in grp.getgrall()}
    accumulated = 0
    for first, count in ranges:
        for offset in range(count):
            in_container = accumulated + offset + 1
            host_gid = first + offset
            if in_container < in_container_min:
                continue
            if host_gid in used_gids:
                continue
            return host_gid
        accumulated += count
    raise NoFreeGidInSubgidRangeError(
        f"No free gid in {host_user!r}'s subgid range satisfies in_container_min={in_container_min}"
    )
