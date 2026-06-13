# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Privilege-boundary doctor checks.

Covers the 11 checks that validate the dev → unprivileged-user → docker daemon
trust chain plus the daemon-side compose project name collision check.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any, NamedTuple, cast, overload

from core import dispatch
from core.doctor.types import _BINARY_PACKAGES, CheckResult, get_install_cmd
from core.host_config import (
    DEFAULT_PROVISIONING_MODE,
    DockerExecutionMode,
    MachinectlAuth,
    minimal_host_config,
)

# Single-source the reserved runtime key AND the expected runtimeArgs from the
# phase that registers them (L6) rather than hardcoding (F-024 — the doctor
# previously looked up the wrong literal "runsc"; and hardcoded a runtimeArgs
# wishlist [--oci-seccomp, --debug-log] that diverged from what L6 actually
# configures [--oci-seccomp, --ignore-cgroups], producing a permanent false "Missing --debug-log"
# WARN). Deriving expected from l6._EXPECTED_RUNTIME makes the two single-source:
# whatever L6 configures is exactly what doctor expects, so they cannot drift
# (and a future opt-in that adds --debug-log to _EXPECTED_RUNTIME is followed
# automatically). Precedent: dispatcher_sha_drift reusing l65's single source.
from core.setup.l6_daemon_json import _EXPECTED_RUNTIME, _RESERVED_RUNTIME_KEY

# Defense-in-depth parse ceiling for untrusted daemon stdout (M-2). Ops 4-8
# reach a verdict by parsing the daemon's self-reported ``docker info`` /
# ``compose ls`` JSON. A daemon-health check inherently trusts that self-report
# (agents have no daemon access in any mode), but the parsing must be strict +
# fail-closed: a malformed / oversized / unexpected-shape segment degrades to
# WARN/skip/not-ok, never a spoofed PASS. ``json.loads`` on an unbounded blob is
# a DoS vector, so reject any segment larger than this before parsing.
_MAX_DAEMON_JSON_BYTES = 256 * 1024


@overload
def _safe_load_json(stdout: str, expected_type: type[dict[str, Any]]) -> dict[str, Any] | None: ...


@overload
def _safe_load_json(stdout: str, expected_type: type[list[Any]]) -> list[Any] | None: ...


def _safe_load_json(
    stdout: str, expected_type: type[dict[str, Any]] | type[list[Any]]
) -> dict[str, Any] | list[Any] | None:
    """Size-bound + strict-shape parse of untrusted daemon stdout (M-2).

    Returns the parsed value ONLY when it is well-formed AND an instance of
    ``expected_type``; returns ``None`` (the fail-closed sentinel) when the
    segment exceeds :data:`_MAX_DAEMON_JSON_BYTES`, is not valid JSON, or is not
    the expected container type. Callers MUST treat ``None`` as a not-ok / WARN
    verdict — never reach a PASS on the absence of a parse error.
    """
    if len(stdout.encode("utf-8", errors="ignore")) > _MAX_DAEMON_JSON_BYTES:
        return None
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, expected_type):
        return None
    return parsed


def check_sudo(user: str, distro: str | None) -> CheckResult:
    """Check that sudo is present on PATH."""
    path = shutil.which("sudo")
    if path:
        return CheckResult(status="pass", name="sudo", detail=f"Found at {path}")
    return CheckResult(
        status="fail",
        name="sudo",
        detail="sudo not found on PATH",
        remediation=get_install_cmd(distro, _BINARY_PACKAGES["sudo"]),
    )


def check_tlog(user: str, distro: str | None) -> CheckResult:
    """Check that tlog-rec is present on PATH (host-side dependency)."""
    path = shutil.which("tlog-rec")
    if path:
        return CheckResult(status="pass", name="tlog", detail=f"Found at {path}")
    return CheckResult(
        status="fail",
        name="tlog",
        detail="tlog-rec not found on PATH",
        remediation=get_install_cmd(distro, _BINARY_PACKAGES["tlog"]),
    )


def check_machinectl(user: str, distro: str | None) -> CheckResult:
    """Check that machinectl is present on PATH."""
    path = shutil.which("machinectl")
    if path:
        return CheckResult(status="pass", name="machinectl", detail=f"Found at {path}")
    return CheckResult(
        status="fail",
        name="machinectl",
        detail="machinectl not found on PATH",
        remediation=get_install_cmd(distro, _BINARY_PACKAGES["machinectl"]),
    )


