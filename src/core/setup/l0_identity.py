# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""L0 — identity, distro classification, binary check, machinectl-path resolution.

The first phase of ``sandbox setup`` (design D5; spec "Phase Execution Order"
step 1). L0 is pure host *verification* — it owns no on-disk file. It resolves
the operator user (precedence rule), classifies the detected distro into the
three support tiers (Validated / Untested / Unrecognized), verifies every
required host binary is present, captures the sudo-version floor, and resolves
``machinectl`` on the sudoers ``secure_path`` basis with the uniqueness
assertion (B-3, F-005). The machinectl assertion underpins the privilege
boundary's sudoers ``Cmnd_Spec`` path, so it is gated to the crossing modes —
**skipped in operator-rootless** (no machinectl crossing exists there; D2) while
operator/distro/binary resolution stays mode-agnostic.

Content-aware probe (design D10): L0's convergence target is "operator
resolvable AND distro supported AND every required binary present AND exactly
one canonical ``machinectl`` on secure_path". The probe computes that expected
state from the live host and compares it to the observed host:

- all satisfied → ``ALREADY_CORRECT``;
- a required binary that should be present is absent → ``DRIFT`` (the operator
  must install it — L0's ``act`` emits the copy-pasteable per-distro install
  command and raises, since L0 cannot install packages itself);
- an unrecognized distro, an unresolvable operator, a ``SUDO_USER``/``SUDO_UID``
  inconsistency, or a zero / non-unique / non-canonical ``machinectl`` →
  ``CONFLICT`` (an unconvergeable refusal — the runner never calls ``act``).

