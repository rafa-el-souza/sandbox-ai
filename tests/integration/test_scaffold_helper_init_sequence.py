# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
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
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
try:
    from core.exceptions import SandboxExecutionError
    from core.executor import Executor
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
    host_section: dict[str, object] = raw.get("host", {})
    user = host_section.get("docker_unprivileged_user")
    if not isinstance(user, str):
        pytest.skip(f"skipped: {real_toml} missing [host].docker_unprivileged_user")
    auth_raw = host_section.get("machinectl_authentication", "sudo")
    if not isinstance(auth_raw, str):
        pytest.skip(f"skipped: non-string [host].machinectl_authentication={auth_raw!r}")
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

    # The helper ops under test cross via ``machinectl_cmd`` → the dispatcher
    # binary, and ``machinectl shell`` does NOT propagate the inner
    # ``/bin/bash -c`` exit (Finding-I / F-004 silent-footgun class). A
    # precondition that branches on a crossed command's exit MUST recover the
    # REAL inner exit — for a ``machinectl_cmd`` crossing that means the
    # Executor sentinel mechanism (``sentinel=True`` recovers the in-container
    # exit and raises ``SandboxExecutionError`` on non-zero/timeout), NEVER the
    # raw masked ``subprocess.run().returncode``. A masked exit would make
    # these guards (esp. the Finding-H ``test -x dispatch`` guard below) fail
    # OPEN — absent infra read as present → the e2e tests run loud instead of
    # skipping cleanly pre-C-002.
    echo_probe = [*machinectl_cmd(daemon_user), "/bin/bash", "-c", "echo ok"]
    try:
        Executor().run(echo_probe, sentinel=True, timeout=_PROBE_TIMEOUT_S)
    except SandboxExecutionError as exc:
        pytest.skip(
            f"skipped: machinectl shell {daemon_user}@.host crossing not "
            f"usable (sentinel-recovered failure: {exc})"
        )

    pin = IMAGE_REGISTRY["busybox_musl"].pinned
    inspect = [
        *machinectl_cmd(daemon_user),
        "/bin/bash",
        "-c",
        f"docker image inspect {pin} > /dev/null",
    ]
    try:
        Executor().run(inspect, sentinel=True, timeout=_PROBE_TIMEOUT_S)
    except SandboxExecutionError as exc:
        # Sentinel-recovered non-zero inner exit (image absent) or timeout —
        # the REAL result, not the masked machinectl returncode.
        pytest.skip(
            f"skipped: busybox image {pin} not present in {daemon_user}'s docker "
            f"(sentinel-recovered: {exc}); pre-pull with "
            f"`{' '.join(machinectl_cmd(daemon_user))} -- docker pull {pin}`"
        )

    # Post-C-001 the helper primitives cross the boundary as
    # `dispatch helper-* …` and exec the root-owned dispatcher binary; it is
    # installed by sister change C-002 (`sandbox setup`), not by this change.
    # Probe it AS IT IS ACTUALLY INVOKED — via the machinectl crossing, where
    # the binary runs as the daemon user — AND recover the real inner exit via
    # the sentinel (Finding-H must be exit-aware: a masked exit reads an absent
    # binary as present and fails OPEN, so the e2e tests run loud instead of a
    # clean pre-C-002 skip).
    dispatch_probe = [
        *machinectl_cmd(daemon_user),
        "/bin/bash",
        "-c",
        f"test -x {_DISPATCH_BINARY}",
    ]
    try:
        Executor().run(dispatch_probe, sentinel=True, timeout=_PROBE_TIMEOUT_S)
    except SandboxExecutionError as exc:
        pytest.skip(
            f"skipped: dispatcher binary {_DISPATCH_BINARY} absent or non-executable "
            f"for {daemon_user!r} (sentinel-recovered: {exc}; C-001 routes helper ops "
            f"through it; it is installed by sister change C-002 — run "
            f"`sudo sandbox setup` to install)"
        )

    return daemon_user, auth


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


def test_post_helper_leaves_consumer_owned(
    cross_boundary_tmpdir: Path,
    grant_parent_access: Callable[[Path], None],
) -> None:
    """``helper_mkdir_chown_dirs`` creates each cache leaf and chowns to the consumer subuid."""
    daemon_user, auth = _check_preconditions()
    instance_dir = cross_boundary_tmpdir / "instances" / "regression-target"
    create_instance_dirs(str(instance_dir))

    target_uid = host_id_for_in_container(1000, daemon_user)
    target_gid = host_gid_for_in_container(1000, daemon_user)

    for parent_rel, leaf_name in HELPER_RECIPE_CACHE_LEAVES:
        parent = instance_dir / parent_rel
        grant_parent_access(parent)
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
