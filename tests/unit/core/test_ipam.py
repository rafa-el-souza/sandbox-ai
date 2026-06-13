# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the IPAM /24-quintuple allocator with slot reuse and overflow detection."""

import json
from pathlib import Path

import pytest
from core.ipam import (
    MAX_SLOTS,
    IPAMExhaustedError,
    IPAMLedger,
    derive_static_ips,
    derive_subnets,
)


@pytest.fixture
def ledger(isolated_sandbox_ai_home: Path) -> IPAMLedger:
    """Create an IPAM ledger rooted at the per-user home (autouse fixture)."""
    return IPAMLedger()


def _ledger_file(home: Path) -> Path:
    return home / "state" / "ipam.json"


class TestIPAMLedger:
    def test_allocate_lowest_slot(self, ledger: IPAMLedger) -> None:
        """First allocation gets base_index 0."""
        idx = ledger.allocate("instance-aaa")
        assert idx == 0

    def test_sequential_allocation(self, ledger: IPAMLedger) -> None:
        """Sequential allocations get incrementing indices."""
        idx0 = ledger.allocate("p1")
        idx1 = ledger.allocate("p2")
        idx2 = ledger.allocate("p3")
        assert idx0 == 0
        assert idx1 == 1
        assert idx2 == 2

    def test_idempotent_reallocation(self, ledger: IPAMLedger) -> None:
        """Re-allocating the same instance_id returns the same base_index."""
        idx1 = ledger.allocate("instance-aaa")
        idx2 = ledger.allocate("instance-aaa")
        assert idx1 == idx2

    def test_slot_freed_after_release(self, ledger: IPAMLedger) -> None:
        """Released slot is reused by next allocation (lowest-available)."""
        ledger.allocate("p1")  # slot 0
        ledger.allocate("p2")  # slot 1
        ledger.allocate("p3")  # slot 2

        ledger.release("p2")  # frees slot 1

        idx = ledger.allocate("p4")
        assert idx == 1  # lowest available

    def test_release_nonexistent_noop(self, ledger: IPAMLedger) -> None:
        """Releasing a non-existent instance_id does not raise."""
        ledger.release("nonexistent")

    def test_overflow_detection(self, isolated_sandbox_ai_home: Path) -> None:
        """IPAMExhaustedError raised when all 7987 slots consumed."""
        ledger_path = _ledger_file(isolated_sandbox_ai_home)
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        data = {f"p{i}": i for i in range(7987)}
        ledger_path.write_text(json.dumps(data))

        ledger = IPAMLedger()
        with pytest.raises(IPAMExhaustedError, match="sandbox destroy"):
            ledger.allocate("one-more-instance")

    def test_corrupt_json_recovers(self, isolated_sandbox_ai_home: Path) -> None:
        """Corrupt JSON ledger is treated as empty."""
        ledger_path = _ledger_file(isolated_sandbox_ai_home)
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger_path.write_text("{ bad json }")
        ledger = IPAMLedger()
        idx = ledger.allocate("instance-aaa")
        assert idx == 0

    def test_lock_contention_raises(self, isolated_sandbox_ai_home: Path) -> None:
        """IPAMLockException raised when lock is already held."""
        from unittest.mock import patch

        from core.ipam import IPAMLockException

        ledger = IPAMLedger()

        with (
            patch("fcntl.flock", side_effect=BlockingIOError(11, "Resource temporarily unavailable")),
            pytest.raises(IPAMLockException, match="Could not acquire IPAM lock"),
        ):
            ledger.allocate("instance-aaa")


class TestMaxSlots:
    """MAX_SLOTS updated for 5-subnet topology."""

    def test_max_slots_value(self) -> None:
        """MAX_SLOTS is 7987 for the 5-subnet-per-instance model."""
        assert MAX_SLOTS == 7987


