"""L2 — host-side prerequisites: machined, sandbox user, subuid/subgid, sb-ws.

Third phase of ``sandbox setup`` (spec "Phase Execution Order" step 3). L2
performs the shared, idempotent, host-level prerequisite mutations:

- enable + start ``systemd-machined``;
- ``useradd --system --shell /bin/bash --create-home <sandbox-user>`` when the
  user is absent;
- ``/etc/subuid`` + ``/etc/subgid`` — *append-only when safe*: an existing
  entry with an adequate range is left untouched (idempotent skip); an absent
  entry is appended; an existing-but-inadequate range is refused (the spec's
  "Refusing to shrink existing range" — a ``CONFLICT``, never overwritten);
- ``groupadd -g <auto-gid-in-subgid-range> sb-ws`` when the bridge group is
  absent (gid autodetected in the sandbox user's subgid range);
- ``usermod -aG sb-ws <operator>``.

**L2 does NOT install runsc** — that is its own phase L6a (R1).

**L2 is separate-user only** (``applies_in`` excludes operator-rootless, D5a/O3):
its dedicated-user + machined work is inapplicable when the daemon runs as the
operator, and its genuinely-privileged subuid/subgid + ``sb-ws`` mutations are
owned by the ``host_batch`` classifier + ``_bootstrap-host`` escalation in
operator-rootless. The runner reports it ``skipped (operator-rootless)``.

Content-aware probe (design D10): the expected state is computed from the
current source (the configured sandbox user / bridge-group name, the subuid
minimum-range requirement). The probe compares it to the observed host:
everything present+adequate → ``ALREADY_CORRECT``; a missing user / group /
subid entry / group-membership → ``MISSING``; an existing subid range smaller
than the minimum → ``CONFLICT`` (refuse, do not shrink).
"""

from __future__ import annotations

import grp
import pwd
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.host_config import (
    DockerExecutionMode,
    autodetect_workspace_bridge_gid_recommendation,
    parse_subgid_for_user,
    parse_subuid_for_user,
)
from core.setup.phase_runner import Identity, Phase, PhaseResult

if TYPE_CHECKING:
    from core.setup.phase_runner import SetupContext

# The standard rootless subuid/subgid range size shadow-mapped by Docker
# (``useradd`` default = 65536). An existing entry must be at least this large
# or L2 refuses to shrink it (spec "/etc/subuid refuses to shrink").
_MIN_SUBID_RANGE = 65536


def _user_exists(name: str) -> bool:
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


def _group_exists(name: str) -> bool:
    try:
        grp.getgrnam(name)
        return True
    except KeyError:
        return False


def _gid_in_subgid_range(gid: int, ranges: list[tuple[int, int]]) -> bool:
    """``True`` iff host ``gid`` falls inside one of the subgid ranges.

    The workspace bridge group MUST sit inside the sandbox user's subgid
    range (CLAUDE.md: ``groupadd -g <gid-in-subgid-range> sb-ws``). A bridge
    group present with a gid *outside* that range is a content drift (an
    operator hand-created the group at the wrong gid, or a wheel upgrade
    changed the range expectation), distinct from "group absent".
    """
    return any(first <= gid < first + count for first, count in ranges)


def _operator_in_group(operator: str, group: str) -> bool:
    """``True`` iff ``operator`` is a member of ``group`` per ``/etc/group``."""
    try:
        members = set(grp.getgrnam(group).gr_mem)
    except KeyError:
        return False
    if operator in members:
        return True
    # The operator's *primary* group could also be the bridge group.
    try:
        return grp.getgrnam(group).gr_gid == pwd.getpwnam(operator).pw_gid
    except KeyError:
        return False


# The privilege-granting groups whose membership confers (or gates) sudo-to-root
# across the supported distro families: ``sudo`` (Debian/Ubuntu), ``wheel``
# (RHEL/Fedora/Arch), and the legacy ``admin`` group. This is the single source
# for "what counts as an admin group" — the two C-005 doctor safety nets (the
# no-sudo daemon-user invariant and the sudoer daemon-owner WARN) both resolve
# membership through :func:`_user_admin_groups` so they cannot disagree.
_ADMIN_GROUPS: tuple[str, ...] = ("sudo", "wheel", "admin")


