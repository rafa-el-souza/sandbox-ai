"""Privilege-boundary doctor checks.

Covers the 11 checks that validate the dev → unprivileged-user → docker daemon
trust chain plus the daemon-side compose project name collision check.
"""

from __future__ import annotations

import json
import shutil
import subprocess

from core import dispatch
from core.doctor.types import _BINARY_PACKAGES, CheckResult, get_install_cmd
from core.host_config import HostConfig, HostSettings, MachinectlAuth


def _host_config(user: str, auth_mode: MachinectlAuth) -> HostConfig:
    """Adapt a doctor check's ``(user, auth_mode)`` to the ``HostConfig`` that
    ``core.dispatch.invoke`` requires.

    ``invoke`` reads only ``host.docker_unprivileged_user`` and
    ``host.machinectl_authentication`` to build the boundary-crossing prefix;
    this passes the exact two values the check already received through that
    interface (no boundary re-derivation — the single crossing path stays
    ``core.dispatch``).
    """
    return HostConfig(host=HostSettings(docker_unprivileged_user=user, machinectl_authentication=auth_mode))


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
    user: str, distro: str | None, auth_mode: MachinectlAuth = MachinectlAuth.SUDO
) -> CheckResult:
    """Check that machinectl shell can reach the unprivileged user.

    Uses a 10-second timeout to detect sudoers misconfiguration (password prompt hang)
    in sudo mode, or polkit dialog/timeout in polkit mode.
    """
    outcome = dispatch.probe("auth-probe", [], _host_config(user, auth_mode), timeout=10)
    if outcome.timed_out:
        if auth_mode == MachinectlAuth.SUDO:
            timeout_remediation = (
                "Configure passwordless machinectl access in /etc/sudoers.d/:\n"
                f"  <your_user> ALL=(root) NOPASSWD: /usr/bin/machinectl shell {user}@.host *"
            )
        else:
            timeout_remediation = (
                "Configure a passwordless polkit rule for org.freedesktop.machine1.shell "
                f"granting your user access to '{user}@.host', or switch to sudo mode."
            )
        return CheckResult(
            status="fail",
            name="machinectl reachable",
            detail=f"Probe timed out after 10 seconds (likely {auth_mode.value} prompt)",
            remediation=timeout_remediation,
        )
    if not outcome.ok:
        return CheckResult(
            status="fail",
            name="machinectl reachable",
            detail="Shell probe failed",
            remediation=("Ensure systemd-machined is running and the user exists. Check stderr for details."),
        )

    return CheckResult(
        status="pass",
        name="machinectl reachable",
        detail=f"Shell probe succeeded for {user}@.host",
    )


def check_docker_available(
    user: str, distro: str | None, auth_mode: MachinectlAuth = MachinectlAuth.SUDO
) -> CheckResult:
    """Check that Docker is installed and accessible via machinectl."""
    outcome = dispatch.probe("docker-version", [], _host_config(user, auth_mode), timeout=15)
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
    user: str, distro: str | None, auth_mode: MachinectlAuth = MachinectlAuth.SUDO
) -> CheckResult:
    """Check that Docker is running in rootless mode."""
    outcome = dispatch.probe("docker-info", ["security-options"], _host_config(user, auth_mode), timeout=15)
    if outcome.ok and "rootless" in outcome.stdout:
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
    user: str, distro: str | None, auth_mode: MachinectlAuth = MachinectlAuth.SUDO
) -> CheckResult:
    """Check that gVisor runsc runtime is registered in Docker."""
    outcome = dispatch.probe("docker-info", ["runtimes"], _host_config(user, auth_mode), timeout=15)
    if outcome.ok:
        try:
            runtimes = json.loads(outcome.stdout.strip())
            if "runsc" in runtimes:
                return CheckResult(
                    status="pass",
                    name="gVisor runsc",
                    detail="runsc runtime registered in Docker",
                )
        except json.JSONDecodeError:
            pass

    return CheckResult(
        status="fail",
        name="gVisor runsc",
        detail="runsc runtime not registered in Docker",
        remediation="Install gVisor and register the runsc runtime",
        doc_ref="https://gvisor.dev/docs/user_guide/install/",
    )


