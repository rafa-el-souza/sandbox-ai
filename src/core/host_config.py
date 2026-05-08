"""Project-wide configuration: sandbox-ai.toml schema, loader, and machinectl command builder.

Defines the project-root configuration that holds host-level settings
(docker unprivileged user, machinectl authentication mode). Consumed by
CLI commands and the doctor module to determine privilege escalation
strategy for machinectl invocations.

Also exposes subuid/subgid resolvers and the workspace bridge group helpers
used by the helper-container ACL/ownership recipes.
"""

import grp
import os
import pwd
import tomllib
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from core.exceptions import SandboxExecutionError


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


class MachinectlAuth(StrEnum):
    """Machinectl privilege escalation mode."""

    SUDO = "sudo"
    POLKIT = "polkit"


class HostSettings(BaseModel):
    """[host] section of sandbox-ai.toml."""

    docker_unprivileged_user: str
    machinectl_authentication: MachinectlAuth = MachinectlAuth.SUDO
    workspace_bridge_group: str = "sb-ws"


class HostConfig(BaseModel):
    """Top-level Pydantic model for sandbox-ai.toml."""

    host: HostSettings

    @classmethod
    def from_toml(cls) -> HostConfig:
        """Parse the canonical per-user ``sandbox-ai.toml``.

        Resolves ``<sandbox_ai_home()>/config/sandbox-ai.toml``.

        Raises:
            FileNotFoundError: If the canonical file does not exist.
            tomllib.TOMLDecodeError: If the file contains invalid TOML.
            pydantic.ValidationError: If the content fails schema validation.
        """
        path = sandbox_ai_home() / "config" / "sandbox-ai.toml"
        try:
            with open(path, "rb") as f:
                raw = tomllib.load(f)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"No sandbox-ai.toml found at {path}. Run sandbox init to create one.") from exc
        return cls.model_validate(raw)


def machinectl_cmd(user: str, auth: MachinectlAuth) -> list[str]:
    """Build the machinectl shell prefix for the given user and auth mode.

    Returns:
        ``["sudo", "machinectl", "shell", "<user>@.host"]`` when auth is SUDO,
        ``["machinectl", "shell", "<user>@.host"]`` when auth is POLKIT.
    """
    prefix = ["sudo"] if auth == MachinectlAuth.SUDO else []
    return [*prefix, "machinectl", "shell", f"{user}@.host"]


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
            f"run `sandbox doctor` for setup commands or override [host].workspace_bridge_group"
        ) from exc
    in_container_gid_for_host_gid(gid, host.docker_unprivileged_user)
    return gid


def host_user_primary_gid(host_user: str) -> int:
    """Resolve ``host_user``'s primary gid via the host passwd database.

    Used by the credential phase to chgrp secrets to the daemon's primary
    group at write time so the gofer (running as ``host_user``) can read
    them via group permission, eliminating the need for a recursive
    ``rwX`` ACL widening on ``secrets/``.

    Raises:
        KeyError: ``host_user`` does not exist on the host.
    """
    return pwd.getpwnam(host_user).pw_gid


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
        raise NoSubgidRangeError(f"User {host_user!r} has no /etc/subgid entry; cannot recommend a bridge gid")
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