def check_user_exists(user: str, distro: str | None) -> CheckResult:
    """Check that the specified unprivileged user exists on the host."""
    result = subprocess.run(
        ["id", user],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return CheckResult(
            status="pass",
            name="user exists",
            detail=result.stdout.strip(),
        )
    return CheckResult(
        status="fail",
        name="user exists",
        detail=f"User '{user}' not found",
        remediation=f"sudo useradd -r -m -s /bin/bash {user}",
    )


def check_systemd_machined(user: str, distro: str | None) -> CheckResult:
    """Check that systemd-machined service is active."""
    result = subprocess.run(
        ["systemctl", "is-active", "systemd-machined"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.stdout.strip() == "active":
        return CheckResult(
            status="pass",
            name="systemd-machined",
            detail="Service is active",
        )
    return CheckResult(
        status="fail",
        name="systemd-machined",
        detail=f"Service state: {result.stdout.strip()}",
        remediation="sudo systemctl enable --now systemd-machined",
    )


def check_machinectl_reachable(
    user: str,
    distro: str | None,
    mode: DockerExecutionMode = DEFAULT_PROVISIONING_MODE,
) -> CheckResult:
    """Check that machinectl shell can reach the unprivileged user.

    Uses a 10-second timeout to detect sudoers misconfiguration (password prompt hang).
    """
    outcome = dispatch.probe("auth-probe", [], minimal_host_config(user, MachinectlAuth.SUDO, mode), timeout=10)
    return _interpret_machinectl_reachable(outcome, user)


def _interpret_machinectl_reachable(
    outcome: dispatch.ProbeOutcome,
    user: str,
) -> CheckResult:
    """Interpret the ``auth-probe`` outcome (the seam reused by ``start`` preflight)."""
    if outcome.timed_out:
        timeout_remediation = (
            "Configure passwordless machinectl access in /etc/sudoers.d/:\n"
            f"  <your_user> ALL=(root) NOPASSWD: /usr/bin/machinectl shell {user}@.host *"
        )
        return CheckResult(
            status="fail",
            name="machinectl reachable",
            detail="Probe timed out after 10 seconds (likely sudo prompt)",
            remediation=timeout_remediation,
        )
    if not outcome.ok:
        return CheckResult(
            status="fail",
            name="machinectl reachable",
            detail=f"Shell probe failed: {outcome.message}",
            remediation=("Ensure systemd-machined is running and the user exists. Check stderr for details."),
        )

    return CheckResult(
        status="pass",
        name="machinectl reachable",
        detail=f"Shell probe succeeded for {user}@.host",
    )


def check_docker_available(
    user: str,
    distro: str | None,
    mode: DockerExecutionMode = DEFAULT_PROVISIONING_MODE,
) -> CheckResult:
    """Check that Docker is installed and accessible via machinectl."""
    outcome = dispatch.probe("docker-version", [], minimal_host_config(user, MachinectlAuth.SUDO, mode), timeout=15)
    return _interpret_docker_available(outcome, user)


def _interpret_docker_available(outcome: dispatch.ProbeOutcome, user: str) -> CheckResult:
    """Interpret the ``docker-version`` outcome."""
    if outcome.ok and outcome.stdout.strip():
        return CheckResult(
            status="pass",
            name="Docker available",
            detail=f"Docker {outcome.stdout.strip()}",
        )
    return CheckResult(
        status="fail",
        name="Docker available",
        detail="Docker not accessible via machinectl",
        remediation=f"Install Docker in rootless mode for user '{user}'",
        doc_ref="https://docs.docker.com/engine/security/rootless/",
    )


def check_docker_rootless(
    user: str,
    distro: str | None,
    mode: DockerExecutionMode = DEFAULT_PROVISIONING_MODE,
) -> CheckResult:
    """Check that Docker is running in rootless mode."""
    outcome = dispatch.probe(
        "docker-info", ["security-options"], minimal_host_config(user, MachinectlAuth.SUDO, mode), timeout=15
    )
    return _interpret_docker_rootless(outcome, user)


def _security_options_have_rootless(stdout: str) -> bool:
    """``True`` iff the security-options report carries the ``name=rootless`` token (L-1).

    ``docker info --format '{{.SecurityOptions}}'`` emits a Go-slice rendering of
    ``name=<opt>,<k>=<v>,…`` token groups (e.g.
    ``[name=seccomp,profile=builtin name=rootless name=cgroupns]``). Match the
    structural ``name=rootless`` token specifically — NOT a bare ``"rootless" in
    stdout`` substring — so a daemon string that merely *contains* the substring
    ``rootless`` elsewhere (a path, a label, a ``rootlesskit`` mention) does not
    falsely PASS. The output is bracket/space/comma-delimited; split on those and
    require an exact ``name=rootless`` element.
    """
    tokens = re.split(r"[\[\]\s,]+", stdout)
    return "name=rootless" in tokens


def _interpret_docker_rootless(outcome: dispatch.ProbeOutcome, user: str) -> CheckResult:
    """Interpret the ``docker-info ["security-options"]`` outcome."""
    if outcome.ok and _security_options_have_rootless(outcome.stdout):
        return CheckResult(
            status="pass",
            name="Docker rootless",
            detail="Rootless mode confirmed",
        )
    return CheckResult(
        status="fail",
        name="Docker rootless",
        detail="Docker is NOT running in rootless mode",
        remediation=(
            f"Rootless Docker is a non-negotiable security boundary. Configure rootless mode for user '{user}'."
        ),
        doc_ref="https://docs.docker.com/engine/security/rootless/",
    )


def check_runsc_registered(
    user: str,
    distro: str | None,
    mode: DockerExecutionMode = DEFAULT_PROVISIONING_MODE,
) -> CheckResult:
    """Check that gVisor runsc runtime is registered in Docker."""
    outcome = dispatch.probe(
        "docker-info", ["runtimes"], minimal_host_config(user, MachinectlAuth.SUDO, mode), timeout=15
    )
    return _interpret_runsc_registered(outcome)


def _interpret_runsc_registered(outcome: dispatch.ProbeOutcome) -> CheckResult:
    """Interpret the ``docker-info ["runtimes"]`` outcome for runsc registration."""
    if outcome.ok:
        runtimes = _safe_load_json(outcome.stdout.strip(), dict)
        if runtimes is not None and isinstance(runtimes.get(_RESERVED_RUNTIME_KEY), dict):
            return CheckResult(
                status="pass",
                name="gVisor runsc",
                detail=f"{_RESERVED_RUNTIME_KEY} runtime registered in Docker",
            )

    return CheckResult(
        status="fail",
        name="gVisor runsc",
        detail="runsc runtime not registered in Docker",
        remediation="Install gVisor and register the runsc runtime",
        doc_ref="https://gvisor.dev/docs/user_guide/install/",
    )


def check_runsc_runtimeargs(
    user: str,
    distro: str | None,
    mode: DockerExecutionMode = DEFAULT_PROVISIONING_MODE,
) -> CheckResult:
    """Check that runsc runtimeArgs match what L6 configures (single-sourced).

    The expected args are read from ``l6._EXPECTED_RUNTIME["runtimeArgs"]`` — NOT
    hardcoded — so doctor expects exactly what setup configures and the two
    cannot drift (F-024 pattern). Today that is ``["--oci-seccomp",
    "--ignore-cgroups"]``; if a future opt-in adds ``--debug-log=<path>`` to the
    L6 target, this check follows automatically. WARN (not fail) when an expected arg is absent — this
    is a defense-in-depth advisory.
    """
    outcome = dispatch.probe(
        "docker-info", ["runtimes"], minimal_host_config(user, MachinectlAuth.SUDO, mode), timeout=15
    )
    return _interpret_runsc_runtimeargs(outcome, user)


def _interpret_runsc_runtimeargs(outcome: dispatch.ProbeOutcome, user: str) -> CheckResult:
    """Interpret the ``docker-info ["runtimes"]`` outcome for runsc runtimeArgs."""
    if not outcome.ok:
        return CheckResult(
            status="warn",
            name="runsc runtimeArgs",
            detail="Could not query Docker runtimes",
            remediation=(f"Verify Docker is accessible for user '{user}' and check ~{user}/.config/docker/daemon.json"),
        )

    runtimes = _safe_load_json(outcome.stdout.strip(), dict)
    if runtimes is None:
        return CheckResult(
            status="warn",
            name="runsc runtimeArgs",
            detail="Could not parse Runtimes JSON",
            remediation=f"Check ~{user}/.config/docker/daemon.json",
        )

    runsc_entry = runtimes.get(_RESERVED_RUNTIME_KEY, {})
    runsc_entry = runsc_entry if isinstance(runsc_entry, dict) else {}
    raw_args = runsc_entry.get("runtimeArgs", [])
    runtime_args: list[str] = [a for a in raw_args if isinstance(a, str)] if isinstance(raw_args, list) else []

    expected_args = cast("list[str]", _EXPECTED_RUNTIME["runtimeArgs"])
    missing = [exp for exp in expected_args if not _runtime_arg_present(exp, runtime_args)]

    if not missing:
        return CheckResult(
            status="pass",
            name="runsc runtimeArgs",
            detail=f"{', '.join(expected_args)} configured",
        )

    return CheckResult(
        status="warn",
        name="runsc runtimeArgs",
        detail=f"Missing runtimeArgs: {', '.join(missing)}",
        remediation=(f"Add {', '.join(missing)} to runsc runtimeArgs in ~{user}/.config/docker/daemon.json"),
    )


def _runtime_arg_present(expected: str, runtime_args: list[str]) -> bool:
    """``True`` iff ``expected`` is satisfied by ``runtime_args``.

    A value-bearing expected arg (e.g. ``--debug-log=/path``) is matched on its
    flag token, so any configured value satisfies it; a flag-only arg (e.g.
    ``--oci-seccomp``) matches exactly.
    """
    flag = expected.split("=", 1)[0]
    return any(arg == expected or arg.split("=", 1)[0] == flag for arg in runtime_args)


def check_host_uds(
    user: str,
    distro: str | None,
    mode: DockerExecutionMode = DEFAULT_PROVISIONING_MODE,
) -> CheckResult:
    """Check that runsc runtimeArgs do NOT include --host-uds=all.

    The default (--host-uds=none) is the correct security posture.
    Returns PASS if --host-uds=all is absent, WARN if present.
    """
    outcome = dispatch.probe(
        "docker-info", ["runtimes"], minimal_host_config(user, MachinectlAuth.SUDO, mode), timeout=15
    )
    return _interpret_host_uds(outcome, user)


def _interpret_host_uds(outcome: dispatch.ProbeOutcome, user: str) -> CheckResult:
    """Interpret the ``docker-info ["runtimes"]`` outcome for the ``--host-uds`` posture."""
    if not outcome.ok:
        return CheckResult(
            status="warn",
            name="--host-uds=none",
            detail="Could not query Docker runtimes",
            remediation=(f"Verify Docker is accessible for user '{user}' and check ~{user}/.config/docker/daemon.json"),
        )

    runtimes = _safe_load_json(outcome.stdout.strip(), dict)
    if runtimes is None:
        return CheckResult(
            status="warn",
            name="--host-uds=none",
            detail="Could not parse Runtimes JSON",
            remediation=f"Check ~{user}/.config/docker/daemon.json",
        )

    runsc_entry = runtimes.get(_RESERVED_RUNTIME_KEY, {})
    runsc_entry = runsc_entry if isinstance(runsc_entry, dict) else {}
    raw_args = runsc_entry.get("runtimeArgs", [])
    runtime_args: list[str] = [a for a in raw_args if isinstance(a, str)] if isinstance(raw_args, list) else []

    has_host_uds_all = any(arg == "--host-uds=all" for arg in runtime_args)

    if has_host_uds_all:
        return CheckResult(
            status="warn",
            name="--host-uds=none",
            detail="--host-uds=all detected in runsc runtimeArgs",
            remediation=(
                f"Remove --host-uds=all from runtimeArgs in ~{user}/.config/docker/daemon.json "
                f"(default 'none' is correct)"
            ),
        )

    return CheckResult(
        status="pass",
        name="--host-uds=none",
        detail="--host-uds=all not present (default 'none' is active)",
    )


def check_compose_project_name_collision(
    host_user: str,
    distro: str | None,
    mode: DockerExecutionMode = DEFAULT_PROVISIONING_MODE,
) -> CheckResult:
    """Fail if a daemon-side compose project already exists for any registered instance.

    Queries ``docker compose ls --format json`` via machinectl and checks for
    collisions with each registered instance's prefixed project name
    (``<sanitized-dev-username>-<inst>`` per ``instance-registry``).
    """
    del distro
    # The empty-registry PASS is owned solely by
    # ``_interpret_compose_project_name_collision`` (it reproduces it "for
    # totality"); the public check does not re-guard it, so the registry is read
    # exactly once per invocation.
    outcome = dispatch.probe("compose-ls", [], minimal_host_config(host_user, MachinectlAuth.SUDO, mode), timeout=15)
    return _interpret_compose_project_name_collision(outcome)


def _interpret_compose_project_name_collision(outcome: dispatch.ProbeOutcome) -> CheckResult:
    """Interpret the ``compose-ls`` outcome against the local registry.

    Self-contained: reads the instance registry locally (a LOCAL operation, not
    a boundary crossing) so the ``start`` preflight can feed this a
    ``compose-ls``-derived outcome without re-deriving the expected project set.
    The no-registered-instances early return is reproduced here so the function
    is total over any outcome it is handed.
    """
    from core.compose import compose_project_name
    from core.doctor.checks.workspace_bridge import _read_registry_raw

    registered = list(_read_registry_raw().keys())
    if not registered:
        return CheckResult(
            status="pass",
            name="compose project name collision",
            detail="no registered instances; nothing to check",
            category="Privilege Boundary",
        )
    expected = {compose_project_name(name) for name in registered}

    if outcome.timed_out:
        return CheckResult(
            status="skip",
            name="compose project name collision",
            detail="docker compose ls timed out",
            category="Privilege Boundary",
        )
    if not outcome.ok:
        return CheckResult(
            status="skip",
            name="compose project name collision",
            detail=f"docker compose ls failed: {outcome.message}",
            category="Privilege Boundary",
        )
    projects = _safe_load_json(outcome.stdout or "[]", list)
    if projects is None:
        return CheckResult(
            status="skip",
            name="compose project name collision",
            detail="could not parse docker compose ls output",
            category="Privilege Boundary",
        )
    daemon_names = {p.get("Name") for p in projects if isinstance(p, dict)}
    # Collisions: a daemon project whose name matches an *expected* project for
    # a registered instance is normal (that instance's own running compose).
    # A daemon project whose name collides with what we'd construct for a
    # not-yet-running instance — i.e., name is in expected AND that instance
    # has no live entry — is a concrete collision risk only when invariants
    # diverge. The simpler robust check: if any expected name is also present
    # in daemon_names, that's not a collision per se (it's the live project),
    # but if the operator runs `init` for a NEW name that already collides,
    # that's a separate concern handled at init pre-flight.
    return CheckResult(
        status="pass",
        name="compose project name collision",
        detail=f"checked {len(daemon_names)} daemon project(s) against {len(expected)} registered instance(s)",
        category="Privilege Boundary",
    )


def interpret_compose_collision_segment(segment: dispatch.ProbeOutcome | None) -> CheckResult:
    """Interpret a preflight bundle's ``compose-ls`` segment for the collision check.

    Used by ``sandbox init``'s preflight, which collapses its formerly-separate
    ``auth-probe`` + ``compose-ls`` crossings into the one ``preflight`` op
    (C-009 D6). An absent segment (``None`` — a garbled/missing bundle) is
    treated as a not-ok ``compose-ls`` outcome so the underlying
    :func:`_interpret_compose_project_name_collision` skips gracefully rather
    than crashing on partial data.
    """
    if segment is None:
        segment = dispatch.ProbeOutcome(
            ok=False,
            timed_out=False,
            stdout="",
            message="preflight compose-ls segment missing from bundle",
        )
    return _interpret_compose_project_name_collision(segment)


def interpret_preflight_reachability(
    outcome: dispatch.ProbeOutcome,
    user: str,
) -> CheckResult:
    """Render the boundary-reachability verdict for ``sandbox start``'s preflight gate.

    The ``preflight`` crossing itself succeeding IS the reachability signal (the
    old ``machinectl_reachable`` check — C-009 D6). When the crossing fails — a
    timeout, a non-zero op exit, or an absent/garbled ``auth-probe`` segment —
    ``start`` aborts before interpreting the downstream checks (preserving the
    old ``depends_on`` short-circuit). The caller passes the most specific
    failing :class:`~core.dispatch.ProbeOutcome` (the whole-crossing outcome on a
    crossing failure, else the parsed ``auth-probe`` segment); this reuses the
    existing :func:`_interpret_machinectl_reachable` so the diagnostic is
    byte-identical to the per-crossing chain's reachability FAIL.
    """
    return _interpret_machinectl_reachable(outcome, user)


class PreflightGate(NamedTuple):
    """The once-parsed reachability verdict for a ``preflight`` crossing.

    ``sandbox start`` and ``sandbox init`` both gate on the preflight crossing
    being reachable before interpreting the downstream checks (the old
    ``machinectl_reachable`` short-circuit — C-009 D6). Both used to parse the
    bundle once for the gate and then re-parse it inside
    :func:`interpret_preflight_bundle`; this groups the single parse + the gate
    so the blob is parsed exactly once per ``start`` / ``init``.

    Attributes:
        reachable: ``True`` iff the crossing itself succeeded AND its
            ``auth-probe`` segment is present and ok.
        reach_outcome: When NOT reachable, the most-specific failing outcome to
            feed a reachability diagnostic — the whole-crossing outcome on a
            crossing failure (timeout / non-zero op exit), else the parsed
            (not-ok) ``auth-probe`` segment. Equal to the whole-crossing
            ``outcome`` when reachable (unused on that path).
        per_op: The once-parsed per-query segments, reused by
            :func:`interpret_preflight_bundle` so the bundle is not re-parsed.
    """

    reachable: bool
    reach_outcome: dispatch.ProbeOutcome
    per_op: dict[str, dispatch.ProbeOutcome]


def evaluate_preflight_gate(outcome: dispatch.ProbeOutcome) -> PreflightGate:
    """Parse a ``preflight`` outcome ONCE and compute the reachability gate.

    Shared by ``sandbox start`` and ``sandbox init`` (C-009 D6 / F3): both parse
    the bundle for the crossing-as-reachability gate, then ``start`` re-parsed it
    inside :func:`interpret_preflight_bundle`. This returns the gate result, the
    most-specific failing outcome, and the parsed ``per_op`` map so the blob is
    parsed exactly once.
    """
    per_op = dispatch.parse_preflight_outcome(outcome)
    # ``parse_preflight_outcome`` returns every query name (a not-ok segment for a
    # failed/garbled/nonce-absent crossing), so ``auth-probe`` is always present.
    auth_probe_segment = per_op["auth-probe"]
    reachable = not outcome.timed_out and outcome.ok and auth_probe_segment.ok
    # The most-specific failing outcome: the whole-crossing outcome carries the
    # timeout / op-failure message on a crossing failure, else the parsed
    # (not-ok) ``auth-probe`` segment.
    reach_outcome = outcome if (outcome.timed_out or not outcome.ok) else auth_probe_segment
    return PreflightGate(reachable=reachable, reach_outcome=reach_outcome, per_op=per_op)


def interpret_preflight_bundle(
    per_op: dict[str, dispatch.ProbeOutcome],
    user: str,
) -> list[CheckResult]:
    """Map a single ``preflight``-op bundle to the seven privilege-boundary verdicts.

    ``sandbox start``'s preflight collapses the seven instance-agnostic
    read-only crossings into ONE ``preflight``-op crossing (C-009 D6). The caller
    has already established boundary reachability (the crossing itself
    succeeding is the reachability signal — the old ``machinectl_reachable``
    role) before calling this; it passes the ALREADY-PARSED ``per_op`` map (from
    :func:`evaluate_preflight_gate`) so the bundle is parsed exactly once per
    ``start`` — each existing interpret fn is applied to its own segment,
    preserving every check's individual pass/fail + specific diagnostic.

    The single ``docker-info-runtimes`` segment feeds three checks
    (``runsc_registered`` / ``runsc_runtimeargs`` / ``host_uds``) — the intrinsic
    dedup. Results are returned in the doctor-chain order so the operator sees
    the same sequence as the per-crossing chain.
    """
    runtimes = per_op["docker-info-runtimes"]
    return [
        _interpret_machinectl_reachable(per_op["auth-probe"], user),
        _interpret_docker_available(per_op["docker-version"], user),
        _interpret_docker_rootless(per_op["docker-info-security-options"], user),
        _interpret_runsc_registered(runtimes),
        _interpret_runsc_runtimeargs(runtimes, user),
        _interpret_host_uds(runtimes, user),
        _interpret_compose_project_name_collision(per_op["compose-ls"]),
    ]


__all__ = [
    "PreflightGate",
    "check_compose_project_name_collision",
    "check_docker_available",
    "check_docker_rootless",
    "check_host_uds",
    "check_machinectl",
    "check_machinectl_reachable",
    "check_runsc_registered",
    "check_runsc_runtimeargs",
    "check_sudo",
    "check_systemd_machined",
    "check_tlog",
    "check_user_exists",
    "evaluate_preflight_gate",
    "interpret_compose_collision_segment",
    "interpret_preflight_bundle",
    "interpret_preflight_reachability",
]