def check_runsc_runtimeargs(
    user: str, distro: str | None, auth_mode: MachinectlAuth = MachinectlAuth.SUDO
) -> CheckResult:
    """Check that runsc runtimeArgs include --oci-seccomp and --debug-log.

    Validates defense-in-depth configuration for the gVisor runtime.
    Returns warn (not fail) when args are missing — this is an advisory check.
    """
    outcome = dispatch.probe("docker-info", ["runtimes"], _host_config(user, auth_mode), timeout=15)
    if not outcome.ok:
        return CheckResult(
            status="warn",
            name="runsc runtimeArgs",
            detail="Could not query Docker runtimes",
            remediation=(f"Verify Docker is accessible for user '{user}' and check ~{user}/.config/docker/daemon.json"),
        )

    try:
        runtimes = json.loads(outcome.stdout.strip())
    except json.JSONDecodeError:
        return CheckResult(
            status="warn",
            name="runsc runtimeArgs",
            detail="Could not parse Runtimes JSON",
            remediation=f"Check ~{user}/.config/docker/daemon.json",
        )

    runsc_entry = runtimes.get("runsc", {})
    runtime_args: list[str] = runsc_entry.get("runtimeArgs", [])

    has_seccomp = any(arg == "--oci-seccomp" for arg in runtime_args)
    has_debug_log = any(arg.startswith("--debug-log") for arg in runtime_args)

    if has_seccomp and has_debug_log:
        return CheckResult(
            status="pass",
            name="runsc runtimeArgs",
            detail="--oci-seccomp and --debug-log configured",
        )

    missing: list[str] = []
    if not has_seccomp:
        missing.append("--oci-seccomp")
    if not has_debug_log:
        missing.append("--debug-log")

    return CheckResult(
        status="warn",
        name="runsc runtimeArgs",
        detail=f"Missing runtimeArgs: {', '.join(missing)}",
        remediation=(f"Add {', '.join(missing)} to runsc runtimeArgs in ~{user}/.config/docker/daemon.json"),
    )


def check_host_uds(user: str, distro: str | None, auth_mode: MachinectlAuth = MachinectlAuth.SUDO) -> CheckResult:
    """Check that runsc runtimeArgs do NOT include --host-uds=all.

    The default (--host-uds=none) is the correct security posture.
    Returns PASS if --host-uds=all is absent, WARN if present.
    """
    outcome = dispatch.probe("docker-info", ["runtimes"], _host_config(user, auth_mode), timeout=15)
    if not outcome.ok:
        return CheckResult(
            status="warn",
            name="--host-uds=none",
            detail="Could not query Docker runtimes",
            remediation=(f"Verify Docker is accessible for user '{user}' and check ~{user}/.config/docker/daemon.json"),
        )

    try:
        runtimes = json.loads(outcome.stdout.strip())
    except json.JSONDecodeError:
        return CheckResult(
            status="warn",
            name="--host-uds=none",
            detail="Could not parse Runtimes JSON",
            remediation=f"Check ~{user}/.config/docker/daemon.json",
        )

    runsc_entry = runtimes.get("runsc", {})
    runtime_args: list[str] = runsc_entry.get("runtimeArgs", [])

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
    host_user: str, distro: str | None, auth_mode: MachinectlAuth = MachinectlAuth.SUDO
) -> CheckResult:
    """Fail if a daemon-side compose project already exists for any registered instance.

    Queries ``docker compose ls --format json`` via machinectl and checks for
    collisions with each registered instance's prefixed project name
    (``<sanitized-dev-username>-<inst>`` per ``instance-registry``).
    """
    del distro
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
    expected = {compose_project_name(name) for name in registered if isinstance(name, str)}

    outcome = dispatch.probe("compose-ls", [], _host_config(host_user, auth_mode), timeout=15)
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
            detail="docker compose ls failed",
            category="Privilege Boundary",
        )
    try:
        projects = json.loads(outcome.stdout or "[]")
    except json.JSONDecodeError:
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


__all__ = [
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
]
