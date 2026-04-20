"""Tests for the IPAM /24-triple allocator with slot reuse and overflow detection."""

import json

import pytest
from core.ipam import (
    IPAMExhaustedError,
    IPAMLedger,
    derive_static_ips,
    derive_subnets,
)


@pytest.fixture
def ledger(tmp_path: object) -> IPAMLedger:
    """Create an IPAM ledger backed by a temporary file."""
    ledger_path = str(tmp_path) + "/ipam.json"
    return IPAMLedger(ledger_path)


class TestIPAMLedger:
    def test_allocate_lowest_slot(self, ledger: IPAMLedger) -> None:
        """First allocation gets base_index 0."""
        idx = ledger.allocate("project-aaa")
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
        """Re-allocating the same project_id returns the same base_index."""
        idx1 = ledger.allocate("project-aaa")
        idx2 = ledger.allocate("project-aaa")
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
        """Releasing a non-existent project_id does not raise."""
        ledger.release("nonexistent")

    def test_overflow_detection(self, tmp_path: object) -> None:
        """IPAMExhaustedError raised when all 13312 slots consumed."""
        ledger_path = str(tmp_path) + "/ipam.json"
        # Pre-fill ledger with all slots
        data = {f"p{i}": i for i in range(13312)}
        with open(ledger_path, "w") as f:
            json.dump(data, f)

        ledger = IPAMLedger(ledger_path)
        with pytest.raises(IPAMExhaustedError, match="sandbox destroy"):
            ledger.allocate("one-more-project")

    def test_corrupt_json_recovers(self, tmp_path: object) -> None:
        """Corrupt JSON ledger is treated as empty."""
        ledger_path = str(tmp_path) + "/ipam.json"
        with open(ledger_path, "w") as f:
            f.write("{ bad json }")
        ledger = IPAMLedger(ledger_path)
        idx = ledger.allocate("project-aaa")
        assert idx == 0

    def test_lock_contention_raises(self, tmp_path: object) -> None:
        """IPAMLockException raised when lock is already held."""
        from unittest.mock import patch

        from core.ipam import IPAMLockException

        ledger_path = str(tmp_path) + "/ipam.json"
        ledger = IPAMLedger(ledger_path)

        with (
            patch("fcntl.flock", side_effect=BlockingIOError(11, "Resource temporarily unavailable")),
            pytest.raises(IPAMLockException, match="Could not acquire IPAM lock"),
        ):
                ledger.allocate("project-aaa")

class TestDeriveSubnets:
    def test_base_index_zero(self) -> None:
        """base_index=0 → g=0: 10.100.0.0/24, 10.100.1.0/24, 10.100.2.0/24."""
        isolated, proxy, egress = derive_subnets(0)
        assert isolated == "10.100.0.0/24"
        assert proxy == "10.100.1.0/24"
        assert egress == "10.100.2.0/24"

    def test_base_index_max(self) -> None:
        """base_index=13311 → g=39933: verify correct subnet derivation."""
        g = 13311 * 3
        expected_isolated = f"10.{100 + g // 256}.{g % 256}.0/24"
        g1 = g + 1
        expected_proxy = f"10.{100 + g1 // 256}.{g1 % 256}.0/24"
        g2 = g + 2
        expected_egress = f"10.{100 + g2 // 256}.{g2 % 256}.0/24"

        isolated, proxy, egress = derive_subnets(13311)
        assert isolated == expected_isolated
        assert proxy == expected_proxy
        assert egress == expected_egress

    def test_base_index_one(self) -> None:
        """base_index=1 → g=3: 10.100.3.0/24, 10.100.4.0/24, 10.100.5.0/24."""
        isolated, proxy, egress = derive_subnets(1)
        assert isolated == "10.100.3.0/24"
        assert proxy == "10.100.4.0/24"
        assert egress == "10.100.5.0/24"


class TestDeriveStaticIPs:
    def test_output_keys(self) -> None:
        """derive_static_ips returns all required IP keys."""
        ips = derive_static_ips(0)
        expected_keys = {
            "dns_sidecar_ip",
            "db_postgres_ip",
            "agent_isolated_ip",
            "admin_isolated_ip",
            "proxy_ip",
            "agent_proxy_ip",
            "admin_proxy_ip",
        }
        assert set(ips.keys()) == expected_keys

    def test_values_at_index_zero(self) -> None:
        """Verify specific IP values at base_index=0."""
        ips = derive_static_ips(0)
        assert ips["dns_sidecar_ip"] == "10.100.0.53"
        assert ips["db_postgres_ip"] == "10.100.0.54"
        assert ips["agent_isolated_ip"] == "10.100.0.3"
        assert ips["admin_isolated_ip"] == "10.100.0.2"
        assert ips["proxy_ip"] == "10.100.1.254"
        assert ips["agent_proxy_ip"] == "10.100.1.3"
        assert ips["admin_proxy_ip"] == "10.100.1.2"


class TestPeekNextSlot:
    """Task 11.1: peek_next_slot read-only slot preview."""

    def test_peek_new_project_returns_lowest_slot(self, ledger: IPAMLedger) -> None:
        """New project: returns (0, False) — lowest available, not existing."""
        slot, is_existing = ledger.peek_next_slot("new-project")
        assert slot == 0
        assert is_existing is False

    def test_peek_existing_project_returns_existing(self, ledger: IPAMLedger) -> None:
        """Existing project: returns (allocated_slot, True)."""
        ledger.allocate("existing-project")  # slot 0
        slot, is_existing = ledger.peek_next_slot("existing-project")
        assert slot == 0
        assert is_existing is True

    def test_peek_does_not_write(self, ledger: IPAMLedger) -> None:
        """Peek is read-only — no mutations to ledger file."""
        ledger.peek_next_slot("ghost-project")
        # A subsequent allocate should still get slot 0
        idx = ledger.allocate("ghost-project")
        assert idx == 0

    def test_peek_with_gaps(self, ledger: IPAMLedger) -> None:
        """Peek finds lowest available slot with gaps."""
        ledger.allocate("p1")  # 0
        ledger.allocate("p2")  # 1
        ledger.allocate("p3")  # 2
        ledger.release("p2")  # free slot 1
        slot, is_existing = ledger.peek_next_slot("new-project")
        assert slot == 1
        assert is_existing is False

    def test_peek_exhausted_raises(self, tmp_path: object) -> None:
        """IPAMExhaustedError raised when all slots consumed."""
        ledger_path = str(tmp_path) + "/ipam.json"
        data = {f"p{i}": i for i in range(13312)}
        with open(ledger_path, "w") as f:
            json.dump(data, f)
        ledger = IPAMLedger(ledger_path)
        with pytest.raises(IPAMExhaustedError):
            ledger.peek_next_slot("one-more")

