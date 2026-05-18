"""End-to-end regression test for the ``sandbox init`` → helper-recipe sequence.

Covers two halves of the post-Change-D scaffold-vs-helper boundary
contract (per ``orchestrator-volumes``'s "Scaffold-vs-Helper Boundary"
requirement):

1. Post-init: ``core.scaffold.create_instance_dirs`` MUST NOT pre-create
   any cache/log leaf in the inventory. Pre-D scaffold pre-created the
   leaves as ``dev:dev``; the helper's later ``chown`` failed with EPERM
   because dev's host uid was unmapped in the daemon's userns. Asserting
   "leaves absent post-init" pins the structural fix in place.
2. Post-helper: ``helper_mkdir_chown_dirs`` (the primitive driving
   ``_phase_helper_mkdir_chown_cache_log``) creates each leaf as
   in-container root and chowns to the consumer's host subuid. Asserting
   ``os.stat(leaf).st_uid == consumer_subuid`` proves both halves hold
   under a real userns map.

Runs as a **manual pre-merge gate** today — the project is not yet on
GitHub. CI execution is deferred (see ``openspec/deferred.md`` entries
for GitHub-hosting and self-hosted-runner provisioning); the test
source itself does not change when CI is later wired up.

Skips with a specific, log-greppable reason when any precondition is
unavailable so a future CI log reader can identify what to fix.
Mirrors the precondition-resolution pattern in
``test_helper_container_userns.py``.

Maintenance contract: the ``HELPER_RECIPE_CACHE_LEAVES`` constant must
stay in sync with ``orchestrator-volumes``'s "Cache/Log Leaf Inventory"
requirement (currently the two cache leaves; ``log/core``/``log/admin``
are scaffold-managed and outside this test's scope).
"""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
try:
    from core.helper_container import helper_mkdir_chown_dirs
    from core.host_config import (
        MachinectlAuth,
        host_gid_for_in_container,
        host_id_for_in_container,
        machinectl_cmd,
        parse_subgid_for_user,
        parse_subuid_for_user,
    )
    from core.hydration import IMAGE_REGISTRY
    from core.scaffold import create_instance_dirs
finally:
    sys.path.pop(0)

_TEST_USER_ENV = "SANDBOX_AI_TEST_DAEMON_USER"
_PROBE_TIMEOUT_S = 10
_DISPATCH_BINARY = "/usr/local/libexec/sandbox-ai/dispatch"

# Cache/log leaf inventory subset that this test exercises end-to-end.
# Stays in sync with ``orchestrator-volumes``'s "Cache/Log Leaf Inventory"
# requirement; ``log/core`` and ``log/admin`` are scaffold-managed dirs
# that the helper recipe later asserts ownership over but does not need
# to *create*, so they are out of scope for this regression test.
HELPER_RECIPE_CACHE_LEAVES: tuple[tuple[str, str], ...] = (
    ("cache/core", ".claude"),
)


