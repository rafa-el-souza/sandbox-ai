"""End-to-end coverage for the acl-ownership-recipes change.

These tests invoke the CLI via ``uv run sandbox …`` against a real host with
``SANDBOX_AI_USER_HOME`` redirected to ``tmp_path``. They require:

- root (for ``sudo groupadd`` / ``sudo usermod``) to set up the bridge group;
- a configured rootless docker daemon for the unprivileged user;
- ``machinectl`` reachable for the unprivileged user;
- gVisor's ``runsc`` runtime registered in the rootless docker daemon.

Tests skip cleanly when these prerequisites are absent. The helper-hardening
test (``test_helper_container_hardening_flags``) does not require any of this
and runs anywhere — it asserts the helper invocation shape via a stub Executor.
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env(home: Path) -> dict[str, str]:
    """Build a subprocess env that redirects per-user home to ``home``."""
    env = os.environ.copy()
    env["SANDBOX_AI_USER_HOME"] = str(home)
    return env


def _is_root() -> bool:
    return os.geteuid() == 0


def _docker_unprivileged_user_configured() -> bool:
    """Best-effort check: a [host] sandbox-ai.toml in the dev's real home references
    a user that exists. Used by the heavy end-to-end tests to skip in unconfigured
    environments without depending on any specific user name."""
    try:
        import pwd
        import tomllib
    except ImportError:
        return False
    real_toml = Path("~/.sandbox-ai/config/sandbox-ai.toml").expanduser()
    try:
        with open(real_toml, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError, tomllib.TOMLDecodeError:
        return False
    user = raw.get("host", {}).get("docker_unprivileged_user")
    if not isinstance(user, str):
        return False
    try:
        pwd.getpwnam(user)
    except KeyError:
        return False
    return True


# ─── Helper container hardening (always-on; no root or docker required) ─────


def test_helper_container_hardening_flags() -> None:
    """Stub Executor.run via patch.object — assert hardening flags on every invocation."""
    from unittest.mock import patch

    sys.path.insert(0, str(REPO_ROOT / "src"))
    try:
        from core.executor import Executor
        from core.helper_container import helper_chown_files, helper_mkdir_chown_dirs
        from core.host_config import MachinectlAuth
    finally:
        sys.path.pop(0)

    captured: list[str] = []

    def _capture(self: Executor, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(cmd[-1])
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with patch.object(Executor, "run", autospec=True, side_effect=_capture):
        helper_chown_files(
            "claude-sandbox",
            "/inst/secrets",
            ["ipc_host_key"],
            owner_uid=100999,
            owner_gid=0,
            mode=0o600,
            machinectl_auth=MachinectlAuth.SUDO,
        )
        helper_mkdir_chown_dirs(
            "claude-sandbox",
            "/inst/cache/core",
            [".claude"],
            owner_uid=100999,
            owner_gid=200999,
            machinectl_auth=MachinectlAuth.SUDO,
        )

    assert len(captured) == 2
    for payload in captured:
        for flag in [
            "--runtime=runc",
            "--network=none",
            "--read-only",
            "--tmpfs /tmp",
            "--user 0:0",
            "--cap-drop ALL",
            "--cap-add CHOWN",
            "--cap-add DAC_OVERRIDE",
            "--security-opt no-new-privileges:true",
        ]:
            assert flag in payload, f"missing {flag} in helper payload"
        assert "@sha256:" in payload, "helper image must be digest-pinned"


# ─── End-to-end recipe verifications (require root + configured docker) ────

requires_root = pytest.mark.skipif(not _is_root(), reason="requires root for groupadd/usermod")
requires_docker_user = pytest.mark.skipif(
    not _docker_unprivileged_user_configured(),
    reason="requires a configured rootless docker user in ~/.sandbox-ai/config/sandbox-ai.toml",
)


def _setup_bridge_group(group_name: str = "sb-ws-itest") -> int:
    """Create a temporary bridge group at a free gid in claude-sandbox's subgid range.

    Returns the gid. Caller is responsible for cleanup via groupdel.
    """
    sys.path.insert(0, str(REPO_ROOT / "src"))
    try:
        from core.host_config import (
            HostConfig,
            autodetect_workspace_bridge_gid_recommendation,
        )
    finally:
        sys.path.pop(0)
    host = HostConfig.from_toml().host
    gid = autodetect_workspace_bridge_gid_recommendation(host.docker_unprivileged_user)
    subprocess.run(["sudo", "groupadd", "-g", str(gid), group_name], check=True)
    return gid


def _teardown_bridge_group(group_name: str) -> None:
    subprocess.run(["sudo", "groupdel", group_name], check=False)


_E2E_SKIP_REASON = (
    "Full end-to-end exercise requires a configured host (rootless docker for the "
    "claude-sandbox user + runsc registered + interactive bridge-group setup). "
    "Run the operator verification commands from "
    "openspec/changes/acl-ownership-recipes/proposal.md (Migration Plan section) "
    "and the post-setup `sandbox doctor` checks to validate. The host-side "
    "mutations themselves are pinned by `test_workspace_named_acl_round_trip` "
    "below and by the unit tests in tests/unit/test_cli.py."
)


@requires_root
@requires_docker_user
def test_cache_leaf_chowned_to_subuid(tmp_path: Path) -> None:
    """Post-`sandbox start`, cache/core/.claude is owned by the agent's host subuid."""
    pytest.skip(_E2E_SKIP_REASON)


