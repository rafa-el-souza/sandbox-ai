"""Per-user-tree doctor checks: layout existence, mode, and legacy-CWD detection."""

from __future__ import annotations

import os
import tomllib

from core.doctor.types import CheckResult
from core.host_config import sandbox_ai_home


def check_per_user_tree_exists(user: str, distro: str | None) -> CheckResult:
    """Verify that ``<home>/``, ``<home>/config/``, ``<home>/state/`` exist."""
    del user, distro
    home = sandbox_ai_home()
    missing: list[str] = []
    for sub in ("", "config", "state"):
        candidate = home / sub if sub else home
        if not candidate.is_dir():
            missing.append(str(candidate))
    if not missing:
        return CheckResult(
            status="pass",
            name="per-user tree exists",
            detail=f"All required directories present at {home}",
        )
    return CheckResult(
        status="fail",
        name="per-user tree exists",
        detail=f"Missing directories: {', '.join(missing)}",
        remediation=f"Run sandbox init to create the per-user tree at {home}.",
    )


def check_per_user_tree_mode(user: str, distro: str | None) -> CheckResult:
    """Warn when ``<home>``, ``<home>/config``, or ``<home>/state`` are more permissive than 0700."""
    import stat as _stat

    del user, distro
    home = sandbox_ai_home()
    drift: list[tuple[str, int]] = []
    for sub in ("", "config", "state"):
        path = home / sub if sub else home
        if not path.is_dir():
            # Tree absent — defer to existence check.
            return CheckResult(
                status="skip",
                name="per-user tree mode",
                detail="Tree not initialized (see per-user tree exists)",
            )
        mode = _stat.S_IMODE(path.stat().st_mode)
        if mode != 0o700:
            drift.append((str(path), mode))
    if not drift:
        return CheckResult(
            status="pass",
            name="per-user tree mode",
            detail="All directories are mode 0700",
        )
    paths = ", ".join(f"{p} (mode {oct(m)})" for p, m in drift)
    fix = "; ".join(f"chmod 0700 {p}" for p, _m in drift)
    return CheckResult(
        status="warn",
        name="per-user tree mode",
        detail=f"Mode drift detected: {paths}; expected 0700",
        remediation=fix,
    )


def check_legacy_cwd_files(user: str, distro: str | None) -> CheckResult:
    """Warn when legacy ``<cwd>/sandbox-ai.toml`` or ``<cwd>/.state/`` exist.

    The legacy path tokens in this docstring are intentional and load-bearing:
    they help users grepping the codebase during migration. Per the
    per-user-config-and-state-relocation change (task 14.7), do not remove
    them in future cleanups.
    """
    del user, distro
    cwd = os.getcwd()
    home = sandbox_ai_home()
    legacy: list[str] = []
    if os.path.exists(os.path.join(cwd, "sandbox-ai.toml")):
        legacy.append(os.path.join(cwd, "sandbox-ai.toml"))
    if os.path.isdir(os.path.join(cwd, ".state")):
        legacy.append(os.path.join(cwd, ".state"))
    if not legacy:
        return CheckResult(
            status="pass",
            name="legacy CWD files",
            detail="No legacy CWD-local orchestrator files detected",
        )
    return CheckResult(
        status="warn",
        name="legacy CWD files",
        detail=f"Legacy files detected: {', '.join(legacy)}",
        remediation=(
            f"Per-host config now lives at {home / 'config' / 'sandbox-ai.toml'} and orchestrator state at "
            f"{home / 'state'}. Migrate manually or delete the legacy files."
        ),
    )


def check_legacy_sandboxes_dir_detected(host_user: str, distro: str | None) -> CheckResult:
    """Warn if the CWD contains a legacy ``sandboxes/`` directory (pre-change-5)."""
    del host_user, distro
    cwd_sandboxes = os.path.join(os.getcwd(), "sandboxes")
    if os.path.isdir(cwd_sandboxes):
        return CheckResult(
            status="warn",
            name="legacy sandboxes dir detected",
            detail=f"pre-change-5 layout at {cwd_sandboxes}",
            remediation=f"Confirm no useful state remains, then `rm -rf {cwd_sandboxes}`",
            category="Per-User Tree",
        )
    return CheckResult(
        status="pass",
        name="legacy sandboxes dir detected",
        detail="no legacy sandboxes/ in CWD",
        category="Per-User Tree",
    )


def check_legacy_workspace_in_user_project_root(host_user: str, distro: str | None) -> CheckResult:
    """Warn if any registered instance's sandbox.toml carries the legacy
    ``[instance].user_project_root`` field."""
    del host_user, distro
    # Lazy import to avoid circular dependency: workspace_bridge depends on this
    # module's package siblings during the in-flight refactor.
    from core.doctor import _scan_instance_dirs

    legacy: list[str] = []
    for inst_dir in _scan_instance_dirs():
        toml_path = os.path.join(inst_dir, "sandbox.toml")
        try:
            with open(toml_path, "rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        instance = data.get("instance", {})
        if isinstance(instance, dict) and "user_project_root" in instance:
            legacy.append(os.path.basename(inst_dir))
    if legacy:
        return CheckResult(
            status="warn",
            name="legacy user_project_root field",
            detail=f"{len(legacy)} instance(s) carry legacy field: {', '.join(legacy[:3])}",
            remediation=(
                "Run `sandbox destroy <inst> && sandbox init <inst> --copy <ws>=<former-user-project-root>` "
                "to migrate."
            ),
            category="Per-User Tree",
        )
    return CheckResult(
        status="pass",
        name="legacy user_project_root field",
        detail="no legacy user_project_root fields detected",
        category="Per-User Tree",
    )


def check_legacy_registry_shape(host_user: str, distro: str | None) -> CheckResult:
    """Warn if ``instances.json`` is path-keyed (pre-change-5 shape)."""
    del host_user, distro
    # Lazy import to avoid circular dependency during the in-flight refactor.
    from core.doctor import _read_registry_raw

    data = _read_registry_raw()
    legacy_keys = [k for k in data if isinstance(k, str) and k.startswith("/")]
    if legacy_keys:
        return CheckResult(
            status="warn",
            name="legacy registry shape",
            detail=f"{len(legacy_keys)} path-keyed entries in instances.json",
            remediation="`rm ~/.sandbox-ai/state/instances.json && sandbox init <each-inst>`",
            category="Per-User Tree",
        )
    return CheckResult(
        status="pass",
        name="legacy registry shape",
        detail="instances.json is name-keyed",
        category="Per-User Tree",
    )


__all__ = [
    "check_legacy_cwd_files",
    "check_legacy_registry_shape",
    "check_legacy_sandboxes_dir_detected",
    "check_legacy_workspace_in_user_project_root",
    "check_per_user_tree_exists",
    "check_per_user_tree_mode",
]
