"""Doctor module: host readiness diagnostics for sandbox operation.

Provides 16 diagnostic checks across 4 independent chains:
- Chain 1 (privilege boundary, 10 checks): sudo -> machinectl -> user -> machined
  -> reachable -> docker -> rootless -> runsc -> runsc_runtimeargs -> host_uds
- Chain 2 (filesystem, 3 checks): setfacl → ACL support → ancestor traverse
- Chain 3 (repo integrity, 2 checks): tooling plane, state dir (independent)
- Chain 4 (supply chain, 1 check): image_digests (depends on docker_available)
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import tempfile
from collections import defaultdict, deque
from dataclasses import dataclass
from importlib.resources import files as _resource_files
from typing import TYPE_CHECKING, Literal

from core.host_config import (
    HostConfig,
    HostSettings,
    MachinectlAuth,
    NoFreeGidInSubgidRangeError,
    NoSubgidRangeError,
    NoSubuidRangeError,
    SubgidOutOfRangeError,
    WorkspaceBridgeGroupMissingError,
    autodetect_workspace_bridge_gid_recommendation,
    host_id_for_in_container,
    machinectl_cmd,
    sandbox_ai_home,
    workspace_bridge_gid,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from rich.console import Console


# ─── Section 1: Data Types ──────────────────────────────────────────────────


@dataclass(frozen=True)
class CheckResult:
    """Result of a single diagnostic check."""

    status: Literal["pass", "fail", "skip", "warn"]
    name: str
    detail: str
    remediation: str | None = None
    doc_ref: str | None = None
    category: str = ""


@dataclass
class Check:
    """Declarative diagnostic check with dependency graph support."""

    id: str
    name: str
    category: str
    depends_on: list[str]
    run: Callable[[str, str | None], CheckResult]
    remediation: str
    doc_ref: str | None = None


# ─── Section 2: Distro Detection ────────────────────────────────────────────

_DISTRO_MAP: dict[str, str] = {
    "debian": "debian",
    "ubuntu": "debian",
    "fedora": "fedora",
    "rhel": "fedora",
    "centos": "fedora",
    "arch": "arch",
    "manjaro": "arch",
}

_INSTALL_CMD: dict[str, str] = {
    "debian": "sudo apt install",
    "fedora": "sudo dnf install",
    "arch": "sudo pacman -S",
}


def detect_distro() -> str | None:
    """Detect the host Linux distribution by parsing /etc/os-release.

    Returns a normalized distro family ('debian', 'fedora', 'arch') or None.
    """
    try:
        with open("/etc/os-release") as f:
            content = f.read()
    except FileNotFoundError:
        return None

    fields: dict[str, str] = {}
    for line in content.splitlines():
        if "=" in line:
            key, _, val = line.partition("=")
            fields[key.strip()] = val.strip().strip('"')

    # Check ID first, then ID_LIKE
    distro_id = fields.get("ID", "")
    if distro_id in _DISTRO_MAP:
        return _DISTRO_MAP[distro_id]

    for like in fields.get("ID_LIKE", "").split():
        if like in _DISTRO_MAP:
            return _DISTRO_MAP[like]

    return None


def get_install_cmd(distro: str | None, package: str) -> str:
    """Return a distro-aware install command for the given package."""
    if distro and distro in _INSTALL_CMD:
        return f"{_INSTALL_CMD[distro]} {package}"
    return f"Install the '{package}' package using your system package manager"


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _get_sandbox_ai_home() -> str:
    """Resolve SANDBOX_AI_HOME from the doctor module location."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─── Section 3: Binary Availability Checks ──────────────────────────────────

_BINARY_PACKAGES: dict[str, str] = {
    "sudo": "sudo",
    "machinectl": "systemd-container",
    "setfacl": "acl",
}


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


def check_setfacl(user: str, distro: str | None) -> CheckResult:
    """Check that setfacl is present on PATH."""
    path = shutil.which("setfacl")
    if path:
        return CheckResult(status="pass", name="setfacl", detail=f"Found at {path}")
    return CheckResult(
        status="fail",
        name="setfacl",
        detail="setfacl not found on PATH",
        remediation=get_install_cmd(distro, _BINARY_PACKAGES["setfacl"]),
    )


# ─── Section 4: User and systemd Checks ─────────────────────────────────────


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


# ─── Section 5: machinectl Reachability ──────────────────────────────────────


