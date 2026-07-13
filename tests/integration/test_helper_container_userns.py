# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end ownership integration test for the helper-container primitives.

Per fix-helper-container-userns design D4 + the helper-container capability's
mocking-policy spec: this is the **only** test that invokes a real helper
container against a real rootless userns and asserts the resulting on-disk
``st_uid``/``st_gid`` (via ``os.stat``) match the host-absolute target uid/gid
passed to the helper. Argv-shape coverage lives in the unit tests; ownership
semantics live here.

Runs as a **manual pre-merge gate** today — the project is not yet on GitHub.
CI execution is deferred (see ``openspec/deferred.md`` entries for GitHub-
hosting and self-hosted-runner provisioning); the test source itself does not
change when CI is later wired up.

Skips with a specific, log-greppable reason when any precondition is
unavailable so a future CI log reader can identify what to fix.
"""

from __future__ import annotations

import getpass
import os
import pwd
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
try:
    from core.exceptions import SandboxExecutionError
    from core.executor import Executor
    from core.helper_container import helper_chown_files, helper_mkdir_chown_dirs
    from core.host_config import (
        HostConfig,
        host_gid_for_in_container,
        host_id_for_in_container,
        machinectl_cmd,
        parse_subgid_for_user,
        parse_subuid_for_user,
        resolve_daemon_owner,
    )
    from core.hydration import IMAGE_REGISTRY
    from core.setup_state import read_entry
finally:
    sys.path.pop(0)

_TEST_USER_ENV = "SANDBOX_AI_TEST_DAEMON_USER"
_PROBE_TIMEOUT_S = 10
DISPATCH_BINARY = "/usr/local/libexec/sandbox-ai/dispatch"


def _resolve_test_environment() -> str:
    """Return ``daemon_user`` or call ``pytest.skip`` with a specific reason.

    Resolution order: ``SANDBOX_AI_TEST_DAEMON_USER`` env var; otherwise the
    setup-state marker for the current operator (host config is the root-owned
    marker post-C-013). Resolves the daemon owner via ``resolve_daemon_owner``
    (correct in both execution modes).
    """
    override = os.environ.get(_TEST_USER_ENV)
    if override is not None:
        return override
    operator = getpass.getuser()
    if read_entry(operator) is None:
        pytest.skip("skipped: host not set up (no marker); run `sudo sandbox setup`")
    return resolve_daemon_owner(HostConfig.from_marker(operator))


def _check_preconditions() -> str:
    """Verify every precondition the helpers need; skip with a specific reason if any fails."""
    if shutil.which("docker") is None:
        pytest.skip("skipped: docker binary not on PATH")

    daemon_user = _resolve_test_environment()

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
        f"test -x {DISPATCH_BINARY}",
    ]
    try:
        Executor().run(dispatch_probe, sentinel=True, timeout=_PROBE_TIMEOUT_S)
    except SandboxExecutionError as exc:
        pytest.skip(
            f"skipped: dispatcher binary {DISPATCH_BINARY} absent or non-executable "
            f"for {daemon_user!r} (sentinel-recovered: {exc}; C-001 routes helper ops "
            f"through it; it is installed by sister change C-002 — run "
            f"`sudo sandbox setup` to install)"
        )

    return daemon_user


def test_helper_mkdir_chown_dirs_lands_host_absolute_ownership(
    cross_boundary_tmpdir: Path,
    grant_parent_access: Callable[[Path], None],
) -> None:
    daemon_user = _check_preconditions()
    tmp_path = cross_boundary_tmpdir
    grant_parent_access(tmp_path)

    target_uid = host_id_for_in_container(1000, daemon_user)
    target_gid = host_gid_for_in_container(1000, daemon_user)

    helper_mkdir_chown_dirs(
        daemon_user,
        str(tmp_path),
        ["leaf"],
        owner_uid=target_uid,
        owner_gid=target_gid,
    )

    leaf = tmp_path / "leaf"
    assert leaf.is_dir(), f"helper did not create {leaf}"
    st = os.stat(leaf)
    assert st.st_uid == target_uid, (
        f"st_uid={st.st_uid} != target {target_uid}; "
        f"in-container translation did not land on the host-absolute target"
    )
    assert st.st_gid == target_gid, f"st_gid={st.st_gid} != target {target_gid}"


def test_helper_chown_files_lands_host_absolute_ownership_and_mode(
    cross_boundary_tmpdir: Path,
    grant_parent_access: Callable[[Path], None],
) -> None:
    daemon_user = _check_preconditions()
    tmp_path = cross_boundary_tmpdir
    grant_parent_access(tmp_path)

    target_uid = host_id_for_in_container(1000, daemon_user)
    target_gid = host_gid_for_in_container(1000, daemon_user)
    target_mode = 0o640

    src = tmp_path / "ipc_host_key"
    src.write_text("test payload\n")

    helper_chown_files(
        daemon_user,
        str(tmp_path),
        ["ipc_host_key"],
        owner_uid=target_uid,
        owner_gid=target_gid,
        mode=target_mode,
    )

    st = os.stat(src)
    assert st.st_uid == target_uid, (
        f"st_uid={st.st_uid} != target {target_uid}; "
        f"in-container translation did not land on the host-absolute target"
    )
    assert st.st_gid == target_gid, f"st_gid={st.st_gid} != target {target_gid}"
    assert st.st_mode & 0o777 == target_mode, (
        f"st_mode bits {oct(st.st_mode & 0o777)} != target {oct(target_mode)}"
    )
