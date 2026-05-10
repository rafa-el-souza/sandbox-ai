"""Doctor module: host readiness diagnostics for sandbox operation.

Provides 16 diagnostic checks across 4 independent chains:
- Chain 1 (privilege boundary, 10 checks): sudo -> machinectl -> user -> machined
  -> reachable -> docker -> rootless -> runsc -> runsc_runtimeargs -> host_uds
- Chain 2 (filesystem, 3 checks): setfacl → ACL support → ancestor traverse
- Chain 3 (repo integrity, 2 checks): tooling plane, state dir (independent)
- Chain 4 (supply chain, 1 check): image_digests (depends on docker_available)
"""

from __future__ import annotations

from collections import defaultdict
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
from core.doctor.registry import build_check_registry as build_check_registry
from core.doctor.registry import run_check_subset as run_check_subset
from core.doctor.registry import run_checks as run_checks
from core.doctor.registry import topological_sort as topological_sort
from core.doctor.types import _BINARY_PACKAGES as _BINARY_PACKAGES
from core.doctor.types import Check as Check
from core.doctor.types import CheckResult as CheckResult
from core.doctor.types import detect_distro as detect_distro
from core.doctor.types import get_install_cmd as get_install_cmd

if TYPE_CHECKING:
    from rich.console import Console


# Public surface re-exported by this package. Topic modules own the
# implementations; the package binds them to the public boundary.
__all__ = [
    "Check",
    "CheckResult",
    "build_check_registry",
    "check_acl_support",
    "check_ancestor_traverse",
    "check_backups_disk_pressure",
    "check_backups_partial_dirs_present",
    "check_compose_project_name_collision",
    "check_dev_in_workspace_bridge_group",
    "check_dev_umask_workspace_friendly",
    "check_docker_available",
    "check_docker_rootless",
    "check_helper_image_pulled",
    "check_host_uds",
    "check_image_digests",
    "check_legacy_cwd_files",
    "check_legacy_registry_shape",
    "check_legacy_sandboxes_dir_detected",
    "check_legacy_workspace_in_user_project_root",
    "check_machinectl",
    "check_machinectl_reachable",
    "check_per_user_tree_exists",
    "check_per_user_tree_mode",
    "check_pre_existing_instance_layout",
    "check_runsc_registered",
    "check_runsc_runtimeargs",
    "check_secrets_hydrated_restrictively",
    "check_setfacl",
    "check_state_dir_writable",
    "check_subuid_resolver_works",
    "check_sudo",
    "check_systemd_machined",
    "check_tooling_plane",
    "check_user_exists",
    "check_workspace_bridge_group_exists",
    "check_workspace_home_single_filesystem",
    "check_workspace_path_in_walker_boundary",
    "detect_distro",
    "get_install_cmd",
    "render_results",
    "run_check_subset",
    "run_checks",
    "topological_sort",
]


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
