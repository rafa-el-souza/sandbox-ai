"""Disposable-helper-container primitives.

The helper container is a short-lived busybox-musl container invoked across
the privilege boundary (via ``machinectl shell <docker_user>@.host``) to
perform host-side ownership operations that survive the runsc gofer's
named-ACL stripping. Two primitives are exposed:

- :func:`helper_chown_files` — for read-only single files: copy → chmod →
  chown → atomic rename, idempotent across re-invocations. chmod precedes
  chown because, post-userns-translation, the chown lands the file on a
  non-root in-container uid; the helper baseline omits CAP_FOWNER, so a
  chmod by in-container root on a foreign-owned file would EPERM (see
  fix-helper-container-userns design D7).
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

if TYPE_CHECKING:
    from collections.abc import Iterable
from core.host_config import (
    DEFAULT_PROVISIONING_MODE,
    DockerExecutionMode,
    MachinectlAuth,
    in_container_gid_for_host_gid,
    in_container_uid_for_host_uid,
    minimal_host_config,
)

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
    execution_mode: DockerExecutionMode = DEFAULT_PROVISIONING_MODE,
) -> None:
    """Copy → chmod → chown → atomic rename each file under ``parent``.

    Empty ``files`` is a no-op (no helper container is launched).

    The recipe is idempotent: re-invocation against an already-correct
    target is a no-op modulo timestamps.

    Raises:
        SandboxExecutionError: helper exited non-zero or timed out.
    """
    file_list = list(files)
    if not file_list:
        return
    # Deferred import: ``core.dispatch`` imports ``_hardened_docker_run`` from
    # this module at its top level (it is the single source of the hardened
    # ``docker run`` prefix the helper ops reuse), so a module-level import here
    # would be a circular import. The import is cheap and runs only on the
    # non-empty path.
    from core import dispatch

    in_container_uid = in_container_uid_for_host_uid(owner_uid, host_user)
    in_container_gid = in_container_gid_for_host_gid(owner_gid, host_user)
    mode_octal = format(mode, "04o")
    dispatch.invoke(
        dispatch.Op.HELPER_CHOWN_FILES,
        [
            parent,
            mode_octal,
            str(in_container_uid),
            str(in_container_gid),
            *file_list,
        ],
        minimal_host_config(host_user, machinectl_auth, execution_mode),
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
    execution_mode: DockerExecutionMode = DEFAULT_PROVISIONING_MODE,
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
    # Deferred import — see :func:`helper_chown_files` (circular-import note).
    from core import dispatch

    in_container_uid = in_container_uid_for_host_uid(owner_uid, host_user)
    in_container_gid = in_container_gid_for_host_gid(owner_gid, host_user)
    dispatch.invoke(
        dispatch.Op.HELPER_MKDIR_CHOWN_DIRS,
        [
            parent,
            str(in_container_uid),
            str(in_container_gid),
            *leaf_list,
        ],
        minimal_host_config(host_user, machinectl_auth, execution_mode),
        timeout=timeout,
    )
