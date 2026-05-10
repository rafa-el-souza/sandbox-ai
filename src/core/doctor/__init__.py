"""Doctor module: host readiness diagnostics for sandbox operation.

Provides 16 diagnostic checks across 4 independent chains:
- Chain 1 (privilege boundary, 10 checks): sudo -> machinectl -> user -> machined
  -> reachable -> docker -> rootless -> runsc -> runsc_runtimeargs -> host_uds
- Chain 2 (filesystem, 3 checks): setfacl → ACL support → ancestor traverse
- Chain 3 (repo integrity, 2 checks): tooling plane, state dir (independent)
- Chain 4 (supply chain, 1 check): image_digests (depends on docker_available)
"""

from __future__ import annotations

import functools
from collections import defaultdict, deque
from typing import TYPE_CHECKING

from core.doctor.checks.filesystem import _ACL_PROBE_FAILURES as _ACL_PROBE_FAILURES
from core.doctor.checks.filesystem import _has_acl_exec as _has_acl_exec
from core.doctor.checks.filesystem import check_acl_support as check_acl_support
from core.doctor.checks.filesystem import check_ancestor_traverse as check_ancestor_traverse
from core.doctor.checks.filesystem import check_setfacl as check_setfacl
from core.doctor.checks.per_user_tree import check_legacy_cwd_files as check_legacy_cwd_files
from core.doctor.checks.per_user_tree import check_legacy_registry_shape as check_legacy_registry_shape
from core.doctor.checks.per_user_tree import (
    check_legacy_sandboxes_dir_detected as check_legacy_sandboxes_dir_detected,
)
from core.doctor.checks.per_user_tree import (
    check_legacy_workspace_in_user_project_root as check_legacy_workspace_in_user_project_root,
)
from core.doctor.checks.per_user_tree import check_per_user_tree_exists as check_per_user_tree_exists
from core.doctor.checks.per_user_tree import check_per_user_tree_mode as check_per_user_tree_mode
from core.doctor.checks.privilege_boundary import (
    check_compose_project_name_collision as check_compose_project_name_collision,
)
from core.doctor.checks.privilege_boundary import check_docker_available as check_docker_available
from core.doctor.checks.privilege_boundary import check_docker_rootless as check_docker_rootless
from core.doctor.checks.privilege_boundary import check_host_uds as check_host_uds
from core.doctor.checks.privilege_boundary import check_machinectl as check_machinectl
from core.doctor.checks.privilege_boundary import check_machinectl_reachable as check_machinectl_reachable
from core.doctor.checks.privilege_boundary import check_runsc_registered as check_runsc_registered
from core.doctor.checks.privilege_boundary import check_runsc_runtimeargs as check_runsc_runtimeargs
from core.doctor.checks.privilege_boundary import check_sudo as check_sudo
from core.doctor.checks.privilege_boundary import check_systemd_machined as check_systemd_machined
from core.doctor.checks.privilege_boundary import check_user_exists as check_user_exists
from core.doctor.checks.repo_integrity import _UNCONDITIONAL_FILES as _UNCONDITIONAL_FILES
from core.doctor.checks.repo_integrity import _resource_files as _resource_files
from core.doctor.checks.repo_integrity import check_state_dir_writable as check_state_dir_writable
from core.doctor.checks.repo_integrity import check_tooling_plane as check_tooling_plane
from core.doctor.checks.supply_chain import check_image_digests as check_image_digests
from core.doctor.checks.workspace_bridge import _read_registry_raw as _read_registry_raw
from core.doctor.checks.workspace_bridge import _scan_instance_dirs as _scan_instance_dirs
from core.doctor.checks.workspace_bridge import (
    _scan_instance_workspace_paths as _scan_instance_workspace_paths,
)
from core.doctor.checks.workspace_bridge import (
    check_backups_disk_pressure as check_backups_disk_pressure,
)
from core.doctor.checks.workspace_bridge import (
    check_backups_partial_dirs_present as check_backups_partial_dirs_present,
)
from core.doctor.checks.workspace_bridge import (
    check_dev_in_workspace_bridge_group as check_dev_in_workspace_bridge_group,
)
from core.doctor.checks.workspace_bridge import (
    check_dev_umask_workspace_friendly as check_dev_umask_workspace_friendly,
)
from core.doctor.checks.workspace_bridge import (
    check_helper_image_pulled as check_helper_image_pulled,
)
from core.doctor.checks.workspace_bridge import (
    check_pre_existing_instance_layout as check_pre_existing_instance_layout,
)
from core.doctor.checks.workspace_bridge import (
    check_secrets_hydrated_restrictively as check_secrets_hydrated_restrictively,
)
from core.doctor.checks.workspace_bridge import (
    check_subuid_resolver_works as check_subuid_resolver_works,
)
from core.doctor.checks.workspace_bridge import (
    check_workspace_bridge_group_exists as check_workspace_bridge_group_exists,
)
from core.doctor.checks.workspace_bridge import (
    check_workspace_home_single_filesystem as check_workspace_home_single_filesystem,
)
from core.doctor.checks.workspace_bridge import (
    check_workspace_path_in_walker_boundary as check_workspace_path_in_walker_boundary,
)
from core.doctor.types import _BINARY_PACKAGES as _BINARY_PACKAGES
from core.doctor.types import Check as Check
from core.doctor.types import CheckResult as CheckResult
from core.doctor.types import detect_distro as detect_distro
from core.doctor.types import get_install_cmd as get_install_cmd
from core.host_config import MachinectlAuth

