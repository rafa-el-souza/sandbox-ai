"""Filesystem-related doctor checks: setfacl binary, ACL probe, ancestor traverse."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

from core import dispatch
from core.doctor.types import _BINARY_PACKAGES, CheckResult, get_install_cmd
from core.host_config import MachinectlAuth, minimal_host_config, sandbox_ai_home

_ACL_PROBE_FAILURES: tuple[type[BaseException], ...] = (subprocess.CalledProcessError, OSError)


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


def check_acl_support(user: str, distro: str | None) -> CheckResult:
    """Check that the filesystem supports POSIX ACLs."""
    sandbox_home = str(sandbox_ai_home())
    try:
        with tempfile.NamedTemporaryFile(dir=sandbox_home, delete=True) as tmp:
            subprocess.run(
                ["setfacl", "-m", f"u:{os.getenv('USER', 'root')}:r", tmp.name],
                check=True,
                capture_output=True,
            )
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


def check_ancestor_traverse(
    user: str, distro: str | None, auth_mode: MachinectlAuth = MachinectlAuth.SUDO
) -> CheckResult:
    """Check that all ancestor directories of sandboxes/ are traversable by the sandbox user.

    Walks from SANDBOX_AI_HOME/sandboxes upward to root, checking --x permission
    via a two-tier probe: (1) os.stat() mode bits (fast, no subprocess), then
    (2) getfacl for named-user ACL entries if mode bits deny access.
    Also detects symlink divergence (WARN).

    The ancestor-traverse ACL on the operator's home is granted at the **first
    `sandbox start`** (lifecycle: granted-once/persistent), NOT by setup or
    `sandbox init`. So a blocked ancestor on a freshly-set-up host with no
    sandbox started yet is expected, not a defect: report SKIP ("applied at
    first start"). Only when a sandbox is actually running (compose-ls > 0) but
    traverse is still missing is it a real FAIL — that sandbox cannot reach its
    workspace.
    """
    import pwd
    import stat

    sandbox_home = sandbox_ai_home()
    instances_dir = sandbox_home / "instances"
    abs_path = os.path.abspath(instances_dir)

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

    components: list[str] = []
    current = abs_path
    while current != "/":
        components.append(current)
        current = os.path.dirname(current)
    components.append("/")
    components.reverse()

    real_path = os.path.realpath(instances_dir)
    symlink_warning = ""
    if abs_path != real_path:
        symlink_warning = (
            f"Symlink divergence detected: {abs_path} resolves to {real_path}. "
            f"ACLs may need to be applied to the real path."
        )

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

        if not has_exec:
            has_exec = _has_acl_exec(directory, user)

        if not has_exec:
            if _no_sandbox_running(user, auth_mode):
                return CheckResult(
                    status="skip",
                    name="ancestor traverse",
                    detail=(
                        f"User '{user}' cannot yet traverse to {directory}; the "
                        f"ancestor-traverse ACL is granted at first 'sandbox "
                        f"start' (no sandbox running). It will be applied then."
                    ),
                    remediation=f"start a sandbox, or grant manually: setfacl -m u:{user}:--x {directory}",
                )
            return CheckResult(
                status="fail",
                name="ancestor traverse",
                detail=f"User '{user}' lacks execute permission on {directory}",
                remediation=f"setfacl -m u:{user}:--x {directory}",
            )

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


def _no_sandbox_running(user: str, auth_mode: MachinectlAuth) -> bool:
    """``True`` iff the sandbox daemon reports zero compose projects.

    Distinguishes "no sandbox started yet" (traverse-absent is expected — the
    grant is a first-`start` artifact) from "a sandbox is running but traverse
    is missing" (a real failure). Fail-safe: if the daemon can't be queried
    (docker down / probe failure / unparseable output), return ``False`` so the
    caller reports the real traverse gap rather than hiding it behind a SKIP.
    """
    outcome = dispatch.probe("compose-ls", [], minimal_host_config(user, auth_mode), timeout=15)
    if not outcome.ok:
        return False
    try:
        projects = json.loads(outcome.stdout.strip() or "[]")
    except json.JSONDecodeError:
        return False
    return isinstance(projects, list) and len(projects) == 0


__all__ = [
    "check_acl_support",
    "check_ancestor_traverse",
    "check_setfacl",
]
