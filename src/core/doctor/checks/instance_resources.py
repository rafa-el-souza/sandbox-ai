# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-instance host-resource capacity doctor checks.

Two advisory (WARN-only) checks that guard the rendered `compose.yml` against
the host's actual CPU/RAM budget:

- ``check_host_cpu_capacity`` — WARN when any rendered service's ``cpus`` exceeds
  the host CPU count, replacing Docker's opaque
  ``range of CPUs is from 0.01 to N.NN`` error with an actionable message.
- ``check_instance_memory_overcommit`` — WARN when the summed ``mem_limit`` of an
  instance's services exceeds host RAM (containers may be OOM-killed under
  pressure).

Both source their limits ONLY from the instance's rendered
``<instance_dir>/docker/compose.yml`` (the authoritative artifact) — never from
re-spelled container constants — and skip any registered instance without a
rendered compose, mirroring the cascading-skip pattern used by the other
per-instance scans. Host detection routes through ``core.host_resources`` so the
hydration clamp and these checks never disagree about the host's capacity.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from core.doctor.checks.workspace_bridge import _scan_instance_dirs
from core.doctor.types import CheckResult
from core.host_resources import host_cpu_count, host_ram_bytes, parse_docker_size

_CATEGORY = "Instance Resources"

# The rendered compose lives at this leaf under each registered instance dir
# (mirrors ``core.dispatch._resolve_compose_state``'s first compose-file entry).
_COMPOSE_LEAF = ("docker", "compose.yml")


@dataclass(frozen=True)
class _ComposeAbsent:
    """Skip signal: the instance has no rendered compose (mirrors the
    cascading-skip pattern — an absent artifact is "not applicable", not a
    failure)."""


def _load_service_limits(instance_dir: str) -> dict[str, dict[str, object]] | _ComposeAbsent:
    """Load a registered instance's rendered compose and return its services map.

    Returns a ``{service_name: service_body}`` dict on success, or
    ``_ComposeAbsent`` when the rendered ``compose.yml`` is missing/unreadable so
    callers skip that instance (the cascading-skip pattern). YAML is parsed with
    ``ruamel.yaml`` (the repo's YAML lib) in safe mode — the rendered compose is
    config-file data read at a system boundary, so a malformed/unexpected shape
    is treated as "no usable limits" (skip) rather than an error.
    """
    from ruamel.yaml import YAML
    from ruamel.yaml.error import YAMLError

    compose_path = os.path.join(instance_dir, *_COMPOSE_LEAF)
    try:
        with open(compose_path) as f:
            data = YAML(typ="safe").load(f)
    except (OSError, YAMLError):
        return _ComposeAbsent()
    if not isinstance(data, dict):
        return _ComposeAbsent()
    services = data.get("services")
    if not isinstance(services, dict):
        return _ComposeAbsent()
    return {name: body for name, body in services.items() if isinstance(body, dict)}


def check_host_cpu_capacity(host_user: str, distro: str | None) -> CheckResult:
    """WARN when any rendered service's ``cpus`` exceeds the host CPU count.

    Iterates the registry (``_scan_instance_dirs``) and, for each instance with a
    rendered compose, compares every service's ``cpus`` against
    ``host_cpu_count()``. Instances without a rendered compose are skipped.
    Advisory WARN only — never FAIL — so this never flips the doctor exit
    contract; it surfaces on-disk divergence (compose rendered on a larger host,
    relocated, or hand-edited) that ``start`` would otherwise reject opaquely.
    """
    del host_user, distro
    cpus = host_cpu_count()
    offenders: list[str] = []
    for instance_dir in _scan_instance_dirs():
        services = _load_service_limits(instance_dir)
        if isinstance(services, _ComposeAbsent):
            continue
        offenders.extend(_cpu_offenders(instance_dir, services, cpus))

    if offenders:
        return CheckResult(
            status="warn",
            name="host CPU capacity",
            detail=(
                f"{len(offenders)} service(s) request more CPUs than the host's "
                f"{cpus}: {'; '.join(offenders)}"
            ),
            remediation=f"Lower each service's cpus to ≤ {cpus} (the host CPU count) and re-run `sandbox start`",
            category=_CATEGORY,
        )
    return CheckResult(
        status="pass",
        name="host CPU capacity",
        detail=f"all rendered cpus within host count ({cpus})",
        category=_CATEGORY,
    )


def _cpu_offenders(instance_dir: str, services: dict[str, dict[str, object]], host_cpus: int) -> list[str]:
    """Return ``"<inst>/<service> cpus=<v>"`` strings for services over host CPUs.

    A service whose ``cpus`` is absent or unparseable contributes nothing — the
    capacity check only flags concrete over-requests, leaving shape validation to
    hydration.
    """
    inst_name = os.path.basename(instance_dir)
    over: list[str] = []
    for name, body in services.items():
        raw = body.get("cpus")
        if not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if value > host_cpus:
            over.append(f"{inst_name}/{name} cpus={raw}")
    return over


def check_instance_memory_overcommit(host_user: str, distro: str | None) -> CheckResult:
    """WARN when an instance's summed ``mem_limit`` exceeds host physical RAM.

    Per registered instance with a rendered compose, sums the services'
    ``mem_limit`` (Docker size strings parsed via ``parse_docker_size``) and warns
    when the total exceeds ``host_ram_bytes()``. Advisory WARN only — memory
    limits are not clamped at render (an over-commit over-commits rather than
    failing ``start``), so this provides visibility only and never flips the
    doctor exit contract. Instances without a rendered compose are skipped.
    """
    del host_user, distro
    ram = host_ram_bytes()
    overcommitted: list[str] = []
    for instance_dir in _scan_instance_dirs():
        services = _load_service_limits(instance_dir)
        if isinstance(services, _ComposeAbsent):
            continue
        total = _sum_mem_limits(services)
        if total > ram:
            inst_name = os.path.basename(instance_dir)
            overcommitted.append(f"{inst_name} requests {total} bytes")

    if overcommitted:
        return CheckResult(
            status="warn",
            name="instance memory over-commit",
            detail=(
                f"{len(overcommitted)} instance(s) sum mem_limit above host RAM "
                f"({ram} bytes): {'; '.join(overcommitted)} — containers may be "
                "OOM-killed under memory pressure"
            ),
            remediation=f"Lower the services' mem_limit so each instance's total stays ≤ {ram} bytes (host RAM)",
            category=_CATEGORY,
        )
    return CheckResult(
        status="pass",
        name="instance memory over-commit",
        detail=f"all instances within host RAM ({ram} bytes)",
        category=_CATEGORY,
    )


def _sum_mem_limits(services: dict[str, dict[str, object]]) -> int:
    """Sum the services' ``mem_limit`` values in bytes.

    A service whose ``mem_limit`` is absent or unparseable contributes nothing —
    the over-commit check only sums concrete limits, leaving shape validation to
    hydration.
    """
    total = 0
    for body in services.values():
        raw = body.get("mem_limit")
        if not isinstance(raw, (str, int)) or isinstance(raw, bool):
            continue
        try:
            total += parse_docker_size(raw)
        except ValueError:
            continue
    return total


__all__ = ["check_host_cpu_capacity", "check_instance_memory_overcommit"]