if TYPE_CHECKING:
    from rich.console import Console


# ─── Section 8: Check Runner ────────────────────────────────────────────────


# ─── Acl-Ownership-Recipes Checks ──────────────────────────────────────────


def build_check_registry(auth_mode: MachinectlAuth = MachinectlAuth.SUDO) -> list[Check]:
    """Build the doctor check registry with auth-mode-aware machinectl checks.

    When ``auth_mode == MachinectlAuth.POLKIT``, the `sudo` binary check is
    omitted from the registry and the `machinectl_reachable` check no longer
    depends on `sudo`. The 7 machinectl-invoking checks are partial-bound with
    ``auth_mode`` so they construct command prefixes via ``machinectl_cmd()``.
    """
    is_sudo = auth_mode == MachinectlAuth.SUDO
    machinectl_reachable_deps = (
        ["sudo", "machinectl", "user_exists", "systemd_machined"]
        if is_sudo
        else ["machinectl", "user_exists", "systemd_machined"]
    )

    chain1: list[Check] = []
    if is_sudo:
        chain1.append(
            Check(
                id="sudo",
                name="sudo binary",
                category="Privilege Boundary",
                depends_on=[],
                run=check_sudo,
                remediation="",
            )
        )
    chain1.extend(
        [
            Check(
                id="machinectl",
                name="machinectl binary",
                category="Privilege Boundary",
                depends_on=[],
                run=check_machinectl,
                remediation="",
            ),
            Check(
                id="user_exists",
                name="unprivileged user",
                category="Privilege Boundary",
                depends_on=[],
                run=check_user_exists,
                remediation="",
            ),
            Check(
                id="systemd_machined",
                name="systemd-machined",
                category="Privilege Boundary",
                depends_on=["machinectl"],
                run=check_systemd_machined,
                remediation="",
            ),
            Check(
                id="machinectl_reachable",
                name="machinectl reachable",
                category="Privilege Boundary",
                depends_on=machinectl_reachable_deps,
                run=functools.partial(check_machinectl_reachable, auth_mode=auth_mode),
                remediation="",
            ),
            Check(
                id="docker_available",
                name="Docker available",
                category="Privilege Boundary",
                depends_on=["machinectl_reachable"],
                run=functools.partial(check_docker_available, auth_mode=auth_mode),
                remediation="",
            ),
            Check(
                id="docker_rootless",
                name="Docker rootless",
                category="Privilege Boundary",
                depends_on=["docker_available"],
                run=functools.partial(check_docker_rootless, auth_mode=auth_mode),
                remediation="",
            ),
            Check(
                id="runsc",
                name="gVisor runsc",
                category="Privilege Boundary",
                depends_on=["docker_available"],
                run=functools.partial(check_runsc_registered, auth_mode=auth_mode),
                remediation="",
            ),
            Check(
                id="runsc_runtimeargs",
                name="runsc runtimeArgs",
                category="Privilege Boundary",
                depends_on=["runsc"],
                run=functools.partial(check_runsc_runtimeargs, auth_mode=auth_mode),
                remediation="",
            ),
            Check(
                id="host_uds",
                name="--host-uds=none",
                category="Privilege Boundary",
                depends_on=["runsc"],
                run=functools.partial(check_host_uds, auth_mode=auth_mode),
                remediation="",
            ),
            Check(
                id="compose_project_name_collision",
                name="compose project name collision",
                category="Privilege Boundary",
                depends_on=["machinectl_reachable"],
                run=functools.partial(check_compose_project_name_collision, auth_mode=auth_mode),
                remediation="",
            ),
        ]
    )

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
        Check(
            id="ancestor_traverse",
            name="ancestor traverse",
            category="Filesystem",
            depends_on=["acl_support"],
            run=check_ancestor_traverse,
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
            run=functools.partial(check_image_digests, auth_mode=auth_mode),
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
        # Chain 6: workspace bridge group + helper-recipe prereqs
        Check(
            id="workspace_bridge_group_exists",
            name="workspace bridge group",
            category="Workspace Bridge",
            depends_on=[],
            run=check_workspace_bridge_group_exists,
            remediation="",
        ),
        Check(
            id="dev_in_workspace_bridge_group",
            name="dev in workspace bridge group",
            category="Workspace Bridge",
            depends_on=["workspace_bridge_group_exists"],
            run=check_dev_in_workspace_bridge_group,
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
            id="helper_image_pulled",
            name="helper image cached",
            category="Workspace Bridge",
            depends_on=[],
            run=check_helper_image_pulled,
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
            name="dev umask workspace-friendly",
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
) -> list[CheckResult]:
    """Execute checks in topological order with cascading skip on failed deps."""
    ordered = topological_sort(checks)
    results: dict[str, CheckResult] = {}
    output: list[CheckResult] = []

    for check in ordered:
        # Check if any dependency failed
        failed_deps = [dep for dep in check.depends_on if dep in results and results[dep].status in ("fail", "skip")]

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
    auth_mode: MachinectlAuth = MachinectlAuth.SUDO,
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

    Raises:
        ValueError: If any ``depends_on`` reference in the filtered subset
            points to a check outside the subset.
    """
    if not categories:
        return []

    registry = build_check_registry(auth_mode)
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

    return run_checks(subset, user, distro)


# ─── Section 9: Rich Output Renderer ────────────────────────────────────────


def render_results(
    results: list[CheckResult],
    *,
    console: Console | None = None,
) -> None:
    """Render check results using Rich with progressive disclosure."""
    from rich.console import Console as RichConsole
    from rich.text import Text

    if console is None:
        console = RichConsole()

    # Group by category
    grouped: dict[str, list[CheckResult]] = defaultdict(list)
    for r in results:
        cat = r.category or "General"
        grouped[cat].append(r)

    pass_count = sum(1 for r in results if r.status == "pass")
    fail_count = sum(1 for r in results if r.status == "fail")
    skip_count = sum(1 for r in results if r.status == "skip")
    warn_count = sum(1 for r in results if r.status == "warn")

    for category, checks in grouped.items():
        console.print(f"\n[bold]{category}[/bold]")
        for r in checks:
            if r.status == "pass":
                line = Text(f"  ✓ {r.name}", style="green")
                if r.detail:
                    line.append(f"  {r.detail}", style="dim")
                console.print(line)
            elif r.status == "fail":
                console.print(Text(f"  ✗ {r.name}", style="red bold"))
                console.print(f"    {r.detail}")
                if r.remediation:
                    console.print(f"    Fix: {r.remediation}", style="yellow")
                if r.doc_ref:
                    console.print(f"    Docs: {r.doc_ref}", style="dim")
            elif r.status == "warn":
                console.print(Text(f"  ⚠ {r.name}", style="yellow"))
                console.print(f"    {r.detail}")
                if r.remediation:
                    console.print(f"    Fix: {r.remediation}", style="yellow")
            elif r.status == "skip":
                console.print(Text(f"  ⊘ {r.name} — {r.detail}", style="dim"))

    # Summary line
    console.print()
    summary = f"{pass_count}/{len(results)} passed"
    if warn_count:
        summary += f" · {warn_count} warnings"
    if fail_count:
        summary += f" · {fail_count} failed"
    if skip_count:
        summary += f" · {skip_count} skipped"

    if fail_count > 0:
        style = "red bold"
    elif warn_count > 0:
        style = "yellow bold"
    else:
        style = "green bold"
    console.print(summary, style=style)