class TestDeriveSubnets:
    """derive_subnets returns 5-tuple (isolated, core_proxy, dns, egress, ipc)."""

    def test_base_index_zero(self) -> None:
        """base_index=0 → g=0: five consecutive /24 subnets starting at 10.100.0.0."""
        isolated, core_proxy, dns, egress, ipc = derive_subnets(0)
        assert isolated == "10.100.0.0/24"
        assert core_proxy == "10.100.1.0/24"
        assert dns == "10.100.2.0/24"
        assert egress == "10.100.3.0/24"
        assert ipc == "10.100.4.0/24"

    def test_base_index_one(self) -> None:
        """base_index=1 → g=5: five consecutive /24 subnets starting at 10.100.5.0."""
        isolated, core_proxy, dns, egress, ipc = derive_subnets(1)
        assert isolated == "10.100.5.0/24"
        assert core_proxy == "10.100.6.0/24"
        assert dns == "10.100.7.0/24"
        assert egress == "10.100.8.0/24"
        assert ipc == "10.100.9.0/24"

    def test_base_index_max_valid(self) -> None:
        """base_index=7986 → g=39930: verify correct subnet derivation."""
        g = 7986 * 5
        expected: list[str] = []
        for offset in range(5):
            gn = g + offset
            expected.append(f"10.{100 + gn // 256}.{gn % 256}.0/24")

        result = derive_subnets(7986)
        assert len(result) == 5
        for i in range(5):
            assert result[i] == expected[i]

    def test_overflow_raises_value_error(self) -> None:
        """derive_subnets(7987) raises ValueError — bounds check guard."""
        with pytest.raises(ValueError):
            derive_subnets(7987)


class TestDeriveStaticIPs:
    """derive_static_ips returns the post-admin-reframe key set across 5 subnets."""

    def test_output_keys(self) -> None:
        """derive_static_ips returns the expected IP keys."""
        ips = derive_static_ips(0)
        expected_keys = {
            "agent_isolated_ip",
            "agent_proxy_ip",
            "proxy_core_ip",
            "dnsdist_isolated_ip",
            "dnsdist_dns_ip",
            "coredns_dns_ip",
            "coredns_egress_ip",
            "db_postgres_ip",
            "mcp_firecrawl_proxy_ip",
            "firecrawl_dns_ip",
            "core_ipc_ip",
            "admin_ipc_ip",
            "firecrawl_isolated_ip",
        }
        assert set(ips.keys()) == expected_keys

    def test_legacy_keys_absent(self) -> None:
        """Legacy keys from prior models are removed."""
        ips = derive_static_ips(0)
        assert "dns_sidecar_ip" not in ips
        assert "admin_isolated_ip" not in ips
        assert "mcp_firecrawl_isolated_ip" not in ips
        assert "proxy_ip" not in ips

    def test_admin_reframe_keys_absent(self) -> None:
        """Admin-network keys removed by the admin-reframe network shrink."""
        ips = derive_static_ips(0)
        assert "coredns_admin_ip" not in ips
        assert "dnsdist_admin_ip" not in ips
        assert "proxy_admin_ip" not in ips
        assert "admin_admin_ip" not in ips
        assert "admin_proxy_ip" not in ips
        assert "db_postgres_admin_ip" not in ips

    def test_specific_values_at_index_zero(self) -> None:
        """Verify critical IP values at base_index=0 per spec scenarios."""
        ips = derive_static_ips(0)
        # coredns on dns_net (10.100.2.0/24) at .53
        assert ips["coredns_dns_ip"] == "10.100.2.53"
        # proxy on core_proxy_net (10.100.1.0/24) at .254
        assert ips["proxy_core_ip"] == "10.100.1.254"
        # dnsdist on isolated_net (10.100.0.0/24) at .56
        assert ips["dnsdist_isolated_ip"] == "10.100.0.56"

    def test_all_values_at_index_zero(self) -> None:
        """Verify all IP values at base_index=0 across the quintuple."""
        ips = derive_static_ips(0)
        # isolated_net = 10.100.0.0/24
        assert ips["agent_isolated_ip"] == "10.100.0.3"
        assert ips["dnsdist_isolated_ip"] == "10.100.0.56"
        assert ips["db_postgres_ip"] == "10.100.0.54"
        assert ips["firecrawl_isolated_ip"] == "10.100.0.55"
        # core_proxy_net = 10.100.1.0/24
        assert ips["agent_proxy_ip"] == "10.100.1.3"
        assert ips["proxy_core_ip"] == "10.100.1.254"
        assert ips["mcp_firecrawl_proxy_ip"] == "10.100.1.55"
        # dns_net = 10.100.2.0/24
        assert ips["coredns_dns_ip"] == "10.100.2.53"
        assert ips["dnsdist_dns_ip"] == "10.100.2.56"
        assert ips["firecrawl_dns_ip"] == "10.100.2.55"
        # egress_net = 10.100.3.0/24
        assert ips["coredns_egress_ip"] == "10.100.3.53"
        # ipc_net = 10.100.4.0/24
        assert ips["core_ipc_ip"] == "10.100.4.3"
        assert ips["admin_ipc_ip"] == "10.100.4.2"

    def test_deterministic_across_calls(self) -> None:
        """Same base_index produces identical IPs across calls."""
        ips1 = derive_static_ips(0)
        ips2 = derive_static_ips(0)
        assert ips1 == ips2

    def test_deterministic_across_restarts(self) -> None:
        """Same base_index → same IP set on successive (simulated) restarts."""
        # Multiple base_index values, each must be stable
        for bi in (0, 1, 42, 7986):
            assert derive_static_ips(bi) == derive_static_ips(bi)