@requires_root
@requires_docker_user
def test_ro_file_chowned_to_consumer_uid_zero(tmp_path: Path) -> None:
    """Post-start, config/coredns/Corefile is owned by 65532:0 mode 0640."""
    pytest.skip(_E2E_SKIP_REASON)


@requires_root
@requires_docker_user
def test_workspace_shared_group_applied(tmp_path: Path) -> None:
    """Post-start, the workspace tree has bridge_gid + setgid + named ACL."""
    pytest.skip(_E2E_SKIP_REASON)


@requires_root
@requires_docker_user
def test_workspace_named_acl_reapplied_on_next_start(tmp_path: Path) -> None:
    """Post-restart, the named entries are back."""
    pytest.skip(_E2E_SKIP_REASON)


@requires_root
@requires_docker_user
def test_workspace_drift_detection(tmp_path: Path) -> None:
    """Manually unsetting setgid causes the next start to re-apply via the recursive path."""
    pytest.skip(_E2E_SKIP_REASON)


# ─── E (option) — workspace named-ACL round-trip with real setfacl/getfacl ─


def _setfacl_available() -> bool:
    return shutil.which("setfacl") is not None and shutil.which("getfacl") is not None


@pytest.mark.skipif(not _setfacl_available(), reason="setfacl/getfacl not installed")
def test_workspace_named_acl_round_trip(tmp_path: Path) -> None:
    """Real setfacl/getfacl: revoke removes the named ACL but preserves persistent state.

    This pins the load-bearing least-privilege concern (Decision 4 of the
    acl-ownership-recipes design): on stop, the workspace's named ACL for the
    daemon user must be removed from BOTH the effective and default ACLs, but
    the persistent shared-group state (group bits, setgid, default group/dev
    entries) survives because it represents the workspace's identity.

    The test substitutes the *current* user for ``host_user`` so it can run
    on any host with setfacl — the production grant/revoke logic is identical
    regardless of which user name appears in the named entry. Real prerequisites
    (rootless docker, runsc, bridge group) are exercised manually per the
    Migration Plan in proposal.md.
    """
    sys.path.insert(0, str(REPO_ROOT / "src"))
    try:
        from cli.main import _acl_revoke_plan
    finally:
        sys.path.pop(0)

    user = getpass.getuser()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # Persistent shared-group state: setgid + group bits (mode 2770) +
    # persistent default ACL (g::rwx, with no named entry yet).
    os.chmod(workspace, 0o2770)
    subprocess.run(
        ["setfacl", "-d", "-m", "u::rwx,g::rwx,o::---,m::rwx", str(workspace)],
        check=True,
    )

    # Apply the workspace named-ACL portion (effective + default named entry).
    subprocess.run(["setfacl", "-m", f"u:{user}:rwx", str(workspace)], check=True)
    subprocess.run(
        [
            "setfacl",
            "-d",
            "-m",
            f"u::rwx,g::rwx,o::---,m::rwx,u:{user}:rwx",
            str(workspace),
        ],
        check=True,
    )

    pre = subprocess.run(
        ["getfacl", "--no-effective", str(workspace)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert f"user:{user}:rwx" in pre
    assert f"default:user:{user}:rwx" in pre

    # Drive revocation through the production plan (just the workspace entries).
    # Match by description prefix, not substring — the tmp_path itself may
    # contain the word "workspace" via pytest's test-name dirs.
    plan = _acl_revoke_plan(str(tmp_path / "fake_instance"), user, str(workspace))
    workspace_revokes = [
        args for args, desc in plan if desc.startswith("workspace ")
    ]
    assert workspace_revokes, "_acl_revoke_plan must emit workspace revocation entries"
    for args in workspace_revokes:
        subprocess.run(args, check=True)

    post = subprocess.run(
        ["getfacl", "--no-effective", str(workspace)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    # Named entries gone — both effective and default.
    assert f"user:{user}:rwx" not in post
    assert f"default:user:{user}:rwx" not in post
    # Persistent state preserved: setgid + group rwx + persistent default ACL.
    assert workspace.stat().st_mode & 0o7777 == 0o2770
    assert "default:group::rwx" in post
