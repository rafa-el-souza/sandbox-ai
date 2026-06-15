# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared fixtures for the cross-boundary integration tests.

Two pieces of setup were byte-duplicated across
``test_helper_container_userns.py`` and
``test_scaffold_helper_init_sequence.py``: the ``cross_boundary_tmpdir``
fixture and the ``_grant_parent_access`` ACL helper. They are hoisted here
so there is exactly one source (anti-hack rule 4).

The project testing convention forbids importing helper *functions* from a
``conftest.py`` at runtime (``tests.unit.test_conventions`` ::
``test_no_runtime_imports_from_conftest``) — the canonical pattern is
fixtures injected via parameters. So the grant helper is exposed as the
``grant_parent_access`` *fixture* (yielding a ``grant(parent)`` callable),
not an importable function.

Teardown contract (Finding L-hygiene): ``_grant_parent_access`` granted
``setfacl -m u:<daemon>:--x`` on every ancestor of the per-test child up to
``$HOME`` plus ``u:<daemon>:rwx`` on the child itself — onto *persistent*
operator-tree parents (``temp/`` and ``temp/integration-test-tmp``) — and
**never revoked**. ``cross_boundary_tmpdir``'s teardown only
``shutil.rmtree``s the per-test child, so every run leaked a persistent
privilege grant into the operator's tree. ``grant_parent_access`` now
records the exact ``(principal, path)`` pairs each ``grant(...)`` applied
and a finalizer issues the inverse ``setfacl -x u:<daemon> <path>`` for
every recorded grant in reverse order. A revoke that errors is surfaced
(raised after the loop completes), never silently swallowed — the user
mandate is *thorough* cleanup.
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
try:
    from core.host_config import HostConfig, resolve_daemon_owner
    from core.setup_state import read_entry
finally:
    sys.path.pop(0)

_TEST_USER_ENV = "SANDBOX_AI_TEST_DAEMON_USER"


@pytest.fixture
def cross_boundary_tmpdir() -> Iterator[Path]:
    """Tmpdir visible to both ``dev`` and the daemon user.

    pytest's built-in ``tmp_path`` lands under ``/tmp/pytest-of-<user>/``,
    which is on the dev user's per-user mount (``user@.service`` defaults to
    ``PrivateTmp=`` under systemd, isolating ``/tmp`` per user). The rootless
    docker daemon runs as a *different* user with its own ``/tmp`` view, so a
    bind mount from a dev-side ``/tmp/pytest-of-dev/...`` path silently
    resolves to an empty directory inside the helper container — assertions
    against the dev-side path then see nothing.

    Use the repo-rooted ``temp/integration-test-tmp/`` instead. Both privilege
    contexts see the repo on the same filesystem; the ``temp/`` dir is
    already established as the project's scratch area (gitignored). Keeping
    test fixtures inside the repo means stragglers show up in ``git status``
    if cleanup ever fails — a small audit safety net.
    """
    base = REPO_ROOT / "temp" / "integration-test-tmp"
    base.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(dir=str(base)))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _resolve_daemon_user() -> str:
    """Resolve the daemon user the same way ``_check_preconditions`` does.

    Resolution order (identical to each test file's
    ``_resolve_test_environment``): ``SANDBOX_AI_TEST_DAEMON_USER`` env var,
    else the setup-state marker for the current operator (host config is the
    root-owned marker post-C-013, not a user-editable toml). Skips with a
    specific reason if neither is available — so a grant requested before the
    user is resolvable is a clean skip, never a silent un-revoked grant. The
    daemon owner is resolved via ``resolve_daemon_owner`` so it is correct in
    both execution modes (op-rootless → the operator; separate-user → the
    marker's ``docker_unprivileged_user``).
    """
    override = os.environ.get(_TEST_USER_ENV)
    if override is not None:
        return override
    operator = getpass.getuser()
    if read_entry(operator) is None:
        pytest.skip(
            f"skipped: host not set up (no marker); run `sudo sandbox setup` "
            f"and {_TEST_USER_ENV} unset"
        )
    return resolve_daemon_owner(HostConfig.from_marker(operator))


@pytest.fixture
def grant_parent_access() -> Iterator[Callable[[Path], None]]:
    """Yield a ``grant(parent)`` callable; revoke every grant on teardown.

    Each ``grant(parent)`` call applies the production phase ordering: the
    cli's ``_phase_acl_grant`` ancestor walker (``u:<daemon>:--x`` on every
    ancestor of ``parent`` up to ``$HOME``) plus the per-target effective +
    default ACLs (``u:<daemon>:rwx``) before any helper container is
    launched. It **records** the exact ``(path,)`` it ``setfacl -m``'d (one
    record per ancestor cursor + one for ``parent`` itself).

    The finalizer issues the inverse ``setfacl -x u:<daemon_user> <path>``
    for every recorded grant in reverse order (children before ancestors).
    Revoke failures are collected and re-raised after the loop completes so a
    single failing revoke neither aborts the remaining cleanup nor is
    silently swallowed — Finding L-hygiene mandates thorough teardown.
    """
    if shutil.which("setfacl") is None:
        pytest.skip("skipped: setfacl not on PATH (required to bridge the dev↔daemon-user fence)")
    daemon_user = _resolve_daemon_user()
    granted_paths: list[Path] = []

    def grant(parent: Path) -> None:
        home = Path.home()
        cursor = parent.parent
        while True:
            subprocess.run(
                ["setfacl", "-m", f"u:{daemon_user}:--x", str(cursor)],
                check=True,
                capture_output=True,
            )
            granted_paths.append(cursor)
            if cursor == home or cursor == cursor.parent:
                break
            cursor = cursor.parent
        subprocess.run(
            ["setfacl", "-m", f"u:{daemon_user}:rwx", str(parent)],
            check=True,
            capture_output=True,
        )
        granted_paths.append(parent)
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

    try:
        yield grant
    finally:
        # Revoke every recorded grant, reverse order (children before
        # ancestors). The default ACL on `parent` is dropped together with
        # its effective entry by a single `setfacl -x u:<daemon> <parent>`
        # (the -x removes the named user from both the access and the
        # default ACL view). Collect failures; surface them after the loop
        # so one bad revoke does not strand the rest, and is never swallowed.
        errors: list[str] = []
        for path in reversed(granted_paths):
            result = subprocess.run(
                ["setfacl", "-x", f"u:{daemon_user}", str(path)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                errors.append(
                    f"  setfacl -x u:{daemon_user} {path} → "
                    f"rc={result.returncode}: {result.stderr.strip()!r}"
                )
        if errors:
            joined = "\n".join(errors)
            raise RuntimeError(
                f"grant_parent_access teardown failed to revoke {len(errors)} "
                f"daemon ACL grant(s) — a persistent privilege grant may have "
                f"leaked into the operator tree:\n{joined}"
            )