``resolve_machinectl_path`` is exported as a module-level *pure* function:
Group 7's ``l3_sudoers`` re-imports and re-calls it at rule-render time
so the rendered ``Cmnd_Spec`` path equals what sudo resolves the orchestrator's
relative ``machinectl`` to (B-3); re-resolution also detects an L0↔live
``secure_path`` drift (the F-005 footgun ``setup_invariants`` re-checks).
"""

from __future__ import annotations

import os
import pwd
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from core.doctor import detect_distro, get_install_cmd
from core.host_config import is_operator_rootless, pipe_cmd
from core.setup.phase_runner import Identity, Phase, PhaseResult

if TYPE_CHECKING:
    from core.host_config import HostConfig
    from core.setup.phase_runner import SetupContext

# ── Distro support tiers (spec "Distro Support Tiers") ───────────────────────
# Enumerable in code (single point of edit for contributors adding support).
# Keyed on the raw ``/etc/os-release`` ``ID`` value, NOT the family-normalized
# value ``core.doctor.detect_distro`` collapses to — the spec names the raw
# distro in the operator-facing texts (e.g. "Ubuntu", "Fedora").
_VALIDATED_DISTROS: frozenset[str] = frozenset({"debian", "ubuntu"})
_UNTESTED_DISTROS: frozenset[str] = frozenset(
    {"fedora", "rhel", "centos", "arch", "manjaro"}
)

# ── Required host binaries (spec / task 5.1 binary check) ────────────────────
# ``tlog-rec`` ships in the ``tlog`` package; the others are their own binary
# name = package name on the common distros, except ``machinectl`` (the
# ``systemd-container`` package) which is checked separately via the
# secure_path resolution below, so it is not in this list.
_REQUIRED_BINARIES: tuple[str, ...] = (
    "sudo",
    "setfacl",
    "getfacl",
    "rsync",
    "loginctl",
    "useradd",
    "usermod",
    "groupadd",
    "visudo",
    "chattr",
    "sysctl",
    "tlog-rec",
)

# Binary → package name override where they differ. Anything not listed maps
# to its own name.
_BINARY_PACKAGE: dict[str, str] = {
    "setfacl": "acl",
    "getfacl": "acl",
    "tlog-rec": "tlog",
}

# ``tlog`` is in the Arch AUR (not extra/community); ``get_install_cmd`` would
# wrongly suggest ``sudo pacman -S tlog``. Spec / task 5.1 mandate the AUR
# helper hint on Arch-family.
_TLOG_ARCH_INSTALL = "paru -S tlog"

# ``tlog`` is packaged on Ubuntu and Debian <=12 but NOT on Debian 13+ (trixie),
# where it must be built from source — ``get_install_cmd`` would wrongly suggest
# a bare ``sudo apt install tlog`` that fails on trixie. ``detect_distro``
# normalizes Ubuntu and Debian to the same ``debian`` family, so the hint covers
# both paths rather than version-gating (round-5 Debian-trixie finding).
_TLOG_DEBIAN_INSTALL = (
    "sudo apt install tlog (Ubuntu / Debian <=12); on Debian 13+ (trixie) tlog "
    "is not packaged — build from source: https://github.com/Scribery/tlog"
)

# The sudoers compiled-default ``secure_path`` (spec "Phase Execution Order"
# L0). Used as the fallback when ``Defaults secure_path`` is not parseable.
_DEFAULT_SECURE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Canonical systemd-provided ``machinectl`` locations (spec L0). Exactly-one in
# one of these → accepted; sole-one elsewhere or >1 anywhere → refused.
_CANONICAL_MACHINECTL_DIRS: frozenset[str] = frozenset({"/usr/bin", "/usr/sbin"})

# The V9c-validated sudo floor (task 5.1 / spec "Sudo-version compatibility").
SUDO_FLOOR: tuple[int, int, int, int] = (1, 9, 5, 2)

_SECURE_PATH_RE = re.compile(r"secure_path\s*=\s*(?:\")?([^\"\n]+)(?:\")?")
_SUDO_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:p(\d+))?")


class OperatorResolutionError(ValueError):
    """The operator user could not be resolved (spec "Operator Resolution")."""


class MachinectlResolutionError(ValueError):
    """``machinectl`` does not resolve to a single canonical secure_path entry.

    Raised by :func:`resolve_machinectl_path` for the zero / >1 / sole-non-
    canonical cases (spec L0; B-3, F-005). The message names every found path.
    """


class SystemdRunResolutionError(ValueError):
    """The transient-unit launcher has no single canonical secure_path entry.

    The :func:`resolve_systemd_run_path` sibling of
    :class:`MachinectlResolutionError` (C-009 design D5): raised for the zero /
    ≥2-genuinely-distinct / sole-non-canonical cases so the SUDO pipe
    ``Cmnd_Spec`` is rendered against a single, canonical, absolute launcher
    path. The message names every found path.
    """


# ── Operator resolution (spec "Operator Resolution Precedence") ──────────────


def resolve_operator(operator_flag: str | None = None) -> str:
    """Resolve the operator user via the explicit precedence; refuse on none.

    Precedence: ``--operator`` flag → ``$SUDO_USER`` (consistent with
    ``$SUDO_UID``) → refuse. No TTY heuristics.

    Raises:
        OperatorResolutionError: No operator could be resolved, the flag names
            a nonexistent user, or ``SUDO_USER``/``SUDO_UID`` are inconsistent.
    """
    if operator_flag is not None:
        try:
            pwd.getpwnam(operator_flag)
        except KeyError as exc:
            raise OperatorResolutionError(
                f"--operator {operator_flag!r} does not match an existing user"
            ) from exc
        return operator_flag

    sudo_user = os.environ.get("SUDO_USER")
    sudo_uid = os.environ.get("SUDO_UID")
    if sudo_user and sudo_uid:
        try:
            entry = pwd.getpwnam(sudo_user)
        except KeyError as exc:
            raise OperatorResolutionError(
                f"$SUDO_USER={sudo_user!r} does not match an existing user"
            ) from exc
        if entry.pw_uid != int(sudo_uid):
            raise OperatorResolutionError(
                f"$SUDO_USER={sudo_user!r} is inconsistent with $SUDO_UID="
                f"{sudo_uid!r}: pwd.getpwnam({sudo_user!r}).pw_uid="
                f"{entry.pw_uid} != {int(sudo_uid)}"
            )
        return sudo_user

    raise OperatorResolutionError(
        "cannot resolve operator user. Re-invoke as: sudo sandbox setup, "
        "or pass --operator <name>."
    )


# ── Distro classification (spec "Distro Support Tiers") ──────────────────────


def _read_os_release() -> dict[str, str]:
    """Parse ``/etc/os-release`` into a flat key→value dict (empty on absence)."""
    try:
        content = Path("/etc/os-release").read_text()
    except FileNotFoundError:
        return {}
    fields: dict[str, str] = {}
    for line in content.splitlines():
        if "=" in line:
            key, _, val = line.partition("=")
            fields[key.strip()] = val.strip().strip('"')
    return fields


def _distro_identity() -> tuple[str, str]:
    """Return ``(raw_id, pretty_version)`` for operator-facing texts."""
    fields = _read_os_release()
    raw_id = fields.get("ID", "").strip().lower()
    version = fields.get("VERSION_ID", "") or fields.get("VERSION", "")
    return raw_id, version


def classify_distro() -> tuple[str, str, str]:
    """Classify the host distro into a support tier.

    Returns ``(tier, raw_id, version)`` where ``tier`` is one of
    ``"validated"`` / ``"untested"`` / ``"unrecognized"``.
    """
    raw_id, version = _distro_identity()
    if raw_id in _VALIDATED_DISTROS:
        return "validated", raw_id, version
    if raw_id in _UNTESTED_DISTROS:
        return "untested", raw_id, version
    return "unrecognized", raw_id or "unknown", version


def untested_distro_warning(raw_id: str, version: str, *, with_prompt: bool) -> str:
    """Render the canonical untested-distro warning (spec exact text)."""
    detected = f"{raw_id} {version}".strip()
    body = (
        "⚠ Untested distro\n"
        "\n"
        f"  Detected: {detected}\n"
        "\n"
        "  Debian and Ubuntu are sandbox-ai's reference distros. On other distros\n"
        "  it uses the same logic with distro-specific package commands, but has\n"
        "  not been end-to-end validated.\n"
        "\n"
        "  To preview the steps without applying any, re-run with --dry-run.\n"
        "  Manual remediation steps for each phase are documented in sandbox-ai's\n"
        "  setup guide."
    )
    if with_prompt:
        body += "\n\n  Press Enter to continue, Ctrl-C to abort."
    return body


def unsupported_distro_refusal(raw_id: str, version: str) -> str:
    """Render the canonical unrecognized-distro refusal (spec exact text)."""
    detected = f"{raw_id} {version}".strip()
    return (
        "✗ Unsupported distro\n"
        "\n"
        f"  Detected: {detected}\n"
        "\n"
        "  sandbox-ai's setup currently supports: Debian, Ubuntu, Fedora, RHEL,\n"
        "  CentOS, Arch, Manjaro.\n"
        "\n"
        "  If you'd like to use sandbox-ai on a different distro, the setup guide\n"
        "  documents the manual ceremony steps so you can perform them by hand.\n"
        "\n"
        "  Setup refuses to proceed on unrecognized distros to avoid making\n"
        "  unpredictable system changes."
    )


# ── Binary check (spec / task 5.1) ───────────────────────────────────────────


def _secure_path_dirs() -> list[str]:
    """Return the sudoers ``secure_path`` dir list (or the compiled default).

    Parsed from ``sudo -V`` first (it prints ``Value to override user's $PATH
    with: …`` / the ``secure_path`` Default), then ``/etc/sudoers``, then the
    compiled fallback. Never uses the caller's ``$PATH`` (sudo resolves against
    secure_path, not the invoking shell's PATH).
    """
    raw = ""
    try:
        proc = subprocess.run(
            ["sudo", "-V"], capture_output=True, text=True, check=False
        )
        m = _SECURE_PATH_RE.search(proc.stdout)
        if m:
            raw = m.group(1)
    except (OSError, subprocess.SubprocessError):
        raw = ""
    if not raw:
        try:
            sudoers = Path("/etc/sudoers").read_text()
            for line in sudoers.splitlines():
                stripped = line.strip()
                if stripped.startswith("Defaults") and "secure_path" in stripped:
                    m = _SECURE_PATH_RE.search(stripped)
                    if m:
                        raw = m.group(1)
                        break
        except (OSError, ValueError):
            raw = ""
    if not raw:
        raw = _DEFAULT_SECURE_PATH
    return [d for d in raw.split(":") if d]


def _binary_install_cmd(binary: str, distro_family: str | None) -> str:
    """Per-distro copy-pasteable install command for a missing binary."""
    if binary == "tlog-rec":
        if distro_family == "arch":
            return _TLOG_ARCH_INSTALL
        if distro_family == "debian":
            return _TLOG_DEBIAN_INSTALL
    package = _BINARY_PACKAGE.get(binary, binary)
    return get_install_cmd(distro_family, package)


def missing_binaries() -> list[str]:
    """Return the required binaries absent from the sudoers secure_path basis.

    Resolution is on the secure_path dir list — NOT ``shutil.which`` /
    ``$PATH`` — so the check matches what sudo will actually resolve.
    """
    dirs = _secure_path_dirs()
    missing: list[str] = []
    for binary in _REQUIRED_BINARIES:
        if not any(os.access(os.path.join(d, binary), os.X_OK) for d in dirs):
            missing.append(binary)
    return missing


def parse_sudo_version() -> tuple[int, int, int, int] | None:
    """Parse ``sudo --version`` into a comparable ``(maj, min, patch, p)`` tuple."""
    try:
        proc = subprocess.run(
            ["sudo", "--version"], capture_output=True, text=True, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    m = _SUDO_VERSION_RE.search(proc.stdout)
    if not m:
        return None
    maj, min_, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    p = int(m.group(4)) if m.group(4) else 0
    return (maj, min_, patch, p)


def sudo_floor_warning() -> str | None:
    """Return the non-blocking sub-floor sudo WARN text, or ``None`` if OK."""
    ver = parse_sudo_version()
    if ver is None or ver >= SUDO_FLOOR:
        return None
    rendered = f"{ver[0]}.{ver[1]}.{ver[2]}" + (f"p{ver[3]}" if ver[3] else "")
    return (
        f"sudo {rendered} predates the validated floor 1.9.5p2; the V9 rule "
        f"shape is unverified on this version — only EOL distros [RHEL 7, "
        f"Debian 10] are below"
    )


# ── machinectl-path resolution + uniqueness (B-3, F-005; orchestrator dec. 1) ─


def _file_identity(path: str) -> tuple[int, int] | None:
    """Return ``(st_dev, st_ino)`` for ``path``, or ``None`` if unstattable.

    The real-file identity key used to dedupe usrmerge symlink aliases:
    ``/usr/bin``↔``/usr/sbin``↔``/sbin``↔``/bin`` on a usrmerged host all
    point at the same inode, so multiple secure_path entries that resolve to
    the *same file* are ONE binary — not the F-005 attacker-shadow case (a
    shadow at a non-canonical dir is a *different* inode).
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _resolve_secure_path_binary(
    binary: str,
    error: type[ValueError],
    *,
    absent_hint: str,
    distinct_hint: str,
    non_canonical_hint: str,
) -> str:
    """Resolve a single canonical ``binary`` on the sudoers secure_path basis.

    The shared F-005 inode-dedup resolver behind both
    :func:`resolve_machinectl_path` and :func:`resolve_systemd_run_path` (the
    two differ only by binary name, error class, and diagnostic hints — the
    secure_path enumeration + ``(st_dev, st_ino)`` dedup + canonical-preference
    logic is ONE definition, never re-derived).

    Pure / re-derivable (no caching, no module state). Enumerates every
    ``secure_path`` directory containing an executable ``binary``, then
    **dedupes by real file identity** (``os.stat`` ``(st_dev, st_ino)``) so a
    usrmerged host — where ``/usr/bin``/``/usr/sbin``/``/sbin``/``/bin`` are
    symlink aliases of one directory — counts the aliased paths as the ONE
    underlying binary rather than a false ">1" refusal:

    - the secure_path entries resolve (by inode) to exactly one file, and at
      least one of its paths is in a canonical location (``/usr/bin`` or
      ``/usr/sbin``) → return that canonical path (preferring ``/usr/bin``);
    - zero → ``error`` (the orchestrator's relative invocation could never be
      granted/resolved by any rule);
    - ≥2 *genuinely distinct* files (different ``(st_dev, st_ino)``) → the
      F-005 attacker-shadow refusal (``error`` listing every found path);
    - a sole binary whose only path is non-canonical → ``error``.

    The inode dedupe PRESERVES the F-005/V9e anti-shadow property: a shadow
    binary at e.g. a non-canonical dir is a *different* inode, so it still
    triggers the genuinely-distinct refusal; only the usrmerge symlink dup
    (same inode) is collapsed.
    """
    found: list[str] = []
    for d in _secure_path_dirs():
        candidate = os.path.join(d, binary)
        if os.access(candidate, os.X_OK) and candidate not in found:
            found.append(candidate)

    if not found:
        raise error(absent_hint.format(secure_path=":".join(_secure_path_dirs())))

    # Dedupe by real file identity: group every found path by its
    # ``(st_dev, st_ino)`` so usrmerge symlink aliases of one binary collapse
    # to a single distinct file. A path that fails ``os.stat`` keys on its own
    # string so it is never silently merged into a genuine binary's group.
    distinct: dict[tuple[int, int] | str, list[str]] = {}
    for path in found:
        key: tuple[int, int] | str = _file_identity(path) or path
        distinct.setdefault(key, []).append(path)

    if len(distinct) > 1:
        raise error(distinct_hint.format(count=len(distinct), found=found))

    aliases = next(iter(distinct.values()))
    canonical = [p for p in aliases if os.path.dirname(p) in _CANONICAL_MACHINECTL_DIRS]
    if canonical:
        # Prefer ``/usr/bin/<binary>`` (the form the L3 renderer + operator
        # texts name); fall back to any other canonical alias deterministically.
        for preferred in sorted(canonical):
            if os.path.dirname(preferred) == "/usr/bin":
                return preferred
        return sorted(canonical)[0]

    raise error(non_canonical_hint.format(found=found))