def _user_admin_groups(user: str) -> list[str]:
    """Return the :data:`_ADMIN_GROUPS` the ``user`` is a member of.

    Membership is by supplementary-group listing **or** primary group (same
    member-or-primary logic as :func:`_operator_in_group`). A user absent from
    ``/etc/passwd`` (no primary gid to resolve) is treated as in no admin group.
    Empty list ⇒ the user cannot sudo-to-root via group membership.
    """
    try:
        primary_gid: int | None = pwd.getpwnam(user).pw_gid
    except KeyError:
        primary_gid = None
    found: list[str] = []
    for group in _ADMIN_GROUPS:
        try:
            entry = grp.getgrnam(group)
        except KeyError:
            continue
        if user in entry.gr_mem or entry.gr_gid == primary_gid:
            found.append(group)
    return found


@dataclass(frozen=True)
class SudoersGrant:
    """Whether the **sudoers policy** grants a user sudo (drop-ins + NOPASSWD).

    The companion to :func:`_user_admin_groups`: group membership is only one of
    two ways a user reaches root. The sudoers *policy* — a ``/etc/sudoers.d/``
    drop-in (cloud-init's ``90-cloud-init-users``, the common cloud-VM / dev-box
    pattern) or an inline ``NOPASSWD`` rule — confers sudo independently of any
    ``sudo``/``wheel``/``admin`` group, so a group-only check misses it.

    Fields:

    - ``granted`` — the sudoers policy grants this user sudo (NOPASSWD or
      password-gated alike). A password-gated grant is still a grant: the user
      *can* reach root.
    - ``nopasswd`` — at least one grant is ``NOPASSWD`` (instant, unprompted
      escalation — the sharpest blast-radius signal).
    - ``determinable`` — ``False`` when the query could not be performed (e.g.
      ``-U <other-user>`` needs root and we are not root). Indeterminate is NOT
      a grant: never false-WARN on it.
    """

    granted: bool
    nopasswd: bool
    determinable: bool


# Version-tolerant markers in ``sudo -l`` output (combined stdout+stderr,
# case-folded before matching so distro/version casing differences are moot).
_SUDO_NOT_ALLOWED = "not allowed to run sudo"
_SUDO_MAY_NOT_LIST = "may not list"
_SUDO_RUNNABLE_MARKERS = (
    "may run the following commands",
    "may run the following command",
)


def _user_sudoers_grant(user: str, *, self_query: bool) -> SudoersGrant:
    """Detect whether the **sudoers policy** grants ``user`` sudo.

    Complements :func:`_user_admin_groups` (group membership) with the
    policy-grant path: ``/etc/sudoers.d/`` drop-ins and inline ``NOPASSWD``
    rules. Runs ``sudo -n -l`` non-interactively and parses the captured output
    version-tolerantly.

    - ``self_query=True`` (operator-rootless: the daemon owner IS the current
      process user) → ``sudo -n -l`` queries the *current* user's own
      privileges, needs no root. ``-U`` is rejected for non-root even for self,
      so it is deliberately NOT used here.
    - ``self_query=False`` (separate-user: the owner is a *different*, dedicated
      account) → ``sudo -n -l -U <user>`` lists another user's privileges, which
      requires root. When not root, sudo refuses ("may not list" / errors) →
      ``determinable=False`` (we did not learn anything; do NOT infer a grant).

    Parsing (case-folded combined output):

    - contains "not allowed to run sudo" → ``granted=False`` (clean no-priv).
    - contains a "may run the following command(s)" marker → ``granted=True``.
    - contains "NOPASSWD" → ``nopasswd=True`` (only meaningful when granted).
    - a non-zero exit whose output asks for a password (no "not allowed", no
      runnable listing — the ``-n`` non-interactive run that would otherwise
      prompt) still means the user CAN sudo, password-gated →
      ``granted=True, nopasswd=False``.
    - "may not list" (the non-root ``-U`` refusal) → ``determinable=False``.
    - ``OSError`` (sudo absent) or genuinely unparseable output →
      ``determinable=False`` (never a false WARN).
    """
    argv = ["sudo", "-n", "-l"]
    if not self_query:
        argv += ["-U", user]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError:
        return SudoersGrant(granted=False, nopasswd=False, determinable=False)

    combined = f"{proc.stdout}\n{proc.stderr}".casefold()
    nopasswd = "nopasswd" in combined

    if _SUDO_NOT_ALLOWED in combined:
        return SudoersGrant(granted=False, nopasswd=False, determinable=True)
    if any(marker in combined for marker in _SUDO_RUNNABLE_MARKERS):
        return SudoersGrant(granted=True, nopasswd=nopasswd, determinable=True)
    if _SUDO_MAY_NOT_LIST in combined:
        # Non-root ``-U <other-user>`` refusal — we could not query. Never infer.
        return SudoersGrant(granted=False, nopasswd=False, determinable=False)
    if proc.returncode != 0:
        # ``-n`` refused to prompt for a password the user WOULD be asked for:
        # the user can sudo, but it is password-gated (no NOPASSWD listing seen).
        return SudoersGrant(granted=True, nopasswd=False, determinable=True)
    # Clean exit with no recognizable marker — unparseable; do not guess.
    return SudoersGrant(granted=False, nopasswd=False, determinable=False)


