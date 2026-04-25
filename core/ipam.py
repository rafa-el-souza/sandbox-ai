"""IPAM allocator: /24-sextuple subnet allocation with lowest-slot scan and overflow detection.

Each sandbox instance is assigned six consecutive /24 subnets (isolated, core_proxy, dns, admin,
admin_proxy, egress) from the 10.100.0.0-10.255.255.0 range. The ledger maps project_id -> base_index
(integer). Subnets are derived at runtime using: 10.(100 + g//256).(g%256).0/24 where g = base_index * 6.

Maximum concurrent instances: 6,656 (base_index 0-6655).
"""

import fcntl
import json
import os

MAX_SLOTS = 6656


class IPAMExhaustedError(Exception):
    """Raised when all IPAM slots are consumed."""

    pass


class IPAMLockException(BlockingIOError):
    """Raised when IPAM lock cannot be acquired."""

    pass


class IPAMLedger:
    """File-backed IPAM ledger with fcntl locking for concurrent access."""

    def __init__(self, ledger_path: str) -> None:
        self._path = ledger_path

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
        """Acquire the IPAM lock file. Returns the lock fd."""
        lock_path = self._path + ".lock"
        os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock_fd
        except BlockingIOError as exc:
            os.close(lock_fd)
            raise IPAMLockException("Could not acquire IPAM lock") from exc

    def allocate(self, project_id: str) -> int:
        """Allocate the lowest available base_index for a project.

        Returns the existing base_index if project_id is already allocated.
        Raises IPAMExhaustedError if all slots are consumed.
        """
        lock_fd = self._acquire_lock()
        try:
            data = self._load()

            # Idempotent: return existing allocation
            if project_id in data:
                return data[project_id]

            # Find lowest available slot
            used_indices = set(data.values())
            for candidate in range(MAX_SLOTS):
                if candidate not in used_indices:
                    data[project_id] = candidate
                    self._save(data)
                    return candidate

            raise IPAMExhaustedError(
                f"All {MAX_SLOTS} IPAM slots are consumed. Free slots by running 'sandbox destroy' on unused instances."
            )
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def release(self, project_id: str) -> None:
        """Release an IPAM slot, freeing it for reuse."""
        lock_fd = self._acquire_lock()
        try:
            data = self._load()
            data.pop(project_id, None)
            self._save(data)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def peek_next_slot(self, project_id: str) -> tuple[int, bool]:
        """Read-only scan: return (slot, is_existing) without lock or write.

        Returns the existing base_index if project_id is already allocated (True),
        otherwise the lowest available slot (False).
        Raises IPAMExhaustedError if all slots are consumed.
        """
        data = self._load()

        # Existing allocation
        if project_id in data:
            return data[project_id], True

        # Find lowest available slot
        used_indices = set(data.values())
        for candidate in range(MAX_SLOTS):
            if candidate not in used_indices:
                return candidate, False

        raise IPAMExhaustedError(
            f"All {MAX_SLOTS} IPAM slots are consumed. Free slots by running 'sandbox destroy' on unused instances."
        )


def derive_subnets(base_index: int) -> tuple[str, str, str, str, str, str]:
    """Derive six /24 subnets from a base_index.

    Formula: g = base_index * 6
        isolated     = 10.(100 + g//256).(g%256).0/24
        core_proxy   = 10.(100 + (g+1)//256).((g+1)%256).0/24
        dns          = 10.(100 + (g+2)//256).((g+2)%256).0/24
        admin        = 10.(100 + (g+3)//256).((g+3)%256).0/24
        admin_proxy  = 10.(100 + (g+4)//256).((g+4)%256).0/24
        egress       = 10.(100 + (g+5)//256).((g+5)%256).0/24

    Raises ValueError if base_index >= MAX_SLOTS.
    """
    if base_index >= MAX_SLOTS:
        raise ValueError(f"base_index {base_index} exceeds MAX_SLOTS ({MAX_SLOTS})")
    g = base_index * 6
    subnets = tuple(
        f"10.{100 + (g + i) // 256}.{(g + i) % 256}.0/24" for i in range(6)
    )
    return subnets  # type: ignore[return-value]


def derive_static_ips(base_index: int) -> dict[str, str]:
    """Derive all static IP addresses from a base_index.

    Uses six subnet bases for containers with multi-network membership.
    Each container gets a distinct static IP on every network it participates in.
    """
    (
        isolated_subnet,
        core_proxy_subnet,
        dns_subnet,
        admin_subnet,
        admin_proxy_subnet,
        egress_subnet,
    ) = derive_subnets(base_index)

    isolated_base = isolated_subnet.rsplit(".0/24", 1)[0]
    core_proxy_base = core_proxy_subnet.rsplit(".0/24", 1)[0]
    dns_base = dns_subnet.rsplit(".0/24", 1)[0]
    admin_base = admin_subnet.rsplit(".0/24", 1)[0]
    admin_proxy_base = admin_proxy_subnet.rsplit(".0/24", 1)[0]
    egress_base = egress_subnet.rsplit(".0/24", 1)[0]

    return {
        # Core (agent) — isolated_net + core_proxy_net
        "agent_isolated_ip": f"{isolated_base}.3",
        "agent_proxy_ip": f"{core_proxy_base}.3",
        # Proxy (squid) — core_proxy_net + admin_proxy_net + egress_net
        "proxy_core_ip": f"{core_proxy_base}.254",
        "proxy_admin_ip": f"{admin_proxy_base}.254",
        # dnsdist — isolated_net + dns_net + admin_net
        "dnsdist_isolated_ip": f"{isolated_base}.56",
        "dnsdist_dns_ip": f"{dns_base}.56",
        "dnsdist_admin_ip": f"{admin_base}.56",
        # coredns — dns_net + admin_net + egress_net
        "coredns_dns_ip": f"{dns_base}.53",
        "coredns_admin_ip": f"{admin_base}.53",
        "coredns_egress_ip": f"{egress_base}.53",
        # Admin (human) — admin_net + admin_proxy_net
        "admin_admin_ip": f"{admin_base}.2",
        "admin_proxy_ip": f"{admin_proxy_base}.2",
        # db-postgres — isolated_net + admin_net
        "db_postgres_ip": f"{isolated_base}.54",
        "db_postgres_admin_ip": f"{admin_base}.54",
        # mcp-firecrawl — core_proxy_net + dns_net
        "mcp_firecrawl_proxy_ip": f"{core_proxy_base}.55",
        "firecrawl_dns_ip": f"{dns_base}.55",
    }