def _resolve_test_environment() -> tuple[str, MachinectlAuth]:
    """Return ``(daemon_user, auth)`` or call ``pytest.skip`` with a specific reason."""
    override = os.environ.get(_TEST_USER_ENV)
    if override is not None:
        return override, MachinectlAuth.SUDO
    real_toml = Path("~/.sandbox-ai/config/sandbox-ai.toml").expanduser()
    if not real_toml.exists():
        pytest.skip(f"skipped: {real_toml} not present and {_TEST_USER_ENV} unset")
    try:
        with open(real_toml, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        pytest.skip(f"skipped: {real_toml} is malformed TOML: {exc}")
    host_section = raw.get("host", {}) if isinstance(raw, dict) else {}
    user = host_section.get("docker_unprivileged_user")
    if not isinstance(user, str):
        pytest.skip(f"skipped: {real_toml} missing [host].docker_unprivileged_user")
    auth_raw = host_section.get("machinectl_authentication", "sudo")
    try:
        auth = MachinectlAuth(auth_raw)
    except ValueError:
        pytest.skip(f"skipped: invalid [host].machinectl_authentication={auth_raw!r}")
    return user, auth


def _check_preconditions() -> tuple[str, MachinectlAuth]:
    """Verify every precondition the helpers need; skip with a specific reason if any fails."""
    if shutil.which("docker") is None:
        pytest.skip("skipped: docker binary not on PATH")

    daemon_user, auth = _resolve_test_environment()

    try:
        pwd.getpwnam(daemon_user)
    except KeyError:
        pytest.skip(f"skipped: daemon user {daemon_user!r} does not exist on this host")

    if not parse_subuid_for_user(daemon_user):
        pytest.skip(f"skipped: /etc/subuid has no entry for {daemon_user!r}")
    if not parse_subgid_for_user(daemon_user):
        pytest.skip(f"skipped: /etc/subgid has no entry for {daemon_user!r}")

    probe = [*machinectl_cmd(daemon_user, auth), "/bin/echo", "ok"]
    try:
        result = subprocess.run(
            probe,
            capture_output=True,
            timeout=_PROBE_TIMEOUT_S,
            text=True,
        )
    except FileNotFoundError:
        pytest.skip("skipped: machinectl binary not on PATH")
    except subprocess.TimeoutExpired:
        pytest.skip(
            f"skipped: machinectl shell {daemon_user}@.host timed out after "
            f"{_PROBE_TIMEOUT_S}s (sudo password not cached, or polkit rule missing)"
        )
    if result.returncode != 0:
        pytest.skip(
            f"skipped: machinectl shell {daemon_user}@.host exited "
            f"{result.returncode} ({result.stderr.strip()!r})"
        )

    pin = IMAGE_REGISTRY["busybox_musl"].pinned
    inspect = [
        *machinectl_cmd(daemon_user, auth),
        "/bin/bash",
        "-c",
        f"docker image inspect {pin} > /dev/null",
    ]
    try:
        ins = subprocess.run(inspect, capture_output=True, timeout=_PROBE_TIMEOUT_S, text=True)
    except subprocess.TimeoutExpired:
        pytest.skip(f"skipped: docker image inspect {pin} timed out via machinectl")
    if ins.returncode != 0:
        pytest.skip(
            f"skipped: busybox image {pin} not present in {daemon_user}'s docker "
            f"(stderr: {ins.stderr.strip()!r}); pre-pull with "
            f"`{' '.join(machinectl_cmd(daemon_user, auth))} -- docker pull {pin}`"
        )

    # Post-C-001 the helper primitives cross the boundary as
    # `dispatch helper-* …` and exec the root-owned dispatcher binary; it is
    # installed by sister change C-002 (`sandbox setup`), not by this change.
    # Probe it AS IT IS ACTUALLY INVOKED — via the machinectl crossing, where
    # the binary runs as the daemon user — so a clean pre-C-002 skip replaces a
    # loud (and, pre-C-002, expected) SandboxExecutionError.
    dispatch_probe = [
        *machinectl_cmd(daemon_user, auth),
        "/bin/bash",
        "-c",
        f"test -x {_DISPATCH_BINARY}",
    ]
    try:
        dp = subprocess.run(dispatch_probe, capture_output=True, timeout=_PROBE_TIMEOUT_S, text=True)
    except subprocess.TimeoutExpired:
        pytest.skip(f"skipped: dispatcher-present probe (test -x {_DISPATCH_BINARY}) timed out via machinectl")
    if dp.returncode != 0:
        pytest.skip(
            f"skipped: dispatcher binary {_DISPATCH_BINARY} absent or non-executable "
            f"for {daemon_user!r} (C-001 routes helper ops through it; it is installed "
            f"by sister change C-002 — run `sudo sandbox setup` to install)"
        )

    return daemon_user, auth


@pytest.fixture
def cross_boundary_tmpdir() -> Iterator[Path]:
    """Tmpdir visible to both ``dev`` and the daemon user.

    Mirrors ``test_helper_container_userns.py``'s relocated fixture
    (commit ``9a3b426``): pytest's ``tmp_path`` lands under the dev user's
    per-user ``/tmp/pytest-of-dev/`` mount which the daemon's rootless
    docker cannot see (PrivateTmp= split). Use ``<repo>/temp/integration-
    test-tmp/`` instead — already established as project scratch
    (gitignored), shared filesystem view, stragglers visible in
    ``git status`` if cleanup ever fails.
    """
    base = REPO_ROOT / "temp" / "integration-test-tmp"
    base.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(dir=str(base)))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _grant_parent_access(parent: Path, daemon_user: str) -> None:
    """Apply effective + default ACLs on ``parent`` plus traverse on every
    ancestor up to ``Path.home()`` so the daemon user can reach and modify
    files under ``parent``.
    """
    if shutil.which("setfacl") is None:
        pytest.skip("skipped: setfacl not on PATH (required to bridge the dev↔daemon-user fence)")
    home = Path.home()
    cursor = parent.parent
    while True:
        subprocess.run(
            ["setfacl", "-m", f"u:{daemon_user}:--x", str(cursor)],
            check=True,
            capture_output=True,
        )
        if cursor == home or cursor == cursor.parent:
            break
        cursor = cursor.parent
    subprocess.run(
        ["setfacl", "-m", f"u:{daemon_user}:rwx", str(parent)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "setfacl",
            "-d",
            "-m",
            f"u::rwx,g::rwx,o::---,m::rwx,u:{daemon_user}:rwx",
            str(parent),
        ],
        check=True,
        capture_output=True,
    )