class TestPeekNextSlot:
    """peek_next_slot read-only slot preview."""

    def test_peek_new_project_returns_lowest_slot(self, ledger: IPAMLedger) -> None:
        """New instance: returns (0, False) — lowest available, not existing."""
        slot, is_existing = ledger.peek_next_slot("new-instance")
        assert slot == 0
        assert is_existing is False

    def test_peek_existing_project_returns_existing(self, ledger: IPAMLedger) -> None:
        """Existing instance: returns (allocated_slot, True)."""
        ledger.allocate("existing-instance")  # slot 0
        slot, is_existing = ledger.peek_next_slot("existing-instance")
        assert slot == 0
        assert is_existing is True

    def test_peek_does_not_write(self, ledger: IPAMLedger) -> None:
        """Peek is read-only — no mutations to ledger file."""
        ledger.peek_next_slot("ghost-instance")
        # A subsequent allocate should still get slot 0
        idx = ledger.allocate("ghost-instance")
        assert idx == 0

    def test_peek_with_gaps(self, ledger: IPAMLedger) -> None:
        """Peek finds lowest available slot with gaps."""
        ledger.allocate("p1")  # 0
        ledger.allocate("p2")  # 1
        ledger.allocate("p3")  # 2
        ledger.release("p2")  # free slot 1
        slot, is_existing = ledger.peek_next_slot("new-instance")
        assert slot == 1
        assert is_existing is False

    def test_peek_exhausted_raises(self, isolated_sandbox_ai_home: Path) -> None:
        """IPAMExhaustedError raised when all 7987 slots consumed."""
        ledger_path = _ledger_file(isolated_sandbox_ai_home)
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        data = {f"p{i}": i for i in range(7987)}
        ledger_path.write_text(json.dumps(data))
        ledger = IPAMLedger()
        with pytest.raises(IPAMExhaustedError):
            ledger.peek_next_slot("one-more")


