# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the subid free-block allocator + overlap helper."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.setup import subid

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class TestConstants:
    def test_min_floor_at_most_alloc_size(self) -> None:
        # The documented invariant: the validation floor never exceeds the
        # allocation size (a freshly minted block always clears the floor).
        assert subid.MIN_SUBID_RANGE <= subid.SUBID_RANGE_SIZE

    def test_base_and_size_values(self) -> None:
        assert subid.SUBID_BASE == 100000
        assert subid.SUBID_RANGE_SIZE == 65536
        assert subid.MIN_SUBID_RANGE == 65536


class TestRangesOverlap:
    def test_disjoint_below(self) -> None:
        assert not subid.ranges_overlap(0, 10, 10, 10)

    def test_disjoint_above(self) -> None:
        assert not subid.ranges_overlap(10, 10, 0, 10)

    def test_touching_is_not_overlap(self) -> None:
        # Half-open: [0,10) and [10,20) share no element.
        assert not subid.ranges_overlap(0, 10, 10, 5)

    def test_one_inside_other(self) -> None:
        assert subid.ranges_overlap(0, 100, 50, 10)

    def test_partial_overlap(self) -> None:
        assert subid.ranges_overlap(0, 60, 50, 60)


class TestPickFreeBlock:
    def test_free_at_base_when_no_existing(self) -> None:
        start, size = subid.pick_free_subid_block(existing=[])
        assert (start, size) == (100000, 65536)

    def test_occupied_base_shifts_up(self) -> None:
        # Another user holds 100000:65536 → picker shifts to the next block.
        start, size = subid.pick_free_subid_block(existing=[(100000, 65536)])
        assert (start, size) == (165536, 65536)

    def test_fedora_base_free_returns_base(self) -> None:
        # Fedora ships an existing entry at 524288:65536 (well above the seed),
        # leaving the seed free → the picker returns SUBID_BASE, proving the
        # base is a SCAN SEED, not an assumption that subids start at 100000.
        start, size = subid.pick_free_subid_block(existing=[(524288, 65536)])
        assert (start, size) == (100000, 65536)

    def test_skips_multiple_occupied_blocks(self) -> None:
        existing = [(100000, 65536), (165536, 65536)]
        start, _ = subid.pick_free_subid_block(existing=existing)
        assert start == 231072

    def test_partial_overlap_of_a_block_is_avoided(self) -> None:
        # An entry straddling the base block forces a shift even though it does
        # not align to a SUBID_RANGE_SIZE boundary.
        start, _ = subid.pick_free_subid_block(existing=[(120000, 1000)])
        assert start == 165536


class TestReadAllSubidRanges:
    def test_reads_all_users_from_both_files(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        subuid = tmp_path / "subuid"
        subgid = tmp_path / "subgid"
        subuid.write_text("alice:100000:65536\nbob:165536:65536\n")
        # A bare line (no colons) → skipped by the field-count guard; a
        # 3-field line with a non-integer count → skipped by the ValueError
        # guard. Both are tolerated, leaving one well-formed subgid entry.
        subgid.write_text(
            "alice:100000:65536\n# comment\nmalformed-line\ncarol:abc:def\n"
        )
        monkeypatch.setattr(subid, "_SUBUID_PATH", subuid)
        monkeypatch.setattr(subid, "_SUBGID_PATH", subgid)

        ranges = subid.read_all_subid_ranges()

        assert (100000, 65536) in ranges
        assert (165536, 65536) in ranges
        # subuid (2 entries) + subgid (1 well-formed) = 3 total.
        assert len(ranges) == 3

    def test_missing_files_yield_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(subid, "_SUBUID_PATH", tmp_path / "nope-uid")
        monkeypatch.setattr(subid, "_SUBGID_PATH", tmp_path / "nope-gid")
        assert subid.read_all_subid_ranges() == []

    def test_by_user_reader_preserves_user_label(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        subuid = tmp_path / "subuid"
        subgid = tmp_path / "subgid"
        # Two DISTINCT users hold the IDENTICAL (100000, 65536) value — the
        # by-user reader MUST keep both, labelled, so cross-user overlap
        # detection can tell them apart (a value-only view would collapse them).
        subuid.write_text("alice:100000:65536\nbob:100000:65536\n")
        subgid.write_text("alice:100000:65536\n")
        monkeypatch.setattr(subid, "_SUBUID_PATH", subuid)
        monkeypatch.setattr(subid, "_SUBGID_PATH", subgid)

        by_user = subid.read_all_subid_ranges_by_user()

        assert ("alice", 100000, 65536) in by_user
        assert ("bob", 100000, 65536) in by_user
        # 2 subuid entries + 1 subgid entry, both alice-at-100000 retained.
        assert len(by_user) == 3
        # The value-only view drops the label but keeps every occurrence.
        assert subid.read_all_subid_ranges().count((100000, 65536)) == 3

    def test_picker_defaults_to_live_reader(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        subuid = tmp_path / "subuid"
        subgid = tmp_path / "subgid"
        subuid.write_text("alice:100000:65536\n")
        subgid.write_text("alice:100000:65536\n")
        monkeypatch.setattr(subid, "_SUBUID_PATH", subuid)
        monkeypatch.setattr(subid, "_SUBGID_PATH", subgid)

        # No explicit ``existing`` → picker consults read_all_subid_ranges.
        start, _ = subid.pick_free_subid_block()
        assert start == 165536
