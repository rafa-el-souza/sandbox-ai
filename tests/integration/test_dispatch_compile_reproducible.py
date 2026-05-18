"""Integration smoke: the offline compile recipe is byte-reproducible.

Marked ``@pytest.mark.integration`` — NOT collected by the default
``make test`` / ``make coverage`` gate (``pytest.testpaths = ["tests/unit"]``).
Runs only via ``make test-integration`` on a real-docker host with the
sandbox-ai privilege boundary configured (a real ``sandbox-ai.toml`` present
at ``~/.sandbox-ai/config/sandbox-ai.toml``, read directly — see below).

It invokes :func:`core.dispatch.compile_dispatcher` twice into two distinct
output paths against identical source + the same digest-pinned
``golang:1.23-alpine`` image and asserts the two binaries' sha512 match
(design D3 reproducibility; spec "Offline Reproducible Compile Recipe"
scenario "Reproducible build across two invocations"). ``compile_dispatcher``
takes no build dir — it embeds the source in the crossed payload, builds in
an ephemeral per-call ``mktemp -d`` under the lingering daemon user's
per-user runtime dir ``/run/user/$(id -u)`` — reachable under the
PAM-skipping ``pipe_cmd`` crossing where ``$XDG_RUNTIME_DIR`` is unset
(tmpfs, daemon-user-owned, ZERO operator-tree ACLs, self-cleaning via
``trap … EXIT``; linger is an architectural prerequisite — sister-change
``sandbox-setup`` L5), and returns the binary over captured stdout — so this test
has no cross-boundary build dir / ACL grant to tear down (reproducibility is
location-neutral: the container always mounts at a fixed path + ``-trimpath``).

Skips with a specific, log-greppable reason when any precondition is
unavailable so a future CI log reader can identify what to fix. Mirrors
the precondition-resolution pattern in ``test_helper_container_userns.py``
and ``test_scaffold_helper_init_sequence.py`` (docker on PATH → real
per-host toml present/parseable → daemon user + subuid/subgid →
boundary crossing reachable → pinned image present), adapted to the
``golang_alpine`` image that :func:`compile_dispatcher` pins. Because
``compile_dispatcher`` now crosses via ``pipe_cmd`` (binary-frame
transport, no PTY), these precondition probes ALSO cross via ``pipe_cmd``
— it propagates the inner exit, so ``subprocess.run(...).returncode`` is
the REAL exit and the image-absent / unreachable cases skip correctly
instead of failing OPEN behind the masked ``machinectl`` returncode. The
``HostConfig`` it needs is built from the real-toml-resolved
``(daemon_user, auth)`` via :func:`core.host_config.minimal_host_config`
(``compile_dispatcher`` reads only those two boundary fields) — NOT via
``HostConfig.from_toml``, which the integration harness's
``SANDBOX_AI_HOME``→tmp redirect makes permanently unresolvable.
"""

from __future__ import annotations

import hashlib
import os
import pwd
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
from core.dispatch import compile_dispatcher
from core.host_config import (
    MachinectlAuth,
    minimal_host_config,
    parse_subgid_for_user,
    parse_subuid_for_user,
    pipe_cmd,
)
from core.hydration import IMAGE_REGISTRY

pytestmark = pytest.mark.integration

_TEST_USER_ENV = "SANDBOX_AI_TEST_DAEMON_USER"
_PROBE_TIMEOUT_S = 10


