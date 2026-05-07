"""Tests for in_container_uid_for_host_uid (inverse of host_id_for_in_container)."""

from pathlib import Path

import pytest
from core.host_config import (
    NoSubuidRangeError,
    SubuidOutOfRangeError,
    in_container_uid_for_host_uid,
)


def _patch_subuid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str) -> Path:
    f = tmp_path / "subuid"
    f.write_text(body)
    monkeypatch.setattr("core.host_config._SUBUID_PATH", f)
    return f


class TestInContainerUidForHostUid:
    def test_in_range_translates_to_one_based_offset(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_subuid(monkeypatch, tmp_path, "claude-sandbox:165536:65536\n")
        assert in_container_uid_for_host_uid(166535, "claude-sandbox") == 1000
        assert in_container_uid_for_host_uid(165536, "claude-sandbox") == 1

    def test_below_range_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch_subuid(monkeypatch, tmp_path, "claude-sandbox:165536:65536\n")
        with pytest.raises(SubuidOutOfRangeError):
            in_container_uid_for_host_uid(100, "claude-sandbox")

    def test_above_range_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch_subuid(monkeypatch, tmp_path, "claude-sandbox:165536:65536\n")
        with pytest.raises(SubuidOutOfRangeError):
            in_container_uid_for_host_uid(165536 + 65536, "claude-sandbox")

    def test_multi_range_second_range(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch_subuid(monkeypatch, tmp_path, "u:1000:5\nu:9000:5\n")
        # 1000 → 1, 1004 → 5, 9000 → 6, 9004 → 10
        assert in_container_uid_for_host_uid(1000, "u") == 1
        assert in_container_uid_for_host_uid(1004, "u") == 5
        assert in_container_uid_for_host_uid(9000, "u") == 6
        assert in_container_uid_for_host_uid(9004, "u") == 10

    def test_primary_uid_not_special_cased(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Daemon user's primary uid lies outside the subuid range; the inverse
        # MUST raise rather than return 0 (asymmetry with host_id_for_in_container).
        _patch_subuid(monkeypatch, tmp_path, "claude-sandbox:165536:65536\n")
        primary_uid = 1001  # outside the 165536..231071 subuid range
        with pytest.raises(SubuidOutOfRangeError):
            in_container_uid_for_host_uid(primary_uid, "claude-sandbox")

    def test_no_subuid_entry_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_subuid(monkeypatch, tmp_path, "other:1:1\n")
        with pytest.raises(NoSubuidRangeError):
            in_container_uid_for_host_uid(166535, "claude-sandbox")