def resolve_machinectl_path(host_config: HostConfig) -> str:
    """Resolve the single canonical ``machinectl`` on the secure_path basis.

    Thin wrapper over :func:`_resolve_secure_path_binary` (the shared F-005
    inode-dedup resolver). Returns the canonical ``/usr/bin/machinectl`` for the
    zero / >1 / sole-non-canonical refusals see
    :class:`MachinectlResolutionError`.

    ``host_config`` is accepted for signature stability (Group 7's ``l3``
    codes against this exact signature); resolution itself is host-global.
    """
    return _resolve_secure_path_binary(
        "machinectl",
        MachinectlResolutionError,
        absent_hint=(
            "no executable 'machinectl' found on the sudoers secure_path "
            "({secure_path}); the orchestrator's relative `sudo machinectl …` "
            "can never be granted by any rule. Install the systemd-container "
            "package."
        ),
        distinct_hint=(
            "machinectl does not resolve to a single canonical secure_path "
            "entry; found {count} genuinely distinct binaries: {found}. Remove "
            "the unexpected copy; the orchestrator expects the single systemd "
            "/usr/bin/machinectl."
        ),
        non_canonical_hint=(
            "machinectl resolves to a single binary but only outside a "
            "canonical systemd location; found: {found}. The orchestrator "
            "expects the single systemd /usr/bin/machinectl."
        ),
    )