def _adequate_range(ranges: list[tuple[int, int]]) -> bool:
    """``True`` iff the user's total allocated subid count meets the minimum."""
    return sum(count for _, count in ranges) >= _MIN_SUBID_RANGE


def _subid_status(user: str) -> tuple[str, str]:
    """Classify the subuid+subgid state for ``user``.

    Returns ``(status, detail)`` where status ∈
    ``{"adequate", "absent", "inadequate"}``. ``inadequate`` is the
    refuse-to-shrink case (spec).
    """
    uid_ranges = parse_subuid_for_user(user)
    gid_ranges = parse_subgid_for_user(user)
    if not uid_ranges and not gid_ranges:
        return "absent", "no /etc/subuid or /etc/subgid entry"
    for label, ranges in (("subuid", uid_ranges), ("subgid", gid_ranges)):
        if ranges and not _adequate_range(ranges):
            total = sum(c for _, c in ranges)
            return (
                "inadequate",
                f"existing /etc/{label} entry for {user} has range {total}; "
                f"minimum required is {_MIN_SUBID_RANGE}. Refusing to shrink "
                f"existing range. Manually update /etc/{label} and re-run setup.",
            )
    if not uid_ranges or not gid_ranges:
        return "absent", "one of /etc/subuid or /etc/subgid entry is absent"
    return "adequate", "subuid/subgid ranges adequate"


def _machined_active() -> bool:
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", "systemd-machined"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.stdout.strip() == "active"


def _probe(ctx: SetupContext) -> tuple[PhaseResult, str]:
    """Content-aware L2 probe (see module docstring)."""
    host_config = ctx.host_config
    sandbox_user = host_config.host.docker_unprivileged_user
    bridge_group = host_config.host.workspace_bridge_group
    operator = ctx.operator

    if not _user_exists(sandbox_user):
        return PhaseResult.MISSING, f"sandbox user {sandbox_user!r} absent"

    status, detail = _subid_status(sandbox_user)
    if status == "inadequate":
        return PhaseResult.CONFLICT, detail
    if status == "absent":
        return PhaseResult.MISSING, f"subid entries for {sandbox_user!r}: {detail}"

    if not _machined_active():
        return PhaseResult.MISSING, "systemd-machined not active"
    if not _group_exists(bridge_group):
        return PhaseResult.MISSING, f"bridge group {bridge_group!r} absent"
    bridge_gid = grp.getgrnam(bridge_group).gr_gid
    if not _gid_in_subgid_range(bridge_gid, parse_subgid_for_user(sandbox_user)):
        return (
            PhaseResult.DRIFT,
            f"bridge group {bridge_group!r} exists at gid {bridge_gid} which "
            f"is outside {sandbox_user!r}'s /etc/subgid range; the group must "
            f"sit inside the subgid range",
        )
    if not _operator_in_group(operator, bridge_group):
        return (
            PhaseResult.MISSING,
            f"operator {operator!r} not in group {bridge_group!r}",
        )
    return (
        PhaseResult.ALREADY_CORRECT,
        f"machined active; user {sandbox_user!r}, group {bridge_group!r}, "
        f"operator {operator!r} membership, subid ranges all present",
    )


