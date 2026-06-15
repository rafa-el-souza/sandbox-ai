# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Doctor check registry, topological sort, and runner.

Wires the per-topic check functions into a single ordered registry with
``depends_on`` declarations preserved byte-for-byte from the pre-refactor
single-file ``doctor.py``. The registry is rebuilt per call (cheap; no
shared state) so monkeypatching topic-module check functions in tests
takes effect on the next ``build_check_registry`` invocation.
"""

from __future__ import annotations

import functools
from collections import defaultdict, deque

from core.doctor.checks.binary_integrity_posture import check_binary_integrity_posture
from core.doctor.checks.daemon_owner_sudo import check_daemon_owner_sudo
from core.doctor.checks.dispatcher_sha_drift import check_dispatcher_sha_drift
from core.doctor.checks.filesystem import (
    check_acl_support,
    check_ancestor_traverse,
    check_cgroup_v2,
    check_setfacl,
)
from core.doctor.checks.instance_resources import (
    check_host_cpu_capacity,
    check_instance_memory_overcommit,
)
from core.doctor.checks.per_user_tree import (
    check_legacy_cwd_files,
    check_legacy_registry_shape,
    check_legacy_sandboxes_dir_detected,
    check_legacy_workspace_in_user_project_root,
    check_obsolete_host_toml,
    check_per_user_tree_exists,
    check_per_user_tree_mode,
)
from core.doctor.checks.privilege_boundary import (
    check_compose_project_name_collision,
    check_docker_available,
    check_docker_rootless,
    check_host_uds,
    check_machinectl,
    check_machinectl_reachable,
    check_runsc_registered,
    check_runsc_runtimeargs,
    check_sudo,
    check_systemd_machined,
    check_tlog,
    check_user_exists,
)
from core.doctor.checks.repo_integrity import check_state_dir_writable, check_tooling_plane
from core.doctor.checks.runsc_pinned_match import check_runsc_pinned_match
from core.doctor.checks.setup_invariants import check_setup_invariants
from core.doctor.checks.supply_chain import check_image_digests
from core.doctor.checks.workspace_bridge import (
    check_backups_disk_pressure,
    check_backups_partial_dirs_present,
    check_dev_in_workspace_bridge_group,
    check_dev_umask_workspace_friendly,
    check_pre_existing_instance_layout,
    check_secrets_hydrated_restrictively,
    check_subuid_resolver_works,
    check_workspace_bridge_group_exists,
    check_workspace_home_single_filesystem,
    check_workspace_path_in_walker_boundary,
)
from core.doctor.types import Check, CheckResult
from core.host_config import DEFAULT_PROVISIONING_MODE, DockerExecutionMode

# The crossing-only checks that have no meaning in ``operator-rootless`` mode
# (there is no machinectl crossing / dedicated daemon user / sudoers rule to
# audit). These are gated to ``separate-user`` via ``Check.applies_in`` so the
# runner emits an explicit mode-skip rather than a false green (design D2).
_SEPARATE_USER_ONLY = frozenset({DockerExecutionMode.SEPARATE_USER})

# The sudoer-daemon-owner WARN has meaning ONLY in ``operator-rootless`` (where
# the daemon owner is the invoking operator, who may be a sudoer); in
# ``separate-user`` the owner is the dedicated dead-end user covered by the
# no-sudo invariant folded into ``setup_invariants`` (design D4). Gated to
# ``operator-rootless`` so the runner mode-skips it in ``separate-user``.
_OPERATOR_ROOTLESS_ONLY = frozenset({DockerExecutionMode.OPERATOR_ROOTLESS})


def build_check_registry(
    mode: DockerExecutionMode = DEFAULT_PROVISIONING_MODE,
) -> list[Check]:
    """Build the doctor check registry with execution-mode-aware checks.

    ``mode`` (the marker-resolved :class:`DockerExecutionMode`) is partial-bound
    into the closure of every check that builds ``minimal_host_config(...)`` so
    its ``dispatch.probe(...)`` routes locally in ``operator-rootless`` (C-003,
    no dispatch change needed — just thread the mode). The five crossing-only
    checks (``machinectl_reachable``, ``systemd_machined``, ``user_exists``,
    ``dispatcher_sha_drift``) carry ``applies_in=separate-user`` so the runner
    mode-skips them in ``operator-rootless``; ``setup_invariants`` stays
    both-mode and branches internally.
    """
    machinectl_reachable_deps = ["sudo", "machinectl", "user_exists", "systemd_machined"]

    chain1: list[Check] = [
        Check(
            id="sudo",
            name="sudo binary",
            category="Privilege Boundary",
            depends_on=[],
            run=check_sudo,
            remediation="",
        ),
        Check(
            id="machinectl",
            name="machinectl binary",
            category="Privilege Boundary",
            depends_on=[],
            run=check_machinectl,
            remediation="",
        ),
        Check(
            id="tlog",
            name="tlog binary",
            category="Privilege Boundary",
            depends_on=[],
            run=check_tlog,
            remediation="",
        ),
        Check(
            id="user_exists",
            name="unprivileged user",
            category="Privilege Boundary",
            depends_on=[],
            run=check_user_exists,
            remediation="",
            applies_in=_SEPARATE_USER_ONLY,
        ),
        Check(
            id="systemd_machined",
            name="systemd-machined",
            category="Privilege Boundary",
            depends_on=["machinectl"],
            run=check_systemd_machined,
            remediation="",
            applies_in=_SEPARATE_USER_ONLY,
        ),
        Check(
            id="machinectl_reachable",
            name="machinectl reachable",
            category="Privilege Boundary",
            depends_on=machinectl_reachable_deps,
            run=functools.partial(check_machinectl_reachable, mode=mode),
            remediation="",
            applies_in=_SEPARATE_USER_ONLY,
        ),
        Check(
            id="docker_available",
            name="Docker available",
            category="Privilege Boundary",
            depends_on=["machinectl_reachable"],
            run=functools.partial(check_docker_available, mode=mode),
            remediation="",
        ),
        Check(
            id="docker_rootless",
            name="Docker rootless",
            category="Privilege Boundary",
            depends_on=["docker_available"],
            run=functools.partial(check_docker_rootless, mode=mode),
            remediation="",
        ),
        Check(
            id="runsc",
            name="gVisor runsc",
            category="Privilege Boundary",
            depends_on=["docker_available"],
            run=functools.partial(check_runsc_registered, mode=mode),
            remediation="",
        ),
        Check(
            id="runsc_runtimeargs",
            name="runsc runtimeArgs",
            category="Privilege Boundary",
            depends_on=["runsc"],
            run=functools.partial(check_runsc_runtimeargs, mode=mode),
            remediation="",
        ),
        Check(
            id="host_uds",
            name="--host-uds=none",
            category="Privilege Boundary",
            depends_on=["runsc"],
            run=functools.partial(check_host_uds, mode=mode),
            remediation="",
        ),
        Check(
            id="compose_project_name_collision",
            name="compose project name collision",
            category="Privilege Boundary",
            depends_on=["machinectl_reachable"],
            run=functools.partial(check_compose_project_name_collision, mode=mode),
            remediation="",
        ),
    ]

    return [
        *chain1,
        # Chain 2: filesystem
        Check(
            id="setfacl",
            name="setfacl binary",
            category="Filesystem",
            depends_on=[],
            run=check_setfacl,
            remediation="",
        ),
        Check(
            id="acl_support",
            name="ACL support",
            category="Filesystem",
            depends_on=["setfacl"],
            run=check_acl_support,
            remediation="",
        ),
        # cgroup-v2 is a genuine runtime prerequisite in BOTH modes (rootless
        # dockerd needs the unified hierarchy). C-004 gated setup's L1 cgroup-v2
        # verify OUT of op-rootless setup, so doctor MUST still surface it here
        # — hence the default both-mode ``applies_in`` (NOT separate-user-only).
        Check(
            id="cgroup_v2",
            name="cgroup v2",
            category="Filesystem",
            depends_on=[],
            run=check_cgroup_v2,
            remediation="",
        ),
        Check(
            id="ancestor_traverse",
            name="ancestor traverse",
            category="Filesystem",
            depends_on=["acl_support"],
            run=functools.partial(check_ancestor_traverse, mode=mode),
            remediation="",
        ),
        # Chain 3: repo integrity
        Check(
            id="tooling_plane",
            name="tooling plane",
            category="Repo Integrity",
            depends_on=[],
            run=check_tooling_plane,
            remediation="",
        ),
        Check(
            id="state_dir",
            name="state dir writable",
            category="Repo Integrity",
            depends_on=[],
            run=check_state_dir_writable,
            remediation="",
        ),
        # Chain 4: supply chain
        Check(
            id="image_digests",
            name="image digests",
            category="Supply Chain",
            depends_on=["docker_available"],
            run=functools.partial(check_image_digests, mode=mode),
            remediation="",
        ),
        # Chain 5: per-user tree
        Check(
            id="per_user_tree_exists",
            name="per-user tree exists",
            category="Per-User Tree",
            depends_on=[],
            run=check_per_user_tree_exists,
            remediation="",
        ),
        Check(
            id="per_user_tree_mode",
            name="per-user tree mode",
            category="Per-User Tree",
            depends_on=["per_user_tree_exists"],
            run=check_per_user_tree_mode,
            remediation="",
        ),
        Check(
            id="legacy_cwd_files",
            name="legacy CWD files",
            category="Per-User Tree",
            depends_on=[],
            run=check_legacy_cwd_files,
            remediation="",
        ),
        Check(
            id="obsolete_host_toml",
            name="obsolete host toml",
            category="Per-User Tree",
            depends_on=[],
            run=check_obsolete_host_toml,
            remediation="",
        ),
        # Chain 6: workspace bridge group + helper-recipe prereqs
        Check(
            id="workspace_bridge_group_exists",
            name="workspace bridge group",
            category="Workspace Bridge",
            depends_on=[],
            run=functools.partial(check_workspace_bridge_group_exists, mode=mode),
            remediation="",
        ),
        Check(
            id="dev_in_workspace_bridge_group",
            name="operator in workspace bridge group",
            category="Workspace Bridge",
            depends_on=["workspace_bridge_group_exists"],
            run=functools.partial(check_dev_in_workspace_bridge_group, mode=mode),
            remediation="",
        ),
        Check(
            id="subuid_resolver_works",
            name="subuid resolver",
            category="Workspace Bridge",
            depends_on=[],
            run=check_subuid_resolver_works,
            remediation="",
        ),
        Check(
            id="secrets_hydrated_restrictively",
            name="secrets hydrated restrictively",
            category="Workspace Bridge",
            depends_on=[],
            run=check_secrets_hydrated_restrictively,
            remediation="",
        ),
        Check(
            id="pre_existing_instance_layout",
            name="pre-existing instance layout",
            category="Workspace Bridge",
            depends_on=["subuid_resolver_works"],
            run=check_pre_existing_instance_layout,
            remediation="",
        ),
        Check(
            id="backups_disk_pressure",
            name="backups disk pressure",
            category="Workspace Bridge",
            depends_on=[],
            run=check_backups_disk_pressure,
            remediation="",
        ),
        Check(
            id="backups_partial_dirs_present",
            name="backups partial dirs",
            category="Workspace Bridge",
            depends_on=[],
            run=check_backups_partial_dirs_present,
            remediation="",
        ),
        Check(
            id="dev_umask_workspace_friendly",
            name="operator umask workspace-friendly",
            category="Workspace Bridge",
            depends_on=[],
            run=check_dev_umask_workspace_friendly,
            remediation="",
        ),
        Check(
            id="workspace_path_in_walker_boundary",
            name="workspace path in walker boundary",
            category="Workspace Bridge",
            depends_on=[],
            run=check_workspace_path_in_walker_boundary,
            remediation="",
        ),
        Check(
            id="workspace_home_single_filesystem",
            name="workspace home single filesystem",
            category="Workspace Bridge",
            depends_on=[],
            run=check_workspace_home_single_filesystem,
            remediation="",
        ),
        Check(
            id="legacy_sandboxes_dir_detected",
            name="legacy sandboxes dir detected",
            category="Per-User Tree",
            depends_on=[],
            run=check_legacy_sandboxes_dir_detected,
            remediation="",
        ),
        Check(
            id="legacy_workspace_in_user_project_root",
            name="legacy user_project_root field",
            category="Per-User Tree",
            depends_on=[],
            run=check_legacy_workspace_in_user_project_root,
            remediation="",
        ),
        Check(
            id="legacy_registry_shape",
            name="legacy registry shape",
            category="Per-User Tree",
            depends_on=[],
            run=check_legacy_registry_shape,
            remediation="",
        ),
        # Chain 6.5: per-instance host-resource capacity (advisory WARN-only).
        # Both-mode + dependency-free: each scans the registry and reads the
        # rendered compose.yml, which is install-mode independent.
        Check(
            id="host_cpu_capacity",
            name="host CPU capacity",
            category="Instance Resources",
            depends_on=[],
            run=check_host_cpu_capacity,
            remediation="",
        ),
        Check(
            id="instance_memory_overcommit",
            name="instance memory over-commit",
            category="Instance Resources",
            depends_on=[],
            run=check_instance_memory_overcommit,
            remediation="",
        ),
        # Chain 7: setup integrity (sandbox-setup Group 9).
        # Independent filesystem/local probes — no doctor-internal deps
        # (spec "runsc Pinned Match Check" / "Dispatcher Sha Drift Check" /
        # "Binary Integrity Posture Check" / "Setup Invariants Check":
        # dependencies are filesystem readability only). These checks are
        # partial-bound with ``mode`` so the host_config they build carries the
        # configured execution mode.
        Check(
            id="runsc_pinned_match",
            name="runsc pinned match",
            category="Setup Integrity",
            depends_on=[],
            run=functools.partial(check_runsc_pinned_match, mode=mode),
            remediation="",
        ),
        Check(
            id="dispatcher_sha_drift",
            name="dispatcher sha drift",
            category="Setup Integrity",
            depends_on=[],
            run=check_dispatcher_sha_drift,
            remediation="",
            applies_in=_SEPARATE_USER_ONLY,
        ),
        Check(
            id="binary_integrity_posture",
            name="binary integrity posture",
            category="Setup Integrity",
            depends_on=[],
            run=check_binary_integrity_posture,
            remediation="",
        ),
        Check(
            id="setup_invariants",
            name="setup invariants",
            category="Setup Integrity",
            depends_on=[],
            run=functools.partial(check_setup_invariants, mode=mode),
            remediation="",
        ),
        Check(
            id="daemon_owner_sudo",
            name="daemon owner sudo",
            category="Setup Integrity",
            depends_on=[],
            run=functools.partial(check_daemon_owner_sudo, mode=mode),
            remediation="",
            applies_in=_OPERATOR_ROOTLESS_ONLY,
        ),
    ]


def topological_sort(checks: list[Check]) -> list[Check]:
    """Topologically sort checks respecting depends_on declarations."""
    id_to_check = {c.id: c for c in checks}
    in_degree: dict[str, int] = {c.id: 0 for c in checks}
    adjacency: dict[str, list[str]] = defaultdict(list)

    for c in checks:
        for dep in c.depends_on:
            if dep in id_to_check:
                adjacency[dep].append(c.id)
                in_degree[c.id] += 1

    queue: deque[str] = deque(cid for cid, deg in in_degree.items() if deg == 0)
    sorted_ids: list[str] = []

    while queue:
        current = queue.popleft()
        sorted_ids.append(current)
        for neighbor in adjacency[current]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    return [id_to_check[cid] for cid in sorted_ids]


def run_checks(
    checks: list[Check],
    user: str,
    distro: str | None,
    mode: DockerExecutionMode = DEFAULT_PROVISIONING_MODE,
) -> list[CheckResult]:
    """Execute checks in topological order with mode gating + cascading skip.

    A check whose ``applies_in`` EXCLUDES ``mode`` is mode-skipped BEFORE it
    runs, with a mode-based detail (``"skipped (operator-rootless)"``) — never
    PASS, so a crossing-only check never reads as a false green (design D2). A
    mode-skip is "not applicable", NOT a failure: it does NOT cascade-skip its
    dependents (mirrors ``Phase.applies_in``). The pre-existing
    dependency-cascade skip (a dep that genuinely failed or was skipped for
    dependency reasons) is preserved and distinct.
    """
    ordered = topological_sort(checks)
    results: dict[str, CheckResult] = {}
    mode_skipped: set[str] = set()
    output: list[CheckResult] = []

    for check in ordered:
        if mode not in check.applies_in:
            result = CheckResult(
                status="skip",
                name=check.name,
                detail=f"skipped ({mode.value})",
            )
            mode_skipped.add(check.id)
            results[check.id] = result
            output.append(result)
            continue

        # A dependency blocks only if it genuinely failed / dependency-skipped;
        # a mode-skipped dependency is "not applicable" and does NOT block.
        failed_deps = [
            dep
            for dep in check.depends_on
            if dep in results and dep not in mode_skipped and results[dep].status in ("fail", "skip")
        ]

        if failed_deps:
            dep_names = ", ".join(failed_deps)
            result = CheckResult(
                status="skip",
                name=check.name,
                detail=f"skipped (requires: {dep_names})",
            )
        else:
            result = check.run(user, distro)

        results[check.id] = result
        output.append(result)

    return output


def run_check_subset(
    categories: list[str],
    user: str,
    distro: str | None,
    *,
    exclude_ids: set[str] | None = None,
    mode: DockerExecutionMode = DEFAULT_PROVISIONING_MODE,
) -> list[CheckResult]:
    """Execute a filtered subset of doctor checks by category.

    Filters ``build_check_registry()`` by ``Check.category``, validates the
    cross-chain invariant (all ``depends_on`` references must resolve within
    the subset), then delegates to ``run_checks``.

    Args:
        categories: Category names to include.
        user: Unprivileged user to check.
        distro: Linux distribution name or None.
        exclude_ids: Optional set of check IDs to exclude from the subset.
            Excluded checks are removed *before* the cross-chain invariant
            validation. Checks that ``depends_on`` an excluded ID will be
            auto-skipped by the dependency engine.
        mode: Active :class:`DockerExecutionMode` (threaded into each check's
            ``minimal_host_config(...)`` and used by the runner for mode-skip
            gating).

    Raises:
        ValueError: If any ``depends_on`` reference in the filtered subset
            points to a check outside the subset.
    """
    if not categories:
        return []

    registry = build_check_registry(mode)
    category_set = set(categories)
    excluded = exclude_ids or set()
    subset = [c for c in registry if c.category in category_set and c.id not in excluded]

    # Assert cross-chain invariant: every depends_on must resolve internally
    subset_ids = {c.id for c in subset}
    for check in subset:
        for dep in check.depends_on:
            if dep not in subset_ids and dep not in excluded:
                raise ValueError(
                    f"Check '{check.id}' depends on '{dep}' which is outside "
                    f"the subset (categories: {categories}). Cross-chain "
                    f"dependencies are not supported in subset execution."
                )

    return run_checks(subset, user, distro, mode)


__all__ = [
    "build_check_registry",
    "run_check_subset",
    "run_checks",
    "topological_sort",
]