def _systemd_run_binary_name() -> str:
    """The relative transient-unit launcher name, taken from ``pipe_cmd``.

    Derived from :func:`core.host_config.pipe_cmd` (a call, not a re-typed
    literal) so the launcher name has ONE source and the convention guard
    ``test_no_raw_systemd_run_outside_pipe_cmd`` is honored: ``pipe_cmd(_)`` is
    ``[<launcher>, "-q", "--pipe", "--uid=…"]``, so element 0 is the relative
    launcher whose absolute secure_path resolution this module pins.
    """
    return pipe_cmd("")[0]


def resolve_systemd_run_path(host_config: HostConfig) -> str:
    """Resolve the single canonical transient-unit launcher on secure_path.

    The C-009 design-D5 sibling of :func:`resolve_machinectl_path`: the SUDO
    separate-user crossing rides ``sudo`` + the transient-unit launcher
    (``core.host_config.sudo_pipe_cmd``), so the per-op pipe ``Cmnd_Spec`` must
    name the absolute path sudo resolves that *relative* launcher to via
    ``secure_path``. Delegates to the same :func:`_resolve_secure_path_binary`
    inode-dedup resolver (usrmerge aliases collapse to one; ≥2 genuinely-distinct
    inodes are the F-005 shadow refusal; zero is refused; the canonical
    ``/usr/bin`` path is preferred). Raises :class:`SystemdRunResolutionError`.

    ``host_config`` is accepted for signature symmetry with
    :func:`resolve_machinectl_path`; resolution itself is host-global.
    """
    return _resolve_secure_path_binary(
        _systemd_run_binary_name(),
        SystemdRunResolutionError,
        absent_hint=(
            "no executable transient-unit launcher found on the sudoers "
            "secure_path ({secure_path}); the SUDO pipe `Cmnd_Spec` can never "
            "be granted/resolved by any rule. It ships with systemd."
        ),
        distinct_hint=(
            "the transient-unit launcher does not resolve to a single canonical "
            "secure_path entry; found {count} genuinely distinct binaries: "
            "{found}. Remove the unexpected copy; the orchestrator expects the "
            "single systemd /usr/bin launcher."
        ),
        non_canonical_hint=(
            "the transient-unit launcher resolves to a single binary but only "
            "outside a canonical systemd location; found: {found}. The "
            "orchestrator expects the single systemd /usr/bin launcher."
        ),
    )