def check_machinectl_reachable(
    user: str, distro: str | None, auth_mode: MachinectlAuth = MachinectlAuth.SUDO
) -> CheckResult:
    """Check that machinectl shell can reach the unprivileged user.

    Uses a 10-second timeout to detect sudoers misconfiguration (password prompt hang)
    in sudo mode, or polkit dialog/timeout in polkit mode.
    """
    cmd = [*machinectl_cmd(user, auth_mode), "/bin/bash", "-c", "echo ok"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
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

    if result.returncode == 0:
        return CheckResult(
            status="pass",
            name="machinectl reachable",
            detail=f"Shell probe succeeded for {user}@.host",
        )
    return CheckResult(
        status="fail",
        name="machinectl reachable",
        detail=f"Shell probe failed (exit {result.returncode}): {result.stderr.strip()}",
        remediation=("Ensure systemd-machined is running and the user exists. Check stderr for details."),
    )


# ─── Section 6: Docker Checks ───────────────────────────────────────────────


def check_docker_available(
    user: str, distro: str | None, auth_mode: MachinectlAuth = MachinectlAuth.SUDO
) -> CheckResult:
    """Check that Docker is installed and accessible via machinectl."""
    result = subprocess.run(
        [
            *machinectl_cmd(user, auth_mode),
            "/bin/bash",
            "-c",
            "docker version --format '{{.Server.Version}}'",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return CheckResult(
            status="pass",
            name="Docker available",
            detail=f"Docker {result.stdout.strip()}",
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
    result = subprocess.run(
        [
            *machinectl_cmd(user, auth_mode),
            "/bin/bash",
            "-c",
            "docker info --format '{{.SecurityOptions}}'",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode == 0 and "rootless" in result.stdout:
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
    result = subprocess.run(
        [
            *machinectl_cmd(user, auth_mode),
            "/bin/bash",
            "-c",
            "docker info --format '{{json .Runtimes}}'",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode == 0:
        try:
            runtimes = json.loads(result.stdout.strip())
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
    result = subprocess.run(
        [
            *machinectl_cmd(user, auth_mode),
            "/bin/bash",
            "-c",
            "docker info --format '{{json .Runtimes}}'",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        return CheckResult(
            status="warn",
            name="runsc runtimeArgs",
            detail="Could not query Docker runtimes",
            remediation=(f"Verify Docker is accessible for user '{user}' and check ~{user}/.config/docker/daemon.json"),
        )

    try:
        runtimes = json.loads(result.stdout.strip())
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
    result = subprocess.run(
        [
            *machinectl_cmd(user, auth_mode),
            "/bin/bash",
            "-c",
            "docker info --format '{{json .Runtimes}}'",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        return CheckResult(
            status="warn",
            name="--host-uds=none",
            detail="Could not query Docker runtimes",
            remediation=(f"Verify Docker is accessible for user '{user}' and check ~{user}/.config/docker/daemon.json"),
        )

    try:
        runtimes = json.loads(result.stdout.strip())
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


# ─── Section 6b: Supply Chain Checks ────────────────────────────────────────


def check_image_digests(user: str, distro: str | None, auth_mode: MachinectlAuth = MachinectlAuth.SUDO) -> CheckResult:
    """Check that all IMAGE_REGISTRY digests are resolvable against container registries.

    Iterates IMAGE_REGISTRY and runs ``docker manifest inspect <ref>@<digest>``
    via machinectl for each entry. Returns PASS if all resolve, FAIL if any are
    stale, or SKIP if the registry is unreachable (timeout/network error).

    Additionally checks for tag drift (upstream tag re-pushed with a different
    digest) and reports it as informational detail.
    """
    from core.hydration import IMAGE_REGISTRY

    stale: list[str] = []
    drift: list[str] = []

    mc_prefix = machinectl_cmd(user, auth_mode)
    for key, pin in IMAGE_REGISTRY.items():
        # Phase 1: verify pinned digest is resolvable
        try:
            result = subprocess.run(
                [
                    *mc_prefix,
                    "/bin/bash",
                    "-c",
                    f"docker manifest inspect {pin.pinned}",
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return CheckResult(
                status="skip",
                name="image digests",
                detail="Registry unreachable (timeout during manifest inspection)",
            )

        if result.returncode != 0:
            stale.append(key)
            continue

        # Phase 2: check tag drift (informational)
        try:
            tag_result = subprocess.run(
                [
                    *mc_prefix,
                    "/bin/bash",
                    "-c",
                    f"docker manifest inspect {pin.tagged}",
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if tag_result.returncode == 0:
                try:
                    manifest = json.loads(tag_result.stdout.strip())
                    tag_digest = manifest.get("digest", "")
                    if tag_digest and tag_digest != pin.digest:
                        drift.append(key)
                except json.JSONDecodeError:
                    pass
        except subprocess.TimeoutExpired:
            pass  # Tag drift check is best-effort

    if stale:
        return CheckResult(
            status="fail",
            name="image digests",
            detail=f"Stale digests: {', '.join(stale)}",
            remediation="Run scripts/rotate_digests.py to update pinned digests",
        )

    count = len(IMAGE_REGISTRY)
    detail = f"All {count} pinned digests verified"
    if drift:
        detail += f" (tag drift detected: {', '.join(drift)})"

    return CheckResult(
        status="pass",
        name="image digests",
        detail=detail,
    )


# ─── Section 7: Filesystem Checks ───────────────────────────────────────────

# 17 unconditional source files in the packaged templates module
_UNCONDITIONAL_FILES: list[str] = [
    "docker/compose.yml",
    "docker/core/entrypoint.sh",
    "docker/admin/entrypoint.sh",
    "docker/coredns/Dockerfile.coredns",
    "config/coredns/Corefile",
    "config/dnsdist/dnsdist.conf",
    "config/proxy/squid.conf",
    "config/proxy/ERR_SANDBOX_403",
    "config/admin/.zshrc",
    "config/admin/.tmux.conf",
    "config/admin/gitmux.conf",
    "config/admin/starship.toml",
    "config/core/.bashrc",
    "config/core/.npmrc",
    "config/core/.gitconfig",
    "config/core/sshd_config",
    "config/core/CLAUDE.md",
]


_ACL_PROBE_FAILURES: tuple[type[BaseException], ...] = (subprocess.CalledProcessError, OSError)


def check_acl_support(user: str, distro: str | None) -> CheckResult:
    """Check that the filesystem supports POSIX ACLs."""
    sandbox_home = _get_sandbox_ai_home()
    try:
        with tempfile.NamedTemporaryFile(dir=sandbox_home, delete=True) as tmp:
            subprocess.run(
                ["setfacl", "-m", f"u:{os.getenv('USER', 'root')}:r", tmp.name],
                check=True,
                capture_output=True,
            )
            # Clean up the ACL
            subprocess.run(
                ["setfacl", "-b", tmp.name],
                check=False,
                capture_output=True,
            )
    except _ACL_PROBE_FAILURES:
        return CheckResult(
            status="fail",
            name="ACL support",
            detail="Filesystem does not support POSIX ACLs",
            remediation="Ensure the filesystem is mounted with ACL support (mount -o acl)",
        )
    return CheckResult(
        status="pass",
        name="ACL support",
        detail="Filesystem supports POSIX ACLs",
    )


def _has_acl_exec(directory: str, user: str) -> bool:
    """Probe getfacl for a named-user ACL entry granting execute on *directory*.

    Returns True if ``getfacl`` reports an entry matching ``user:<user>:..x``
    (execute bit set in the named-user ACL).  Returns False on any error
    (missing getfacl binary, permission denied, parse failure).
    """
    try:
        result = subprocess.run(
            ["getfacl", "--absolute-names", "--no-effective", directory],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            return False
        # Look for "user:<name>:" lines where the permission string contains 'x'
        prefix = f"user:{user}:"
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith(prefix):
                perms = line[len(prefix) :]
                if "x" in perms:
                    return True
    except subprocess.TimeoutExpired:
        pass
    except OSError:
        pass
    return False


def check_ancestor_traverse(user: str, distro: str | None) -> CheckResult:
    """Check that all ancestor directories of sandboxes/ are traversable by the sandbox user.

    Walks from SANDBOX_AI_HOME/sandboxes upward to root, checking --x permission
    via a two-tier probe: (1) os.stat() mode bits (fast, no subprocess), then
    (2) getfacl for named-user ACL entries if mode bits deny access.
    Also detects symlink divergence (WARN).
    Reports FAIL with fix command on first blocked ancestor (D10).
    """
    import pwd
    import stat

    sandbox_home = _get_sandbox_ai_home()
    sandboxes_dir = os.path.join(sandbox_home, "sandboxes")
    abs_path = os.path.abspath(sandboxes_dir)

    # Resolve the user
    try:
        pw = pwd.getpwnam(user)
        target_uid = pw.pw_uid
        target_gid = pw.pw_gid
    except KeyError:
        return CheckResult(
            status="fail",
            name="ancestor traverse",
            detail=f"User '{user}' does not exist on this host",
            remediation=f"Create user: sudo useradd --system --shell /usr/sbin/nologin {user}",
        )

    # Build ancestor chain
    components: list[str] = []
    current = abs_path
    while current != "/":
        components.append(current)
        current = os.path.dirname(current)
    components.append("/")
    components.reverse()

    # Symlink divergence check
    real_path = os.path.realpath(sandboxes_dir)
    symlink_warning = ""
    if abs_path != real_path:
        symlink_warning = (
            f"Symlink divergence detected: {abs_path} resolves to {real_path}. "
            f"ACLs may need to be applied to the real path."
        )

    # Traverse permission check
    for directory in components:
        try:
            st = os.stat(directory)
        except OSError:
            return CheckResult(
                status="fail",
                name="ancestor traverse",
                detail=f"Cannot stat directory: {directory}",
                remediation=f"Verify directory exists and is accessible: ls -la {directory}",
            )

        mode = st.st_mode
        has_exec = False
        if st.st_uid == target_uid:
            has_exec = bool(mode & stat.S_IXUSR)
        elif st.st_gid == target_gid:
            has_exec = bool(mode & stat.S_IXGRP)
        else:
            has_exec = bool(mode & stat.S_IXOTH)

        # Mode bits are blind to POSIX ACL named-user entries.
        # If mode bits deny, probe getfacl for an explicit ACL grant.
        if not has_exec:
            has_exec = _has_acl_exec(directory, user)

        if not has_exec:
            return CheckResult(
                status="fail",
                name="ancestor traverse",
                detail=f"User '{user}' lacks execute permission on {directory}",
                remediation=f"setfacl -m u:{user}:--x {directory}",
            )

    # All ancestors traversable
    if symlink_warning:
        return CheckResult(
            status="warn",
            name="ancestor traverse",
            detail=f"All ancestor directories traversable. {symlink_warning}",
        )

    return CheckResult(
        status="pass",
        name="ancestor traverse",
        detail=f"All ancestor directories traversable by '{user}'",
    )


def check_tooling_plane(user: str, distro: str | None) -> CheckResult:
    """Check that all 17 unconditional tooling plane files exist."""
    templates_root = _resource_files("templates")
    missing: list[str] = []
    for rel_path in _UNCONDITIONAL_FILES:
        if not templates_root.joinpath(rel_path).is_file():
            missing.append(rel_path)

    if not missing:
        return CheckResult(
            status="pass",
            name="tooling plane",
            detail="All 17 unconditional files present",
        )
    return CheckResult(
        status="fail",
        name="tooling plane",
        detail=f"Missing files: {', '.join(missing)}",
        remediation="Restore missing files from the repository or re-clone",
    )


def check_per_user_tree_exists(user: str, distro: str | None) -> CheckResult:
    """Verify that ``<home>/``, ``<home>/config/``, ``<home>/state/`` exist."""
    del user, distro
    home = sandbox_ai_home()
    missing: list[str] = []
    for sub in ("", "config", "state"):
        candidate = home / sub if sub else home
        if not candidate.is_dir():
            missing.append(str(candidate))
    if not missing:
        return CheckResult(
            status="pass",
            name="per-user tree exists",
            detail=f"All required directories present at {home}",
        )
    return CheckResult(
        status="fail",
        name="per-user tree exists",
        detail=f"Missing directories: {', '.join(missing)}",
        remediation=f"Run sandbox init to create the per-user tree at {home}.",
    )


def check_per_user_tree_mode(user: str, distro: str | None) -> CheckResult:
    """Warn when ``<home>``, ``<home>/config``, or ``<home>/state`` are more permissive than 0700."""
    import stat as _stat

    del user, distro
    home = sandbox_ai_home()
    drift: list[tuple[str, int]] = []
    for sub in ("", "config", "state"):
        path = home / sub if sub else home
        if not path.is_dir():
            # Tree absent — defer to existence check.
            return CheckResult(
                status="skip",
                name="per-user tree mode",
                detail="Tree not initialized (see per-user tree exists)",
            )
        mode = _stat.S_IMODE(path.stat().st_mode)
        if mode != 0o700:
            drift.append((str(path), mode))
    if not drift:
        return CheckResult(
            status="pass",
            name="per-user tree mode",
            detail="All directories are mode 0700",
        )
    paths = ", ".join(f"{p} (mode {oct(m)})" for p, m in drift)
    fix = "; ".join(f"chmod 0700 {p}" for p, _m in drift)
    return CheckResult(
        status="warn",
        name="per-user tree mode",
        detail=f"Mode drift detected: {paths}; expected 0700",
        remediation=fix,
    )


def check_legacy_cwd_files(user: str, distro: str | None) -> CheckResult:
    """Warn when legacy ``<cwd>/sandbox-ai.toml`` or ``<cwd>/.state/`` exist.

    The legacy path tokens in this docstring are intentional and load-bearing:
    they help users grepping the codebase during migration. Per the
    per-user-config-and-state-relocation change (task 14.7), do not remove
    them in future cleanups.
    """
    del user, distro
    cwd = os.getcwd()
    home = sandbox_ai_home()
    legacy: list[str] = []
    if os.path.exists(os.path.join(cwd, "sandbox-ai.toml")):
        legacy.append(os.path.join(cwd, "sandbox-ai.toml"))
    if os.path.isdir(os.path.join(cwd, ".state")):
        legacy.append(os.path.join(cwd, ".state"))
    if not legacy:
        return CheckResult(
            status="pass",
            name="legacy CWD files",
            detail="No legacy CWD-local orchestrator files detected",
        )
    return CheckResult(
        status="warn",
        name="legacy CWD files",
        detail=f"Legacy files detected: {', '.join(legacy)}",
        remediation=(
            f"Per-host config now lives at {home / 'config' / 'sandbox-ai.toml'} and orchestrator state at "
            f"{home / 'state'}. Migrate manually or delete the legacy files."
        ),
    )


def check_state_dir_writable(user: str, distro: str | None) -> CheckResult:
    """Check that the per-user ``<home>/state/`` directory is writable."""
    del user, distro
    state_dir = sandbox_ai_home() / "state"
    try:
        with tempfile.NamedTemporaryFile(dir=str(state_dir), delete=True):
            pass
        return CheckResult(
            status="pass",
            name="state dir writable",
            detail=f"{state_dir} is writable",
        )
    except OSError:
        return CheckResult(
            status="fail",
            name="state dir writable",
            detail=f"{state_dir} is not writable",
            remediation=f"Fix permissions: chmod 0700 {state_dir}",
        )


# ─── Section 8: Check Runner ────────────────────────────────────────────────


# ─── Acl-Ownership-Recipes Checks ──────────────────────────────────────────


def _load_host_settings_or_skip(check_name: str) -> HostSettings | CheckResult:
    """Helper: load HostSettings or return a CheckResult skip if absent."""
    try:
        return HostConfig.from_toml().host
    except FileNotFoundError:
        return CheckResult(
            status="skip",
            name=check_name,
            detail="sandbox-ai.toml not found; run `sandbox init` first",
            category="Workspace Bridge",
        )


def check_workspace_bridge_group_exists(host_user: str, distro: str | None) -> CheckResult:
    """Validate the workspace bridge group exists at a gid in the daemon's subgid range."""
    del distro
    settings_or_skip = _load_host_settings_or_skip("workspace bridge group")
    if isinstance(settings_or_skip, CheckResult):
        return settings_or_skip
    host = settings_or_skip
    name = f"workspace bridge group {host.workspace_bridge_group!r}"
    try:
        gid = workspace_bridge_gid(host)
    except WorkspaceBridgeGroupMissingError:
        try:
            recommended = autodetect_workspace_bridge_gid_recommendation(host_user)
            rec_str = str(recommended)
        except NoSubgidRangeError:
            rec_str = "<pick-a-gid-in-claude-sandbox-subgid-range>"
        except NoFreeGidInSubgidRangeError:
            rec_str = "<pick-a-gid-in-claude-sandbox-subgid-range>"
        remediation = (
            f"sudo groupadd -g {rec_str} {host.workspace_bridge_group} && "
            f"sudo usermod -aG {host.workspace_bridge_group} $USER && "
            "log out and back in"
        )
        return CheckResult(
            status="fail",
            name=name,
            detail=f"group {host.workspace_bridge_group!r} does not exist on this host",
            remediation=remediation,
            category="Workspace Bridge",
        )
    except SubgidOutOfRangeError as exc:
        return CheckResult(
            status="fail",
            name=name,
            detail=str(exc),
            remediation=(f"Recreate the bridge group at a gid within {host_user}'s /etc/subgid range"),
            category="Workspace Bridge",
        )
    return CheckResult(
        status="pass",
        name=name,
        detail=f"gid={gid}",
        category="Workspace Bridge",
    )


def check_dev_in_workspace_bridge_group(host_user: str, distro: str | None) -> CheckResult:
    """Validate dev's current process has the bridge gid in supplementary groups."""
    del distro
    import grp
    import pwd

    settings_or_skip = _load_host_settings_or_skip("dev in workspace bridge group")
    if isinstance(settings_or_skip, CheckResult):
        return settings_or_skip
    host = settings_or_skip
    try:
        bridge_gid = workspace_bridge_gid(host)
    except (WorkspaceBridgeGroupMissingError, SubgidOutOfRangeError, NoSubgidRangeError) as exc:
        return CheckResult(
            status="fail",
            name="dev in workspace bridge group",
            detail=str(exc),
            remediation="See `workspace bridge group` check",
            category="Workspace Bridge",
        )

    if bridge_gid in os.getgroups():
        return CheckResult(
            status="pass",
            name="dev in workspace bridge group",
            detail=f"current process supplementary groups include gid {bridge_gid}",
            category="Workspace Bridge",
        )

    current_user = pwd.getpwuid(os.getuid()).pw_name
    in_etc_group = bridge_gid in {g.gr_gid for g in grp.getgrall() if current_user in g.gr_mem}
    if in_etc_group:
        return CheckResult(
            status="fail",
            name="dev in workspace bridge group",
            detail=(
                f"User {current_user!r} is a member of {host.workspace_bridge_group!r} in "
                f"/etc/group but the current process's supplementary groups do not include gid "
                f"{bridge_gid}"
            ),
            remediation="Log out and log back in to refresh group membership",
            category="Workspace Bridge",
        )
    return CheckResult(
        status="fail",
        name="dev in workspace bridge group",
        detail=f"User {current_user!r} is not a member of {host.workspace_bridge_group!r}",
        remediation=f"sudo usermod -aG {host.workspace_bridge_group} {current_user} && relogin",
        category="Workspace Bridge",
    )


def check_subuid_resolver_works(host_user: str, distro: str | None) -> CheckResult:
    """Validate /etc/subuid is readable and host_user has a usable subuid range."""
    del distro
    try:
        host_uid = host_id_for_in_container(1000, host_user)
    except NoSubuidRangeError as exc:
        return CheckResult(
            status="fail",
            name="subuid resolver",
            detail=str(exc),
            remediation=(
                f"Add a /etc/subuid entry for {host_user} (rootless docker setup); "
                "see https://docs.docker.com/engine/security/rootless/"
            ),
            category="Workspace Bridge",
        )
    return CheckResult(
        status="pass",
        name="subuid resolver",
        detail=f"in-container uid 1000 → host uid {host_uid}",
        category="Workspace Bridge",
    )


def check_helper_image_pulled(host_user: str, distro: str | None) -> CheckResult:
    """Warn-only: helper container image is locally available."""
    del host_user, distro
    from core.hydration import IMAGE_REGISTRY

    image = IMAGE_REGISTRY["busybox_musl"].pinned
    docker_inspect_failures: tuple[type[BaseException], ...] = (FileNotFoundError, subprocess.TimeoutExpired)
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except docker_inspect_failures:
        return CheckResult(
            status="warn",
            name="helper image cached",
            detail="docker not reachable from current shell; will pull on first sandbox start",
            category="Workspace Bridge",
        )
    if result.returncode == 0:
        return CheckResult(
            status="pass",
            name="helper image cached",
            detail=f"{image} present locally",
            category="Workspace Bridge",
        )
    return CheckResult(
        status="warn",
        name="helper image cached",
        detail=f"{image} not in local cache; will be pulled on first sandbox start",
        category="Workspace Bridge",
    )


def _scan_instance_dirs() -> list[str] | None:
    """Return registered instance directories from ``<home>/state/instances.json``.

    Instance dirs currently live at ``<sandbox_ai_home>/sandboxes/<instance_id>``,
    where ``sandbox_ai_home`` is derived from this module's filesystem location
    (matches :func:`cli.main._resolve_sandbox_ai_home`). The ``__file__``-based
    resolution only works in dev checkouts; under wheel installs it points
    into ``site-packages/`` where no ``sandboxes/`` tree exists. Removing this
    coupling is deferred to the change-5 followup that retires
    ``_resolve_sandbox_ai_home``.

    Returns:
        - ``[]`` when no instances are registered (legitimate empty state).
        - A list of resolved instance dirs when registered instances are
          locatable on disk.
        - ``None`` when the registry is non-empty but the orchestrator's
          ``sandboxes/`` root cannot be located (wheel-install signal).
          Callers should treat this as "cannot scan" and skip.
    """
    state_path = sandbox_ai_home() / "state" / "instances.json"
    try:
        with open(state_path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        return []
    instances = data.get("instances", {})
    if not isinstance(instances, dict) or not instances:
        return []
    home_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sandboxes_root = os.path.join(home_root, "sandboxes")
    if not os.path.isdir(sandboxes_root):
        # Wheel install (or equivalent): registry has entries but __file__-based
        # resolution does not reach a sandboxes/ tree. Signal "cannot scan".
        return None
    candidates: list[str] = []
    for instance_id in instances.values():
        if not isinstance(instance_id, str):
            continue
        path = os.path.join(sandboxes_root, instance_id)
        if os.path.isdir(path):
            candidates.append(path)
    return candidates


def check_secrets_hydrated_restrictively(host_user: str, distro: str | None) -> CheckResult:
    """Warn-only: scan registered instances' secrets/ for world-readable mode bits."""
    del host_user, distro
    instances = _scan_instance_dirs()
    if instances is None:
        return CheckResult(
            status="skip",
            name="secrets hydrated restrictively",
            detail=(
                "registry has entries but the orchestrator's sandboxes/ tree could not "
                "be located from this module's __file__ (wheel install). Deferred until "
                "the change-5 relocation lands."
            ),
            category="Workspace Bridge",
        )
    leaks: list[str] = []
    for inst in instances:
        secrets_dir = os.path.join(inst, "secrets")
        if not os.path.isdir(secrets_dir):
            continue
        for fname in os.listdir(secrets_dir):
            fpath = os.path.join(secrets_dir, fname)
            try:
                mode = os.stat(fpath).st_mode & 0o777
            except OSError:
                continue
            if mode & 0o004:  # other::r--
                leaks.append(f"{fpath} (mode {mode:04o})")
    if leaks:
        sample = ", ".join(leaks[:3])
        return CheckResult(
            status="warn",
            name="secrets hydrated restrictively",
            detail=f"{len(leaks)} secret(s) world-readable: {sample}",
            remediation="sandbox destroy && sandbox init && sandbox start",
            category="Workspace Bridge",
        )
    return CheckResult(
        status="pass",
        name="secrets hydrated restrictively",
        detail="no world-readable secrets in registered instances",
        category="Workspace Bridge",
    )


def check_pre_existing_instance_layout(host_user: str, distro: str | None) -> CheckResult:
    """Warn-only: detect instances whose cache/log leaves are still dev-owned."""
    del distro
    cache_log_leaves = [
        ("cache/core/.claude",),
        ("cache/admin/tmux_resurrect",),
        ("log/core",),
        ("log/admin",),
    ]
    try:
        host_uid_1000 = host_id_for_in_container(1000, host_user)
    except NoSubuidRangeError:
        return CheckResult(
            status="skip",
            name="pre-existing instance layout",
            detail="cannot resolve subuid; see `subuid resolver` check",
            category="Workspace Bridge",
        )

    instances = _scan_instance_dirs()
    if instances is None:
        return CheckResult(
            status="skip",
            name="pre-existing instance layout",
            detail=(
                "registry has entries but the orchestrator's sandboxes/ tree could not "
                "be located from this module's __file__ (wheel install). Deferred until "
                "the change-5 relocation lands."
            ),
            category="Workspace Bridge",
        )
    stale: list[str] = []
    for inst in instances:
        for leaves in cache_log_leaves:
            for leaf in leaves:
                leaf_path = os.path.join(inst, leaf)
                try:
                    st = os.stat(leaf_path)
                except OSError:
                    continue
                if st.st_uid != host_uid_1000:
                    stale.append(leaf_path)
    if stale:
        sample = ", ".join(stale[:3])
        return CheckResult(
            status="warn",
            name="pre-existing instance layout",
            detail=f"{len(stale)} cache/log leaf(s) not chowned to consumer subuid: {sample}",
            remediation="sandbox destroy <instance> && sandbox init",
            category="Workspace Bridge",
        )
    return CheckResult(
        status="pass",
        name="pre-existing instance layout",
        detail="no pre-change-4 instance layout detected",
        category="Workspace Bridge",
    )


def build_check_registry(auth_mode: MachinectlAuth = MachinectlAuth.SUDO) -> list[Check]:
    """Build the doctor check registry with auth-mode-aware machinectl checks.

    When ``auth_mode == MachinectlAuth.POLKIT``, the `sudo` binary check is
    omitted from the registry and the `machinectl_reachable` check no longer
    depends on `sudo`. The 7 machinectl-invoking checks are partial-bound with
    ``auth_mode`` so they construct command prefixes via ``machinectl_cmd()``.
    """
    is_sudo = auth_mode == MachinectlAuth.SUDO
    machinectl_reachable_deps = (
        ["sudo", "machinectl", "user_exists", "systemd_machined"]
        if is_sudo
        else ["machinectl", "user_exists", "systemd_machined"]
    )

    chain1: list[Check] = []
    if is_sudo:
        chain1.append(
            Check(
                id="sudo",
                name="sudo binary",
                category="Privilege Boundary",
                depends_on=[],
                run=check_sudo,
                remediation="",
            )
        )
    chain1.extend(
        [
            Check(
                id="machinectl",
                name="machinectl binary",
                category="Privilege Boundary",
                depends_on=[],
                run=check_machinectl,
                remediation="",
            ),
            Check(
                id="user_exists",
                name="unprivileged user",
                category="Privilege Boundary",
                depends_on=[],
                run=check_user_exists,
                remediation="",
            ),
            Check(
                id="systemd_machined",
                name="systemd-machined",
                category="Privilege Boundary",
                depends_on=["machinectl"],
                run=check_systemd_machined,
                remediation="",
            ),
            Check(
                id="machinectl_reachable",
                name="machinectl reachable",
                category="Privilege Boundary",
                depends_on=machinectl_reachable_deps,
                run=functools.partial(check_machinectl_reachable, auth_mode=auth_mode),
                remediation="",
            ),
            Check(
                id="docker_available",
                name="Docker available",
                category="Privilege Boundary",
                depends_on=["machinectl_reachable"],
                run=functools.partial(check_docker_available, auth_mode=auth_mode),
                remediation="",
            ),
            Check(
                id="docker_rootless",
                name="Docker rootless",
                category="Privilege Boundary",
                depends_on=["docker_available"],
                run=functools.partial(check_docker_rootless, auth_mode=auth_mode),
                remediation="",
            ),
            Check(
                id="runsc",
                name="gVisor runsc",
                category="Privilege Boundary",
                depends_on=["docker_available"],
                run=functools.partial(check_runsc_registered, auth_mode=auth_mode),
                remediation="",
            ),
            Check(
                id="runsc_runtimeargs",
                name="runsc runtimeArgs",
                category="Privilege Boundary",
                depends_on=["runsc"],
                run=functools.partial(check_runsc_runtimeargs, auth_mode=auth_mode),
                remediation="",
            ),
            Check(
                id="host_uds",
                name="--host-uds=none",
                category="Privilege Boundary",
                depends_on=["runsc"],
                run=functools.partial(check_host_uds, auth_mode=auth_mode),
                remediation="",
            ),
        ]
    )

    return [
        *chain1,
        # Chain 2: filesystem
        Check(
            id="setfacl",
            name="setfacl binary",
            category="Filesystem",
            depends_on=[],
            run=check_setfacl,
            remediation="",
        ),
        Check(
            id="acl_support",
            name="ACL support",
            category="Filesystem",
            depends_on=["setfacl"],
            run=check_acl_support,
            remediation="",
        ),
        Check(
            id="ancestor_traverse",
            name="ancestor traverse",
            category="Filesystem",
            depends_on=["acl_support"],
            run=check_ancestor_traverse,
            remediation="",
        ),
        # Chain 3: repo integrity
        Check(
            id="tooling_plane",
            name="tooling plane",
            category="Repo Integrity",
            depends_on=[],
            run=check_tooling_plane,
            remediation="",
        ),
        Check(
            id="state_dir",
            name="state dir writable",
            category="Repo Integrity",
            depends_on=[],
            run=check_state_dir_writable,
            remediation="",
        ),
        # Chain 4: supply chain
        Check(
            id="image_digests",
            name="image digests",
            category="Supply Chain",
            depends_on=["docker_available"],
            run=functools.partial(check_image_digests, auth_mode=auth_mode),
            remediation="",
        ),
        # Chain 5: per-user tree
        Check(
            id="per_user_tree_exists",
            name="per-user tree exists",
            category="Per-User Tree",
            depends_on=[],
            run=check_per_user_tree_exists,
            remediation="",
        ),
        Check(
            id="per_user_tree_mode",
            name="per-user tree mode",
            category="Per-User Tree",
            depends_on=["per_user_tree_exists"],
            run=check_per_user_tree_mode,
            remediation="",
        ),
        Check(
            id="legacy_cwd_files",
            name="legacy CWD files",
            category="Per-User Tree",
            depends_on=[],
            run=check_legacy_cwd_files,
            remediation="",
        ),
        # Chain 6: workspace bridge group + helper-recipe prereqs
        Check(
            id="workspace_bridge_group_exists",
            name="workspace bridge group",
            category="Workspace Bridge",
            depends_on=[],
            run=check_workspace_bridge_group_exists,
            remediation="",
        ),
        Check(
            id="dev_in_workspace_bridge_group",
            name="dev in workspace bridge group",
            category="Workspace Bridge",
            depends_on=["workspace_bridge_group_exists"],
            run=check_dev_in_workspace_bridge_group,
            remediation="",
        ),
        Check(
            id="subuid_resolver_works",
            name="subuid resolver",
            category="Workspace Bridge",
            depends_on=[],
            run=check_subuid_resolver_works,
            remediation="",
        ),
        Check(
            id="helper_image_pulled",
            name="helper image cached",
            category="Workspace Bridge",
            depends_on=[],
            run=check_helper_image_pulled,
            remediation="",
        ),
        Check(
            id="secrets_hydrated_restrictively",
            name="secrets hydrated restrictively",
            category="Workspace Bridge",
            depends_on=[],
            run=check_secrets_hydrated_restrictively,
            remediation="",
        ),
        Check(
            id="pre_existing_instance_layout",
            name="pre-existing instance layout",
            category="Workspace Bridge",
            depends_on=["subuid_resolver_works"],
            run=check_pre_existing_instance_layout,
            remediation="",
        ),
    ]


def topological_sort(checks: list[Check]) -> list[Check]:
    """Topologically sort checks respecting depends_on declarations."""
    id_to_check = {c.id: c for c in checks}
    in_degree: dict[str, int] = {c.id: 0 for c in checks}
    adjacency: dict[str, list[str]] = defaultdict(list)

    for c in checks:
        for dep in c.depends_on:
            if dep in id_to_check:
                adjacency[dep].append(c.id)
                in_degree[c.id] += 1

    queue: deque[str] = deque(cid for cid, deg in in_degree.items() if deg == 0)
    sorted_ids: list[str] = []

    while queue:
        current = queue.popleft()
        sorted_ids.append(current)
        for neighbor in adjacency[current]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    return [id_to_check[cid] for cid in sorted_ids]


def run_checks(
    checks: list[Check],
    user: str,
    distro: str | None,
) -> list[CheckResult]:
    """Execute checks in topological order with cascading skip on failed deps."""
    ordered = topological_sort(checks)
    results: dict[str, CheckResult] = {}
    output: list[CheckResult] = []

    for check in ordered:
        # Check if any dependency failed
        failed_deps = [dep for dep in check.depends_on if dep in results and results[dep].status in ("fail", "skip")]

        if failed_deps:
            dep_names = ", ".join(failed_deps)
            result = CheckResult(
                status="skip",
                name=check.name,
                detail=f"skipped (requires: {dep_names})",
            )
        else:
            result = check.run(user, distro)

        results[check.id] = result
        output.append(result)

    return output


def run_check_subset(
    categories: list[str],
    user: str,
    distro: str | None,
    *,
    exclude_ids: set[str] | None = None,
    auth_mode: MachinectlAuth = MachinectlAuth.SUDO,
) -> list[CheckResult]:
    """Execute a filtered subset of doctor checks by category.

    Filters ``build_check_registry()`` by ``Check.category``, validates the
    cross-chain invariant (all ``depends_on`` references must resolve within
    the subset), then delegates to ``run_checks``.

    Args:
        categories: Category names to include.
        user: Unprivileged user to check.
        distro: Linux distribution name or None.
        exclude_ids: Optional set of check IDs to exclude from the subset.
            Excluded checks are removed *before* the cross-chain invariant
            validation. Checks that ``depends_on`` an excluded ID will be
            auto-skipped by the dependency engine.

    Raises:
        ValueError: If any ``depends_on`` reference in the filtered subset
            points to a check outside the subset.
    """
    if not categories:
        return []

    registry = build_check_registry(auth_mode)
    category_set = set(categories)
    excluded = exclude_ids or set()
    subset = [c for c in registry if c.category in category_set and c.id not in excluded]

    # Assert cross-chain invariant: every depends_on must resolve internally
    subset_ids = {c.id for c in subset}
    for check in subset:
        for dep in check.depends_on:
            if dep not in subset_ids and dep not in excluded:
                raise ValueError(
                    f"Check '{check.id}' depends on '{dep}' which is outside "
                    f"the subset (categories: {categories}). Cross-chain "
                    f"dependencies are not supported in subset execution."
                )

    return run_checks(subset, user, distro)


# ─── Section 9: Rich Output Renderer ────────────────────────────────────────


def render_results(
    results: list[CheckResult],
    *,
    console: Console | None = None,
) -> None:
    """Render check results using Rich with progressive disclosure."""
    from rich.console import Console as RichConsole
    from rich.text import Text

    if console is None:
        console = RichConsole()

    # Group by category
    grouped: dict[str, list[CheckResult]] = defaultdict(list)
    for r in results:
        cat = r.category or "General"
        grouped[cat].append(r)

    pass_count = sum(1 for r in results if r.status == "pass")
    fail_count = sum(1 for r in results if r.status == "fail")
    skip_count = sum(1 for r in results if r.status == "skip")
    warn_count = sum(1 for r in results if r.status == "warn")

    for category, checks in grouped.items():
        console.print(f"\n[bold]{category}[/bold]")
        for r in checks:
            if r.status == "pass":
                line = Text(f"  ✓ {r.name}", style="green")
                if r.detail:
                    line.append(f"  {r.detail}", style="dim")
                console.print(line)
            elif r.status == "fail":
                console.print(Text(f"  ✗ {r.name}", style="red bold"))
                console.print(f"    {r.detail}")
                if r.remediation:
                    console.print(f"    Fix: {r.remediation}", style="yellow")
                if r.doc_ref:
                    console.print(f"    Docs: {r.doc_ref}", style="dim")
            elif r.status == "warn":
                console.print(Text(f"  ⚠ {r.name}", style="yellow"))
                console.print(f"    {r.detail}")
                if r.remediation:
                    console.print(f"    Fix: {r.remediation}", style="yellow")
            elif r.status == "skip":
                console.print(Text(f"  ⊘ {r.name} — {r.detail}", style="dim"))

    # Summary line
    console.print()
    summary = f"{pass_count}/{len(results)} passed"
    if warn_count:
        summary += f" · {warn_count} warnings"
    if fail_count:
        summary += f" · {fail_count} failed"
    if skip_count:
        summary += f" · {skip_count} skipped"

    if fail_count > 0:
        style = "red bold"
    elif warn_count > 0:
        style = "yellow bold"
    else:
        style = "green bold"
    console.print(summary, style=style)
