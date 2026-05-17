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
scenario "Reproducible build across two invocations").

Skips with a specific, log-greppable reason when any precondition is
unavailable so a future CI log reader can identify what to fix. Mirrors
the precondition-resolution pattern in ``test_helper_container_userns.py``
and ``test_scaffold_helper_init_sequence.py`` (docker on PATH → real
per-host toml present/parseable → daemon user + subuid/subgid →
machinectl crossing reachable → pinned image present), adapted to the
``golang_alpine`` image that :func:`compile_dispatcher` pins. The
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
    machinectl_cmd,
    minimal_host_config,
    parse_subgid_for_user,
    parse_subuid_for_user,
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

    pin = IMAGE_REGISTRY["golang_alpine"].pinned
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
            f"skipped: golang image {pin} not present in {daemon_user}'s docker "
            f"(stderr: {ins.stderr.strip()!r}); pre-pull with "
            f"`{' '.join(machinectl_cmd(daemon_user, auth))} -- docker pull {pin}`"
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

    build_a = tmp_path / "build-a"
    build_b = tmp_path / "build-b"
    out_a = tmp_path / "dispatch-a"
    out_b = tmp_path / "dispatch-b"

    compile_dispatcher(str(build_a), str(out_a), host_config)
    compile_dispatcher(str(build_b), str(out_b), host_config)

    assert out_a.is_file(), "first compile produced no binary"
    assert out_b.is_file(), "second compile produced no binary"
    assert out_a.stat().st_size > 0
    assert _sha512(out_a) == _sha512(out_b), (
        "compile is not byte-reproducible: the two output binaries differ"
    )