# ── Phase wiring ─────────────────────────────────────────────────────────────


def _probe(ctx: SetupContext) -> tuple[PhaseResult, str]:
    """Content-aware L0 probe (see module docstring).

    The operator is already resolved upstream and carried on ``ctx`` — L0 does
    NOT re-resolve it here (it still *defines* :func:`resolve_operator` for the
    CLI to build the context).
    """
    operator = ctx.operator

    tier, raw_id, version = classify_distro()
    if tier == "unrecognized":
        return PhaseResult.CONFLICT, unsupported_distro_refusal(raw_id, version)

    distro_family = detect_distro()
    missing = missing_binaries()
    if missing:
        cmds = "; ".join(
            _binary_install_cmd(b, distro_family) for b in missing
        )
        # A missing operator-installed prerequisite is an unconvergeable
        # REFUSAL (CONFLICT), NOT a convergeable DRIFT: L0 cannot install
        # distro packages, so the plan must show ``✗ refuse`` and the apply
        # must refuse (CONFLICT → runner never calls act), consistent with the
        # other L0 refusals (unrecognized distro, unresolvable machinectl). The
        # earlier DRIFT mis-marked it as ``⊙ will mutate`` in the plan while the
        # apply hard-FAILed via act-raise — a plan/apply contradiction (round-5
        # Debian: tlog-rec). Pattern A: detect early, refuse with an actionable
        # per-distro install hint.
        return (
            PhaseResult.CONFLICT,
            f"required binaries missing: {', '.join(missing)} — install: {cmds}",
        )

    # The machinectl-path uniqueness assertion (B-3, F-005) underpins the
    # privilege boundary's sudoers ``Cmnd_Spec`` path — a crossing-mode concern.
    # operator-rootless has no machinectl crossing (L3/L3a/L8 are skipped and the
    # runtime bypasses the dispatcher), so the assertion is gated out (D2); the
    # operator-resolution + distro + binary checks above stay unchanged.
    machinectl_detail = ""
    if not is_operator_rootless(ctx.host_config):
        try:
            machinectl_path = resolve_machinectl_path(ctx.host_config)
        except MachinectlResolutionError as exc:
            return PhaseResult.CONFLICT, str(exc)
        machinectl_detail = f", machinectl={machinectl_path}"

    detail = f"operator={operator}, distro={raw_id} ({tier}){machinectl_detail}"
    floor_warn = sudo_floor_warning()
    if floor_warn:
        detail += f" [WARN: {floor_warn}]"
    if tier == "untested":
        detail += f" [WARN: untested distro {raw_id}]"
    return PhaseResult.ALREADY_CORRECT, detail