class TestIpamEndToEnd:
    """End-to-end IPAM integration — subnet count, IP count, IP-within-subnet containment."""

    def test_derive_subnets_returns_5(self) -> None:
        """derive_subnets(0) produces exactly 5 subnets."""
        subnets = derive_subnets(0)
        assert len(subnets) == 5

    def test_derive_static_ips_returns_13(self) -> None:
        """derive_static_ips(0) produces exactly 13 IP keys."""
        ips = derive_static_ips(0)
        assert len(ips) == 13

    def test_all_ips_within_expected_subnets(self) -> None:
        """Every static IP falls within its expected subnet."""
        import ipaddress

        subnets = derive_subnets(0)
        (
            isolated,
            core_proxy,
            dns,
            egress,
            ipc,
        ) = subnets
        ips = derive_static_ips(0)

        # Map each IP key to its expected subnet
        ip_subnet_map = {
            "agent_isolated_ip": isolated,
            "agent_proxy_ip": core_proxy,
            "proxy_core_ip": core_proxy,
            "dnsdist_isolated_ip": isolated,
            "dnsdist_dns_ip": dns,
            "coredns_dns_ip": dns,
            "coredns_egress_ip": egress,
            "db_postgres_ip": isolated,
            "mcp_firecrawl_proxy_ip": core_proxy,
            "firecrawl_dns_ip": dns,
            "core_ipc_ip": ipc,
            "admin_ipc_ip": ipc,
            "firecrawl_isolated_ip": isolated,
        }

        for ip_key, subnet_cidr in ip_subnet_map.items():
            net = ipaddress.ip_network(subnet_cidr, strict=False)
            addr = ipaddress.ip_address(ips[ip_key])
            assert addr in net, f"{ip_key}={ips[ip_key]} not in {subnet_cidr}"


class TestIpamQuintuple:
    """Admin-reframe IPAM quintuple: 5-subnet allocation with ipc_net."""

    def test_derive_subnets_returns_5_tuple(self) -> None:
        """derive_subnets(0) returns a 5-tuple with ipc_net as the 5th element."""
        result = derive_subnets(0)
        assert len(result) == 5
        assert result[4] == "10.100.4.0/24"

    def test_derive_subnets_boundary_slot_7986(self) -> None:
        """derive_subnets(7986) returns 5 valid subnets without raising."""
        result = derive_subnets(7986)
        assert len(result) == 5
        # Verify each subnet is a valid /24
        for subnet in result:
            assert subnet.endswith(".0/24")

    def test_derive_subnets_slot_7987_raises(self) -> None:
        """derive_subnets(7987) raises ValueError — exceeds new MAX_SLOTS."""
        with pytest.raises(ValueError):
            derive_subnets(7987)

    def test_derive_static_ips_includes_ipc_keys(self) -> None:
        """derive_static_ips(0) includes core_ipc_ip, admin_ipc_ip, firecrawl_isolated_ip."""
        ips = derive_static_ips(0)
        assert "core_ipc_ip" in ips
        assert "admin_ipc_ip" in ips
        assert "firecrawl_isolated_ip" in ips

    def test_admin_ipc_ip_value(self) -> None:
        """admin_ipc_ip is <ipc_base>.2 per spec scenario."""
        ips = derive_static_ips(0)
        assert ips["admin_ipc_ip"] == "10.100.4.2"

    def test_derive_static_ips_excludes_legacy_keys(self) -> None:
        """derive_static_ips must not contain removed legacy keys."""
        ips = derive_static_ips(0)
        assert "dns_sidecar_ip" not in ips
        assert "admin_isolated_ip" not in ips
        assert "mcp_firecrawl_isolated_ip" not in ips
        assert "proxy_ip" not in ips

    def test_max_slots_is_7987(self) -> None:
        """MAX_SLOTS is 7987 for the 5-subnet-per-instance model."""
        assert MAX_SLOTS == 7987

    def test_ipam_exhausted_at_7987(self, isolated_sandbox_ai_home: Path) -> None:
        """IPAMExhaustedError raised when all 7987 slots consumed."""
        ledger_path = _ledger_file(isolated_sandbox_ai_home)
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        data = {f"p{i}": i for i in range(7987)}
        ledger_path.write_text(json.dumps(data))

        ledger = IPAMLedger()
        with pytest.raises(IPAMExhaustedError, match="sandbox destroy"):
            ledger.allocate("one-more-instance")