def test_post_init_leaves_absent(cross_boundary_tmpdir: Path) -> None:
    """``create_instance_dirs`` does not pre-create cache/log helper-recipe leaves.

    Regression guard for finding 3.E: pre-Change-D the leaves were
    scaffold-created as ``dev:dev``, which made them unmapped in the
    daemon's userns and broke the helper's chown with EPERM.
    """
    # No daemon-user preconditions needed for this assertion — it's a pure
    # filesystem property of ``create_instance_dirs``. Still guard on the
    # cross-boundary tmpdir fixture so CI environments without ``$HOME``
    # writable surface a clean skip rather than a fixture error.
    instance_dir = cross_boundary_tmpdir / "instances" / "regression-target"
    create_instance_dirs(str(instance_dir))

    for parent_rel, leaf_name in HELPER_RECIPE_CACHE_LEAVES:
        leaf = instance_dir / parent_rel / leaf_name
        assert not leaf.exists(), (
            f"post-Change-D scaffold contract violated: helper-recipe leaf "
            f"{leaf} exists post-init (pre-fix tree symptom)"
        )
        # Parent must exist so the helper recipe can land its mkdir there.
        parent = instance_dir / parent_rel
        assert parent.is_dir(), f"scaffold should have created parent {parent}"


def test_post_helper_leaves_consumer_owned(cross_boundary_tmpdir: Path) -> None:
    """``helper_mkdir_chown_dirs`` creates each cache leaf and chowns to the consumer subuid."""
    daemon_user, auth = _check_preconditions()
    instance_dir = cross_boundary_tmpdir / "instances" / "regression-target"
    create_instance_dirs(str(instance_dir))

    target_uid = host_id_for_in_container(1000, daemon_user)
    target_gid = host_gid_for_in_container(1000, daemon_user)

    for parent_rel, leaf_name in HELPER_RECIPE_CACHE_LEAVES:
        parent = instance_dir / parent_rel
        _grant_parent_access(parent, daemon_user)
        helper_mkdir_chown_dirs(
            daemon_user,
            str(parent),
            [leaf_name],
            owner_uid=target_uid,
            owner_gid=target_gid,
            machinectl_auth=auth,
        )

        leaf = parent / leaf_name
        assert leaf.is_dir(), f"helper did not create {leaf}"
        st = os.stat(leaf)
        assert st.st_uid == target_uid, (
            f"{leaf}: st_uid={st.st_uid} != consumer subuid {target_uid}; "
            f"helper chown did not land the host-absolute target"
        )
        assert st.st_gid == target_gid, f"{leaf}: st_gid={st.st_gid} != consumer subgid {target_gid}"