def _act(ctx: SetupContext) -> str:
    """L0 mutates nothing — it is verify-in-probe (refusals are CONFLICT).

    A missing required binary is surfaced by :func:`_probe` as ``CONFLICT``, so
    the runner refuses without ever calling this act. The missing-binary raise
    here is therefore **defensive depth** — it keeps the invariant ("L0 cannot
    proceed with a missing prerequisite; L0 never installs packages") true even
    if some path were to call ``act`` without the probe gate. The normal path is
    ``ALREADY_CORRECT`` → no act.
    """
    distro_family = detect_distro()
    missing = missing_binaries()
    if missing:
        cmds = "; ".join(_binary_install_cmd(b, distro_family) for b in missing)
        raise RuntimeError(
            f"cannot proceed: required binaries missing ({', '.join(missing)}). "
            f"Install them and re-run: {cmds}"
        )
    return "L0 prerequisites satisfied"


def _reverify(ctx: SetupContext) -> bool:
    """L0 converged iff no required binary is missing and machinectl resolves.

    The machinectl resolution is asserted only in crossing modes; operator-
    rootless has no machinectl crossing, so it is skipped there (D2).
    """
    if missing_binaries():
        return False
    if not is_operator_rootless(ctx.host_config):
        try:
            resolve_machinectl_path(ctx.host_config)
        except MachinectlResolutionError:
            return False
    return True


