import threading

import pytest

from core.registry import InstanceRegistry, generate_instance_id


@pytest.fixture
def registry(tmp_path: object) -> InstanceRegistry:
    """Create a registry backed by a temporary file."""
    registry_path = str(tmp_path) + "/instances.json"  # type: ignore[operator]
    return InstanceRegistry(registry_path)


class TestGenerateInstanceId:
    def test_deterministic_from_path(self) -> None:
        """Instance ID is deterministic for a given absolute path."""
        instance_id = generate_instance_id("/home/dev/myproject")
        assert instance_id == generate_instance_id("/home/dev/myproject")

    def test_format_basename_plus_hash(self) -> None:
        """Instance ID is <basename>-<md5[:6]>."""
        instance_id = generate_instance_id("/home/dev/myproject")
        assert instance_id.startswith("myproject-")
        # Hash portion is exactly 6 hex characters
        hash_part = instance_id.split("-", 1)[1]
        assert len(hash_part) == 6
        assert all(c in "0123456789abcdef" for c in hash_part)

    def test_different_paths_different_ids(self) -> None:
        """Different absolute paths produce different instance IDs."""
        id_a = generate_instance_id("/home/dev/project-a")
        id_b = generate_instance_id("/home/dev/project-b")
        assert id_a != id_b


class TestInstanceRegistry:
    def test_register_and_lookup(self, registry: InstanceRegistry) -> None:
        """Register a project and look it up by path."""
        registry.register("/home/dev/myproject", "myproject-abc123")
        result = registry.lookup("/home/dev/myproject")
        assert result == "myproject-abc123"

    def test_lookup_not_found(self, registry: InstanceRegistry) -> None:
        """Lookup returns None for unregistered paths."""
        result = registry.lookup("/home/dev/nonexistent")
        assert result is None

    def test_remove(self, registry: InstanceRegistry) -> None:
        """Remove clears the registry entry."""
        registry.register("/home/dev/myproject", "myproject-abc123")
        registry.remove("/home/dev/myproject")
        assert registry.lookup("/home/dev/myproject") is None

    def test_remove_nonexistent_is_noop(self, registry: InstanceRegistry) -> None:
        """Removing a non-existent entry does not raise."""
        registry.remove("/home/dev/nonexistent")  # Should not raise

    def test_idempotent_reregister(self, registry: InstanceRegistry) -> None:
        """Re-registering the same path overwrites the instance_id."""
        registry.register("/home/dev/myproject", "myproject-aaa111")
        registry.register("/home/dev/myproject", "myproject-bbb222")
        assert registry.lookup("/home/dev/myproject") == "myproject-bbb222"

    def test_concurrent_write_safety(self, tmp_path: object) -> None:
        """Two threads writing concurrently do not corrupt the registry."""
        registry_path = str(tmp_path) + "/instances.json"  # type: ignore[operator]
        errors: list[Exception] = []

        def writer(project_dir: str, instance_id: str) -> None:
            try:
                reg = InstanceRegistry(registry_path)
                reg.register(project_dir, instance_id)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=writer, args=("/home/dev/p1", "p1-aaa111"))
        t2 = threading.Thread(target=writer, args=("/home/dev/p2", "p2-bbb222"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"Concurrent write errors: {errors}"
        reg = InstanceRegistry(registry_path)
        assert reg.lookup("/home/dev/p1") == "p1-aaa111"
        assert reg.lookup("/home/dev/p2") == "p2-bbb222"

    def test_persistence_across_instances(self, tmp_path: object) -> None:
        """Data persists across InstanceRegistry instances (file-backed)."""
        registry_path = str(tmp_path) + "/instances.json"  # type: ignore[operator]
        reg1 = InstanceRegistry(registry_path)
        reg1.register("/home/dev/myproject", "myproject-abc123")

        reg2 = InstanceRegistry(registry_path)
        assert reg2.lookup("/home/dev/myproject") == "myproject-abc123"