def _resolve_test_environment() -> tuple[str, MachinectlAuth]:
    """Return ``(daemon_user, auth)`` or call ``pytest.skip`` with a specific reason.

    Resolution order: ``SANDBOX_AI_TEST_DAEMON_USER`` env var (auth defaults
    to SUDO); otherwise parse ``~/.sandbox-ai/config/sandbox-ai.toml``.
    """
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
    """Verify every precondition the compile recipe needs; skip with a specific reason if any fails."""
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

    # A precondition that branches on a crossed command's exit MUST recover
    # the REAL inner exit, never the raw masked ``machinectl shell`` returncode
    # (Finding-I / F-004 silent-footgun class — a masked exit makes the guard
    # fail OPEN). ``compile_dispatcher`` now crosses via ``pipe_cmd``
    # (``systemd-run --pipe``), which PROPAGATES the inner ``/bin/bash -c``
    # exit, so probing through the SAME ``pipe_cmd`` crossing means
    # ``subprocess.run(...).returncode`` is the real exit — no sentinel needed.
    probe = [*pipe_cmd(daemon_user), "/bin/echo", "ok"]
    try:
        result = subprocess.run(
            probe,
            capture_output=True,
            timeout=_PROBE_TIMEOUT_S,
            text=True,
        )
    except FileNotFoundError:
        pytest.skip("skipped: systemd-run binary not on PATH")
    except subprocess.TimeoutExpired:
        pytest.skip(
            f"skipped: pipe_cmd crossing into {daemon_user} timed out after "
            f"{_PROBE_TIMEOUT_S}s (systemd-run manage-units polkit rule missing)"
        )
    if result.returncode != 0:
        pytest.skip(
            f"skipped: pipe_cmd crossing into {daemon_user} exited "
            f"{result.returncode} ({result.stderr.strip()!r})"
        )

    pin = IMAGE_REGISTRY["golang_alpine"].pinned
    inspect = [
        *pipe_cmd(daemon_user),
        "/bin/bash",
        "-c",
        f"docker image inspect {pin} > /dev/null",
    ]
    try:
        ins = subprocess.run(inspect, capture_output=True, timeout=_PROBE_TIMEOUT_S, text=True)
    except subprocess.TimeoutExpired:
        pytest.skip(f"skipped: docker image inspect {pin} timed out via pipe_cmd")
    # ``pipe_cmd`` propagates the inner exit → an absent image yields a REAL
    # non-zero returncode here, so the image-absent case SKIPS correctly
    # instead of failing OPEN and triggering a multi-MB pull mid-test.
    if ins.returncode != 0:
        pytest.skip(
            f"skipped: golang image {pin} not present in {daemon_user}'s docker "
            f"(stderr: {ins.stderr.strip()!r}); pre-pull with "
            f"`{' '.join(pipe_cmd(daemon_user))} /bin/bash -c 'docker pull {pin}'`"
        )

    return daemon_user, auth


def _sha512(path: Path) -> str:
    return hashlib.sha512(path.read_bytes()).hexdigest()


def test_compile_dispatcher_is_byte_reproducible(tmp_path: Path) -> None:
    """Two compiles of identical source + pinned image are sha512-identical."""
    daemon_user, auth = _check_preconditions()
    # ``_check_preconditions`` resolved ``(daemon_user, auth)`` from the REAL
    # per-host ``~/.sandbox-ai/config/sandbox-ai.toml`` via
    # ``_resolve_test_environment`` (the sibling idiom in
    # ``test_helper_container_userns.py`` — read the real path directly, do
    # NOT call ``HostConfig.from_toml()``, which the integration harness's
    # ``SANDBOX_AI_HOME``→tmp redirect makes permanently unresolvable to an
    # empty dir → permanent skip). ``compile_dispatcher`` reads only the two
    # boundary fields, so build the minimal HostConfig from the resolved
    # pair — the same construction ``minimal_host_config`` exists for.
    host_config = minimal_host_config(daemon_user, auth)

    # No build dirs: ``compile_dispatcher`` embeds the source in the crossed
    # payload and the binary returns over captured stdout. The actual build
    # dir is an ephemeral per-call ``mktemp -d`` under the lingering daemon
    # user's per-user runtime dir ``/run/user/$(id -u)`` — reachable under the
    # PAM-skipping ``pipe_cmd`` crossing where ``$XDG_RUNTIME_DIR`` is unset
    # (tmpfs, daemon-user-owned, ZERO operator-tree ACLs) that self-cleans via
    # ``trap … EXIT`` on success AND failure — so this test has NO ACL to
    # revoke and no cross-boundary dir to tear down.
    # ``tmp_path`` holds only the two output binaries (pytest auto-cleans it).
    out_a = tmp_path / "dispatch-a"
    out_b = tmp_path / "dispatch-b"

    try:
        compile_dispatcher(str(out_a), host_config)
        compile_dispatcher(str(out_b), host_config)

        assert out_a.is_file(), "first compile produced no binary"
        assert out_b.is_file(), "second compile produced no binary"
        assert out_a.stat().st_size > 0
        assert _sha512(out_a) == _sha512(out_b), (
            "compile is not byte-reproducible: the two output binaries differ"
        )
    finally:
        # Belt-and-braces: drop the two operator-side output binaries even if
        # an assertion failed. There are no ACLs / cross-boundary dirs to
        # revoke — the ephemeral build dir is claude-sandbox-side and self-
        # cleaning. ``tmp_path`` itself is pytest-managed.
        out_a.unlink(missing_ok=True)
        out_b.unlink(missing_ok=True)