def emit_distro_gate(
    *, is_tty: bool, assume_yes: bool, stream: TextIO | None = None
) -> None:
    """Emit the tier-appropriate distro warning/refusal and gate accordingly.

    Helper for the CLI surface (Group 8). On the untested tier in a TTY
    without ``--yes`` it blocks on ``input()``; a ``KeyboardInterrupt``
    (Ctrl-C) propagates so the caller exits non-zero with no further
    mutations. Unrecognized tier raises :class:`SystemExit`.
    """
    out: TextIO = sys.stderr if stream is None else stream
    tier, raw_id, version = classify_distro()
    if tier == "validated":
        return
    if tier == "unrecognized":
        print(unsupported_distro_refusal(raw_id, version), file=out)
        raise SystemExit(1)
    with_prompt = is_tty and not assume_yes
    print(
        untested_distro_warning(raw_id, version, with_prompt=with_prompt),
        file=out,
    )
    if with_prompt:
        input()


PHASE = Phase(
    id="l0",
    name="identity, distro, binaries, machinectl-path",
    identity=Identity.ROOT,
    probe=_probe,
    act=_act,
    reverify=_reverify,
    depends_on=(),
)

__all__ = [
    "PHASE",
    "SUDO_FLOOR",
    "MachinectlResolutionError",
    "OperatorResolutionError",
    "SystemdRunResolutionError",
    "classify_distro",
    "emit_distro_gate",
    "missing_binaries",
    "parse_sudo_version",
    "resolve_machinectl_path",
    "resolve_operator",
    "resolve_systemd_run_path",
    "sudo_floor_warning",
    "unsupported_distro_refusal",
    "untested_distro_warning",
]