def _run(argv: list[str]) -> None:
    subprocess.run(argv, capture_output=True, text=True, check=True)


def _act(ctx: SetupContext) -> str:
    """Converge machined, the sandbox user, subid entries, sb-ws, membership."""
    host_config = ctx.host_config
    sandbox_user = host_config.host.docker_unprivileged_user
    bridge_group = host_config.host.workspace_bridge_group
    operator = ctx.operator
    actions: list[str] = []

    if not _machined_active():
        _run(["systemctl", "enable", "--now", "systemd-machined"])
        actions.append("enabled systemd-machined")

    if not _user_exists(sandbox_user):
        _run(
            [
                "useradd",
                "--system",
                "--shell",
                "/bin/bash",
                "--create-home",
                sandbox_user,
            ]
        )
        actions.append(f"created user {sandbox_user}")

    status, detail = _subid_status(sandbox_user)
    if status == "inadequate":
        # Defensive: the runner never calls act on CONFLICT, but never shrink.
        raise RuntimeError(detail)
    if status == "absent":
        if not parse_subuid_for_user(sandbox_user):
            _run(["usermod", "--add-subuids", f"100000-{100000 + _MIN_SUBID_RANGE - 1}", sandbox_user])
            actions.append("appended /etc/subuid entry")
        if not parse_subgid_for_user(sandbox_user):
            _run(["usermod", "--add-subgids", f"100000-{100000 + _MIN_SUBID_RANGE - 1}", sandbox_user])
            actions.append("appended /etc/subgid entry")

    if not _group_exists(bridge_group):
        gid = autodetect_workspace_bridge_gid_recommendation(sandbox_user)
        _run(["groupadd", "-g", str(gid), bridge_group])
        actions.append(f"created group {bridge_group} (gid {gid})")
    else:
        bridge_gid = grp.getgrnam(bridge_group).gr_gid
        if not _gid_in_subgid_range(
            bridge_gid, parse_subgid_for_user(sandbox_user)
        ):
            raise RuntimeError(
                f"group {bridge_group!r} exists at gid {bridge_gid} outside "
                f"{sandbox_user!r}'s /etc/subgid range; setup will not move an "
                f"operator-created group. Recreate it with a gid inside the "
                f"subgid range and re-run setup."
            )

    if not _operator_in_group(operator, bridge_group):
        _run(["usermod", "-aG", bridge_group, operator])
        actions.append(f"added {operator} to {bridge_group}")

    return "; ".join(actions) if actions else "L2 already converged"


def _reverify(ctx: SetupContext) -> bool:
    """L2 converged iff machined active + user/group/membership/subid present."""
    host_config = ctx.host_config
    sandbox_user = host_config.host.docker_unprivileged_user
    bridge_group = host_config.host.workspace_bridge_group
    operator = ctx.operator
    if not _machined_active():
        return False
    if not _user_exists(sandbox_user):
        return False
    status, _ = _subid_status(sandbox_user)
    if status != "adequate":
        return False
    if not _group_exists(bridge_group):
        return False
    bridge_gid = grp.getgrnam(bridge_group).gr_gid
    if not _gid_in_subgid_range(bridge_gid, parse_subgid_for_user(sandbox_user)):
        return False
    return _operator_in_group(operator, bridge_group)


PHASE = Phase(
    id="l2",
    name="host prerequisites (machined, user, subid, sb-ws)",
    identity=Identity.ROOT,
    probe=_probe,
    act=_act,
    reverify=_reverify,
    depends_on=("l1",),
    # separate-user only. Every L2 mutation is either inapplicable or host-root-
    # batch-owned in operator-rootless: the dedicated useradd is skipped (the
    # daemon runs as the operator's own pre-existing user), systemd-machined is
    # skipped (no machinectl consumer), and the genuinely-privileged subuid/subgid
    # + sb-ws groupadd are applied by the classifier + ``_bootstrap-host`` host-root
    # batch (design D5a / O3) rather than by this unprivileged apply pass. So the
    # phase is gated OUT of operator-rootless (reported ``skipped`` in both passes)
    # — mirroring the M2 crossing-only phases (L3/L3a/L6.5/L8).
    applies_in=frozenset({DockerExecutionMode.SEPARATE_USER}),
)

__all__ = ["PHASE"]
