# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for core.registry — name-keyed instance registry."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from core.registry import (
    InstanceNameInUseError,
    InstanceRegistry,
    RegistryEntry,
    is_path_keyed,
)


@pytest.fixture
def registry(isolated_sandbox_ai_home: Path) -> InstanceRegistry:
    del isolated_sandbox_ai_home
    return InstanceRegistry()


def _registry_file(home: Path) -> Path:
    return home / "state" / "instances.json"


class TestIsPathKeyed:
    def test_detects_path_keyed_legacy_shape(self) -> None:
        assert is_path_keyed({"/home/user/foo": "foo-aaa111"})

    def test_name_keyed_returns_false(self) -> None:
        assert not is_path_keyed({"foo": {"instance_dir": "/x", "created_at": "Z"}})

    def test_empty_returns_false(self) -> None:
        assert not is_path_keyed({})


class TestRegisterAndGet:
    def test_register_returns_entry_with_dir_and_timestamp(self, registry: InstanceRegistry) -> None:
        entry = registry.register("foo", "/x/sandboxes/foo-aaa")
        assert entry.instance_dir == "/x/sandboxes/foo-aaa"
        assert entry.created_at  # ISO timestamp populated

    def test_get_returns_registered_entry(self, registry: InstanceRegistry) -> None:
        registry.register("foo", "/x/foo")
        got = registry.get("foo")
        assert got is not None
        assert got.instance_dir == "/x/foo"

    def test_get_unknown_returns_none(self, registry: InstanceRegistry) -> None:
        assert registry.get("unknown") is None

    def test_register_duplicate_name_raises(self, registry: InstanceRegistry) -> None:
        registry.register("foo", "/x/foo")
        with pytest.raises(InstanceNameInUseError):
            registry.register("foo", "/y/foo")

    def test_register_overwrite_allowed(self, registry: InstanceRegistry) -> None:
        registry.register("foo", "/x/foo")
        registry.register("foo", "/y/foo", allow_overwrite=True)
        got = registry.get("foo")
        assert got is not None
        assert got.instance_dir == "/y/foo"


class TestAll:
    def test_all_returns_dict_of_entries(self, registry: InstanceRegistry) -> None:
        registry.register("foo", "/x/foo")
        registry.register("bar", "/x/bar")
        all_entries = registry.all()
        assert set(all_entries.keys()) == {"foo", "bar"}
        assert all(isinstance(v, RegistryEntry) for v in all_entries.values())

    def test_all_skips_records_missing_required_fields(self, isolated_sandbox_ai_home: Path) -> None:
        path = _registry_file(isolated_sandbox_ai_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"good": {"instance_dir": "/x", "created_at": "Z"}, "bad": {"instance_dir": "/y"}}))
        reg = InstanceRegistry()
        all_entries = reg.all()
        assert "good" in all_entries
        assert "bad" not in all_entries


class TestRemove:
    def test_remove_drops_entry(self, registry: InstanceRegistry) -> None:
        registry.register("foo", "/x/foo")
        registry.remove("foo")
        assert registry.get("foo") is None

    def test_remove_missing_is_noop(self, registry: InstanceRegistry) -> None:
        registry.remove("never-existed")  # must not raise


class TestPersistence:
    def test_persistence_across_instances(self, isolated_sandbox_ai_home: Path) -> None:
        del isolated_sandbox_ai_home
        InstanceRegistry().register("foo", "/x/foo")
        got = InstanceRegistry().get("foo")
        assert got is not None
        assert got.instance_dir == "/x/foo"

    def test_corrupt_json_is_treated_empty(self, isolated_sandbox_ai_home: Path) -> None:
        path = _registry_file(isolated_sandbox_ai_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json !!!")
        reg = InstanceRegistry()
        assert reg.get("anything") is None

    def test_legacy_path_keyed_registry_treated_empty(self, isolated_sandbox_ai_home: Path) -> None:
        path = _registry_file(isolated_sandbox_ai_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"/home/user/foo": "foo-aaa111"}))
        reg = InstanceRegistry()
        # Path-keyed entries are filtered; new lookups by name return None.
        assert reg.get("foo") is None

    def test_non_dict_top_level_treated_empty(self, isolated_sandbox_ai_home: Path) -> None:
        path = _registry_file(isolated_sandbox_ai_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([1, 2, 3]))
        reg = InstanceRegistry()
        assert reg.get("foo") is None

    def test_missing_required_field_in_get(self, isolated_sandbox_ai_home: Path) -> None:
        path = _registry_file(isolated_sandbox_ai_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"foo": {"instance_dir": "/x"}}))
        reg = InstanceRegistry()
        assert reg.get("foo") is None


class TestConcurrency:
    def test_concurrent_writes_dont_corrupt(self, isolated_sandbox_ai_home: Path) -> None:
        """Both writers complete and both entries land in the registry under fcntl serialization."""
        del isolated_sandbox_ai_home
        thread_excs: list[BaseException] = []
        original_hook = threading.excepthook

        def capture(args: threading.ExceptHookArgs) -> None:
            thread_excs.append(args.exc_value if args.exc_value else BaseException("unknown"))

        threading.excepthook = capture
        try:
            t1 = threading.Thread(target=lambda: InstanceRegistry().register("a", "/x/a"))
            t2 = threading.Thread(target=lambda: InstanceRegistry().register("b", "/x/b"))
            t1.start()
            t2.start()
            t1.join()
            t2.join()
        finally:
            threading.excepthook = original_hook

        assert thread_excs == []
        reg = InstanceRegistry()
        assert reg.get("a") is not None
        assert reg.get("b") is not None
