"""Disposable-helper-container primitives.

The helper container is a short-lived busybox-musl container invoked across
the privilege boundary (via ``machinectl shell <docker_user>@.host``) to
perform host-side ownership operations that survive the runsc gofer's
named-ACL stripping. Two primitives are exposed:

- :func:`helper_chown_files` — for read-only single files: copy → chown →
  chmod → atomic rename, idempotent across re-invocations.
- :func:`helper_mkdir_chown_dirs` — for cache/log directory leaves:
  ``mkdir -p`` then ``chown`` (no chmod, see Decision 14 in the
  acl-ownership-recipes design).

Every invocation pins the busybox image via ``IMAGE_REGISTRY["busybox_musl"]``
and applies the full hardening flag set per Decision 8 of the
acl-ownership-recipes design: ``--runtime=runc --network=none --read-only
--tmpfs /tmp --user 0:0 --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE
--security-opt no-new-privileges:true``.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from core.executor import Executor

if TYPE_CHECKING:
    from collections.abc import Iterable
from core.host_config import (
    MachinectlAuth,
    in_container_gid_for_host_gid,
    in_container_uid_for_host_uid,
    machinectl_cmd,
)
from core.hydration import IMAGE_REGISTRY

DEFAULT_HELPER_TIMEOUT_S = 30


def _hardened_docker_run(image: str, parent: str, inner_sh: str) -> str:
    """Build the ``docker run …`` command line with the full hardening baseline.

    ``parent`` is bind-mounted at ``/p`` inside the helper. ``inner_sh`` is the
    shell snippet executed under ``sh -c``; it must reference files via
    ``/p/<name>`` and use ``/tmp`` for scratch state.
    """
    return (
        "docker run --rm "
        "--runtime=runc "
        "--network=none "
        "--read-only "
        "--tmpfs /tmp "
        "--user 0:0 "
        "--cap-drop ALL "
        "--cap-add CHOWN "
        "--cap-add DAC_OVERRIDE "
        "--security-opt no-new-privileges:true "
        f"-v {shlex.quote(parent)}:/p "
        f"{shlex.quote(image)} "
        f"sh -c {shlex.quote(inner_sh)}"
    )


def helper_chown_files(
    host_user: str,
    parent: str,
    files: Iterable[str],
    owner_uid: int,
    owner_gid: int,
    mode: int,
    machinectl_auth: MachinectlAuth,
    timeout: float = DEFAULT_HELPER_TIMEOUT_S,
) -> None:
    """Copy → chown → chmod → atomic rename each file under ``parent``.

    Empty ``files`` is a no-op (no helper container is launched).

    The recipe is idempotent: re-invocation against an already-correct
    target is a no-op modulo timestamps.

    Raises:
        SandboxExecutionError: helper exited non-zero or timed out.
    """
    file_list = list(files)
    if not file_list:
        return
    in_container_uid = in_container_uid_for_host_uid(owner_uid, host_user)
    in_container_gid = in_container_gid_for_host_gid(owner_gid, host_user)
    image = IMAGE_REGISTRY["busybox_musl"].pinned
    mode_octal = format(mode, "04o")
    quoted_names = " ".join(shlex.quote(f) for f in file_list)
    inner = (
        f"set -e; for f in {quoted_names}; do "
        f'cp /p/"$f" /tmp/"$f" && '
        f'chmod {mode_octal} /tmp/"$f" && '
        f'chown {in_container_uid}:{in_container_gid} /tmp/"$f" && '
        f'mv /tmp/"$f" /p/"$f"; '
        "done"
    )
    cmd = _hardened_docker_run(image, parent, inner)
    Executor().run(
        [*machinectl_cmd(host_user, machinectl_auth), "/bin/bash", "-c", cmd],
        sentinel=True,
        timeout=timeout,
    )


def helper_mkdir_chown_dirs(
    host_user: str,
    parent: str,
    leaves: Iterable[str],
    owner_uid: int,
    owner_gid: int,
    machinectl_auth: MachinectlAuth,
    timeout: float = DEFAULT_HELPER_TIMEOUT_S,
) -> None:
    """``mkdir -p`` + ``chown`` each leaf under ``parent``.

    No ``chmod`` — chmod on a directory collapses the ACL mask to ``r-x``
    and clamps inherited ``u:dev:rwx`` to ``r-x`` (validated 2026-05-03).
    Empty ``leaves`` is a no-op.

    Raises:
        SandboxExecutionError: helper exited non-zero or timed out.
    """
    leaf_list = list(leaves)
    if not leaf_list:
        return
    in_container_uid = in_container_uid_for_host_uid(owner_uid, host_user)
    in_container_gid = in_container_gid_for_host_gid(owner_gid, host_user)
    image = IMAGE_REGISTRY["busybox_musl"].pinned
    quoted_leaves = " ".join(shlex.quote(leaf) for leaf in leaf_list)
    inner = (
        f'set -e; for d in {quoted_leaves}; do '
        f'mkdir -p /p/"$d" && chown {in_container_uid}:{in_container_gid} /p/"$d"; '
        "done"
    )
    cmd = _hardened_docker_run(image, parent, inner)
    Executor().run(
        [*machinectl_cmd(host_user, machinectl_auth), "/bin/bash", "-c", cmd],
        sentinel=True,
        timeout=timeout,
    )
