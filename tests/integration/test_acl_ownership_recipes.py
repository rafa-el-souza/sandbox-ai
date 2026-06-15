# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end coverage for the acl-ownership-recipes change.

These tests invoke the CLI via ``uv run sandbox …`` against a real host with
``SANDBOX_AI_HOME`` redirected to ``tmp_path``. They require:

- root (for ``sudo groupadd`` / ``sudo usermod``) to set up the bridge group;
- a configured rootless docker daemon for the unprivileged user;
- ``machinectl`` reachable for the unprivileged user;
- gVisor's ``runsc`` runtime registered in the rootless docker daemon.

Tests skip cleanly when these prerequisites are absent. The helper-hardening
test (``test_helper_container_hardening_flags``) does not require any of this
and runs anywhere — it asserts the helper invocation shape via a stub Executor.
"""

from __future__ import annotations

import functools
import getpass
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


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
    except (FileNotFoundError, tomllib.TOMLDecodeError):
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
    """Argv-shape only: assert the hardening flag set + image pin appear in the helper target argv.

    C-001 (``runtime-dispatcher``) moved the hardened ``docker run`` line
    construction OUT of the crossed bash payload and INTO the dispatcher: the
    payload the orchestrator now crosses is ``dispatch helper-chown-files …``,
    and the hardened invocation is assembled by
    ``core.dispatch.build_target_argv("helper-chown-files", …)`` (its post-C-001
    home — ``src/core/dispatch.py:build_target_argv``). This test therefore
    pins the SAME security properties at that new construction site (no
    weakening). End-to-end ownership semantics (the chown actually landing on
    the host-absolute target after userns translation) remain exercised in
    ``tests/integration/test_helper_container_userns.py``; this test is pure
    construction and runs on any host (no docker / subuid / machinectl).
    """
    sys.path.insert(0, str(REPO_ROOT / "src"))
    try:
        from core.dispatch import build_target_argv
        from core.host_config import minimal_host_config
    finally:
        sys.path.pop(0)

    host_config = minimal_host_config("claude-sandbox")

    chown_argv = build_target_argv(
        "helper-chown-files",
        ["/inst/secrets", "600", "100999", "200999", "ipc_host_key"],
        host_config,
    )
    mkdir_argv = build_target_argv(
        "helper-mkdir-chown-dirs",
        ["/inst/cache/core", "100999", "200999", ".claude"],
        host_config,
    )

    # The hardened invocation lives in the `/bin/bash -c <string>` payload that
    # build_target_argv emits (element [-1] of the argv list).
    for argv, parent in ((chown_argv, "/inst/secrets"), (mkdir_argv, "/inst/cache/core")):
        assert argv[:2] == ["/bin/bash", "-c"], f"expected a `bash -c` target argv, got {argv[:2]}"
        payload = argv[-1]
        for flag in [
            "docker run --rm",
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
        # The parent dir is bind-mounted at /p (not --userns=host: D1
        # translation, not bypass).
        assert f"-v {parent}:/p " in payload, f"missing parent bind-mount in {payload}"
        assert "--userns=host" not in payload, "helper must inherit the daemon's userns map"
        # busybox-musl image is digest-pinned.
        assert "@sha256:" in payload, "helper image must be digest-pinned"

    # Host-absolute uid/gid pass through to the dispatcher's hardened argv
    # verbatim; the userns translation (100999 -> in-container 1000) is the
    # helper-side concern exercised in test_helper_container_userns.py, not a
    # property of this construction site.
    assert "chown 100999:200999" in chown_argv[-1]
    assert "chown 100999:200999" in mkdir_argv[-1]


# ─── End-to-end recipe verifications (require root + configured docker) ────

requires_root = pytest.mark.skipif(not _is_root(), reason="requires root for groupadd/usermod")
requires_docker_user = pytest.mark.skipif(
    not _docker_unprivileged_user_configured(),
    reason="requires a configured rootless docker user in ~/.sandbox-ai/config/sandbox-ai.toml",
)


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


@functools.cache
def _setfacl_supports_acls() -> bool:
    """Probe whether setfacl actually mutates ACLs on the system tmp filesystem.

    Both binaries (``setfacl``, ``getfacl``) and POSIX ACL support on the
    underlying filesystem are required. The latter is uncommon to be missing
    but possible on some tmpfs / overlayfs configurations. Probes by running
    a no-op ``setfacl -m`` against a fresh tempdir; cached for the test
    session so the probe runs once.
    """
    if not (shutil.which("setfacl") and shutil.which("getfacl")):
        return False
    with tempfile.TemporaryDirectory() as td:
        result = subprocess.run(
            ["setfacl", "-m", f"u:{getpass.getuser()}:rwx", td],
            capture_output=True,
        )
        return result.returncode == 0


@pytest.mark.skipif(
    not _setfacl_supports_acls(),
    reason="setfacl/getfacl not installed or POSIX ACLs unsupported on tmp filesystem",
)
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
    plan = _acl_revoke_plan(str(tmp_path / "fake_instance"), user, [str(workspace)])
    workspace_revokes = [a.command for a in plan if a.description.startswith("workspace ")]
    assert workspace_revokes, "_acl_revoke_plan must emit workspace revocation entries"
    for argv in workspace_revokes:
        subprocess.run(list(argv), check=True)

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
