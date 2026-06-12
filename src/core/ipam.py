# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""IPAM allocator: /24-quintuple subnet allocation with lowest-slot scan and overflow detection.

Each sandbox instance is assigned five consecutive /24 subnets (isolated, core_proxy, dns, egress,
ipc) from the 10.100.0.0-10.255.255.0 range. The ledger maps instance_id -> base_index
(integer). Subnets are derived at runtime using: 10.(100 + g//256).(g%256).0/24 where g = base_index * 5.

Maximum concurrent instances: 7,987 (base_index 0-7986).
"""

import fcntl
import json
import os
from typing import cast

from core.host_config import ipam_lock_path, sandbox_ai_home

MAX_SLOTS = 7987


def _default_ledger_path() -> str:
    """Resolve ``<home>/state/ipam.json`` for the current user."""
    return str(sandbox_ai_home() / "state" / "ipam.json")


class IPAMExhaustedError(Exception):
    """Raised when all IPAM slots are consumed."""

    pass


class IPAMLockException(BlockingIOError):
    """Raised when IPAM lock cannot be acquired."""

    pass


class IPAMLedger:
    """File-backed IPAM ledger with fcntl locking for concurrent access."""

    def __init__(self, ledger_path: str | None = None) -> None:
        self._path = ledger_path if ledger_path is not None else _default_ledger_path()

    def _load(self) -> dict[str, int]:
        """Load the ledger from disk. Returns empty dict if file missing or corrupt."""
        if not os.path.exists(self._path):
            return {}
        with open(self._path) as f:
            try:
                data: dict[str, int] = json.load(f)
                return data
            except json.JSONDecodeError:
                return {}

    def _save(self, data: dict[str, int]) -> None:
        """Write the ledger to disk."""
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with open(self._path, "w") as f:
            json.dump(data, f, indent=2)

    def _acquire_lock(self) -> int:
        """Acquire the dedicated IPAM ledger lock. Returns the lock fd."""
        lock_path = str(ipam_lock_path())
        os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock_fd
        except BlockingIOError as exc:
            os.close(lock_fd)
            raise IPAMLockException("Could not acquire IPAM lock") from exc

    def allocate(self, instance_id: str) -> int:
        """Allocate the lowest available base_index for an instance.

        Returns the existing base_index if instance_id is already allocated.
        Raises IPAMExhaustedError if all slots are consumed.
        """
        lock_fd = self._acquire_lock()
        try:
            data = self._load()

            # Idempotent: return existing allocation
            if instance_id in data:
                return data[instance_id]

            # Find lowest available slot
            used_indices = set(data.values())
            for candidate in range(MAX_SLOTS):
                if candidate not in used_indices:
                    data[instance_id] = candidate
                    self._save(data)
                    return candidate

            raise IPAMExhaustedError(
                f"All {MAX_SLOTS} IPAM slots are consumed. Free slots by running 'sandbox destroy' on unused instances."
            )
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def release(self, instance_id: str) -> None:
        """Release an IPAM slot, freeing it for reuse."""
        lock_fd = self._acquire_lock()
        try:
            data = self._load()
            data.pop(instance_id, None)
            self._save(data)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def peek_next_slot(self, instance_id: str) -> tuple[int, bool]:
        """Read-only scan: return (slot, is_existing) without lock or write.

        Returns the existing base_index if instance_id is already allocated (True),
        otherwise the lowest available slot (False).
        Raises IPAMExhaustedError if all slots are consumed.
        """
        data = self._load()

        # Existing allocation
        if instance_id in data:
            return data[instance_id], True

        # Find lowest available slot
        used_indices = set(data.values())
        for candidate in range(MAX_SLOTS):
            if candidate not in used_indices:
                return candidate, False

        raise IPAMExhaustedError(
            f"All {MAX_SLOTS} IPAM slots are consumed. Free slots by running 'sandbox destroy' on unused instances."
        )


def derive_subnets(base_index: int) -> tuple[str, str, str, str, str]:
    """Derive five /24 subnets from a base_index.

    Formula: g = base_index * 5
        isolated     = 10.(100 + g//256).(g%256).0/24
        core_proxy   = 10.(100 + (g+1)//256).((g+1)%256).0/24
        dns          = 10.(100 + (g+2)//256).((g+2)%256).0/24
        egress       = 10.(100 + (g+3)//256).((g+3)%256).0/24
        ipc          = 10.(100 + (g+4)//256).((g+4)%256).0/24 (IPC_NET)

    Returns the quintuple ``(isolated, core_proxy, dns, egress, ipc)``.

    Raises ValueError if base_index >= MAX_SLOTS.
    """
    if base_index >= MAX_SLOTS:
        raise ValueError(f"base_index {base_index} exceeds MAX_SLOTS ({MAX_SLOTS})")
    g = base_index * 5
    subnets = tuple(f"10.{100 + (g + i) // 256}.{(g + i) % 256}.0/24" for i in range(5))
    return cast("tuple[str, str, str, str, str]", subnets)


def derive_static_ips(base_index: int) -> dict[str, str]:
    """Derive all static IP addresses from a base_index.

    Uses five subnet bases for containers with multi-network membership.
    Each container gets a distinct static IP on every network it participates in.
    """
    (
        isolated_subnet,
        core_proxy_subnet,
        dns_subnet,
        egress_subnet,
        ipc_subnet,
    ) = derive_subnets(base_index)

    isolated_base = isolated_subnet.rsplit(".0/24", 1)[0]
    core_proxy_base = core_proxy_subnet.rsplit(".0/24", 1)[0]
    dns_base = dns_subnet.rsplit(".0/24", 1)[0]
    egress_base = egress_subnet.rsplit(".0/24", 1)[0]
    ipc_base = ipc_subnet.rsplit(".0/24", 1)[0]

    return {
        # Core (agent) — isolated_net + core_proxy_net + ipc_net
        "agent_isolated_ip": f"{isolated_base}.3",
        "agent_proxy_ip": f"{core_proxy_base}.3",
        "core_ipc_ip": f"{ipc_base}.3",
        # Proxy (squid) — core_proxy_net + egress_net
        "proxy_core_ip": f"{core_proxy_base}.254",
        # dnsdist — isolated_net + dns_net
        "dnsdist_isolated_ip": f"{isolated_base}.56",
        "dnsdist_dns_ip": f"{dns_base}.56",
        # coredns — dns_net + egress_net
        "coredns_dns_ip": f"{dns_base}.53",
        "coredns_egress_ip": f"{egress_base}.53",
        # Admin (human) — ipc_net only
        "admin_ipc_ip": f"{ipc_base}.2",
        # db-postgres — isolated_net
        "db_postgres_ip": f"{isolated_base}.54",
        # mcp-firecrawl — core_proxy_net + dns_net + isolated_net
        "mcp_firecrawl_proxy_ip": f"{core_proxy_base}.55",
        "firecrawl_dns_ip": f"{dns_base}.55",
        "firecrawl_isolated_ip": f"{isolated_base}.55",
    }
