from __future__ import annotations

import os

import pytest
from core.host_resources import host_cpu_count, host_ram_bytes, parse_docker_size


def test_host_cpu_count_uses_cpu_count_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """cpu_count() is the primary source and must not consult affinity."""
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    monkeypatch.setattr(
        os,
        "sched_getaffinity",
        lambda _pid: (_ for _ in ()).throw(AssertionError("affinity must not be consulted")),
    )

    assert host_cpu_count() == 8


def test_host_cpu_count_falls_back_to_affinity_when_cpu_count_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Affinity is the fallback only when cpu_count() returns None."""
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: {0, 1, 2})

    assert host_cpu_count() == 3


def test_host_cpu_count_floors_at_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never return 0/None even if both sources report nothing usable."""
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: set())

    assert host_cpu_count() == 1


def test_host_ram_bytes_is_positive() -> None:
    assert host_ram_bytes() > 0


def test_host_ram_bytes_is_page_size_times_phys_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assert the exact formula: SC_PAGE_SIZE * SC_PHYS_PAGES."""
    values = {"SC_PAGE_SIZE": 4096, "SC_PHYS_PAGES": 100}
    monkeypatch.setattr(os, "sysconf", lambda name: values[name])

    assert host_ram_bytes() == 4096 * 100


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("8gb", 8589934592),
        ("512m", 536870912),
        ("2g", 2147483648),
        ("1048576", 1048576),
        ("8GB", 8589934592),
        ("4k", 4096),
        ("1kb", 1024),
        (1048576, 1048576),
    ],
)
def test_parse_docker_size_valid(value: int | str, expected: int) -> None:
    assert parse_docker_size(value) == expected


@pytest.mark.parametrize("value", ["abc", "", "1.5g", "g", "12x"])
def test_parse_docker_size_rejects_garbage(value: str) -> None:
    with pytest.raises(ValueError, match="parse"):
        parse_docker_size(value)
