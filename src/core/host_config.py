"""Project-wide configuration: sandbox-ai.toml schema, loader, and machinectl command builder.

Defines the project-root configuration that holds host-level settings
(docker unprivileged user, machinectl authentication mode). Consumed by
CLI commands and the doctor module to determine privilege escalation
strategy for machinectl invocations.

Also exposes subuid/subgid resolvers and the workspace bridge group helpers
used by the helper-container ACL/ownership recipes.
"""

import getpass
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


# The PROVISIONING default: the mode `sandbox setup` provisions when the operator
# passes no `--docker-execution-mode` flag and the marker has no entry — the single,
# user-facing default (referenced, never re-literalised; see finding F-051). It is
# DISTINCT from the in-memory fallback on `HostSettings.docker_execution_mode` and
# `minimal_host_config` below (intentionally kept `SEPARATE_USER`): the runtime never
# acts on that fallback because `_resolve_full_host_config` always overlays the
# marker-resolved mode, so it is moot, and keeping it avoids a no-benefit re-pin sweep.
DEFAULT_PROVISIONING_MODE = DockerExecutionMode.OPERATOR_ROOTLESS


class HostSettings(BaseModel):
    """[host] section of sandbox-ai.toml.

    ``machinectl_authentication`` is **inert** when
    ``docker_execution_mode == DockerExecutionMode.OPERATOR_ROOTLESS``: there
    is no crossing to authorize, so the value is accepted and ignored rather
    than rejected. The two fields are intentionally not cross-validated —
    Pydantic accepting both together IS the inert behavior.
    """

    docker_unprivileged_user: str
    machinectl_authentication: MachinectlAuth = MachinectlAuth.SUDO
    # In-memory carrier ONLY (D11): the execution mode is NOT a user-editable toml
    # field — it is setup-determined and resolved at runtime from the per-operator
    # marker (``core.setup_state.resolve_execution_mode``). ``from_toml`` rejects a
    # toml that sets it; the field is populated programmatically (by setup, by
    # ``minimal_host_config``, and by the runtime overlay in ``cli.main``).
    docker_execution_mode: DockerExecutionMode = DockerExecutionMode.SEPARATE_USER
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
            ValueError: If the ``[host]`` table sets ``docker_execution_mode`` —
                that field is setup-determined (the per-operator marker), not a
                user-editable toml value (D11).
        """
        path = sandbox_ai_home() / "config" / "sandbox-ai.toml"
        try:
            with open(path, "rb") as f:
                raw = tomllib.load(f)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"No sandbox-ai.toml found at {path}. Run sandbox init to create one.") from exc
        host_table = raw.get("host")
        if isinstance(host_table, dict) and "docker_execution_mode" in host_table:
            raise ValueError(
                f"docker_execution_mode is no longer a sandbox-ai.toml field; it is "
                f"setup-determined (the per-operator setup-state marker). Remove it "
                f"from {path} and rerun `sudo sandbox setup` to change the mode."
            )
        return cls.model_validate(raw)


def minimal_host_config(
    user: str, auth: MachinectlAuth, mode: DockerExecutionMode = DockerExecutionMode.SEPARATE_USER
) -> HostConfig:
    """Build a HostConfig carrying only the fields the dispatch boundary reads.

    The fields are ``docker_unprivileged_user``, ``machinectl_authentication``,
    and ``docker_execution_mode`` (defaulting to ``SEPARATE_USER``).
    """
    return HostConfig(
        host=HostSettings(
            docker_unprivileged_user=user,
            machinectl_authentication=auth,
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
    return host.docker_unprivileged_user


def resolve_daemon_owner(host_config: HostConfig) -> str:
    """Resolve the rootless-daemon owner (design D7) — the ``HostConfig`` alias.

    The command-level resolver; delegates to :func:`resolve_daemon_owner_settings`.
    The runtime parallel of setup's ``daemon_owner_user(ctx)``.
    """
    return resolve_daemon_owner_settings(host_config.host)


def machinectl_cmd(user: str, auth: MachinectlAuth) -> list[str]:
    """Build the machinectl shell prefix for the given user and auth mode.

    Returns:
        ``["sudo", "machinectl", "shell", "<user>@.host"]`` when auth is SUDO,
        ``["machinectl", "shell", "<user>@.host"]`` when auth is POLKIT.

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
    prefix = ["sudo"] if auth == MachinectlAuth.SUDO else []
    return [*prefix, "machinectl", "shell", f"{user}@.host"]


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
            f"run `sandbox doctor` for setup commands or override [host].workspace_bridge_group"
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
