"""Tests for core/scaffold.py — instance directory scaffolding."""


import os
import stat
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from core.scaffold import (
    apply_default_acls,
    create_env_file,
    create_instance_dirs,
    prompt_secrets,
    write_initialized_sentinel,
    write_sandbox_toml,
)

# ─── Expected directory tree (S1 from design spec) ───────────────────────────

EXPECTED_DIRS = [
    "docker/core",
    "docker/admin",
    "docker/extras",
    "config/admin",
    "config/core",
    "config/dns-sidecar",
    "config/proxy",
    "log/admin",
    "log/core",
    "log/proxy",
    "log/orchestrator",
    "cache/.claude",
    "custom/config/admin",
    "custom/config/core",
]


class TestCreateInstanceDirs:
    def test_creates_full_tree(self, tmp_path: Path) -> None:
        """All required subdirectories are created."""
        instance_dir = tmp_path / "sandboxes" / "myproject-abc123"
        create_instance_dirs(str(instance_dir))

        for rel in EXPECTED_DIRS:
            assert (instance_dir / rel).is_dir(), f"Missing dir: {rel}"

    def test_idempotent(self, tmp_path: Path) -> None:
        """Calling twice does not raise."""
        instance_dir = tmp_path / "sandboxes" / "myproject-abc123"
        create_instance_dirs(str(instance_dir))
        create_instance_dirs(str(instance_dir))  # Should not raise


class TestWriteSandboxToml:
    def test_generates_valid_toml(self, tmp_path: Path) -> None:
        """sandbox.toml is written with auto-derived defaults."""
        instance_dir = tmp_path / "sandboxes" / "myproject-abc123"
        instance_dir.mkdir(parents=True)

        write_sandbox_toml(
            instance_dir=str(instance_dir),
            project_name="myproject",
            project_dir="/home/dev/myproject",
            host_unprivileged_user="sandbox",
        )

        toml_path = instance_dir / "sandbox.toml"
        assert toml_path.exists()
        content = toml_path.read_text()

        # Verify key fields present
        assert 'name = "myproject"' in content
        assert 'user_project_root = "/home/dev/myproject"' in content
        assert 'host_unprivileged_user = "sandbox"' in content
        assert "host_uid" in content

    def test_host_uid_auto_detected(self, tmp_path: Path) -> None:
        """host_uid matches the current process UID."""
        instance_dir = tmp_path / "sandboxes" / "myproject-abc123"
        instance_dir.mkdir(parents=True)

        write_sandbox_toml(
            instance_dir=str(instance_dir),
            project_name="myproject",
            project_dir="/home/dev/myproject",
            host_unprivileged_user="sandbox",
        )

        content = (instance_dir / "sandbox.toml").read_text()
        assert f'host_uid = "{os.getuid()}"' in content


class TestCreateEnvFile:
    def test_mode_0600(self, tmp_path: Path) -> None:
        """Env file is created with mode 0600 (owner-read-write only)."""
        env_path = tmp_path / ".sandbox.env"
        create_env_file(str(env_path), db_postgres=True, mcp_firecrawl=False)

        file_stat = os.stat(str(env_path))
        mode = stat.S_IMODE(file_stat.st_mode)
        assert mode == 0o600, f"Expected 0600, got {oct(mode)}"

    def test_core_secrets_always_present(self, tmp_path: Path) -> None:
        """Core container secrets are always written."""
        env_path = tmp_path / ".sandbox.env"
        create_env_file(str(env_path), db_postgres=False, mcp_firecrawl=False)

        content = env_path.read_text()
        assert "CORE_ANTHROPIC_API_KEY" in content
        assert "CORE_GITHUB_TOKEN" in content

    def test_postgres_conditional(self, tmp_path: Path) -> None:
        """PG_* secrets present only when db_postgres=True."""
        env_on = tmp_path / "env_on"
        create_env_file(str(env_on), db_postgres=True, mcp_firecrawl=False)
        assert "PG_USER" in env_on.read_text()
        assert "PG_PASSWORD" in env_on.read_text()

        env_off = tmp_path / "env_off"
        create_env_file(str(env_off), db_postgres=False, mcp_firecrawl=False)
        assert "PG_USER" not in env_off.read_text()

    def test_firecrawl_conditional(self, tmp_path: Path) -> None:
        """FIRECRAWL_API_KEY present only when mcp_firecrawl=True."""
        env_on = tmp_path / "env_on"
        create_env_file(str(env_on), db_postgres=False, mcp_firecrawl=True)
        assert "FIRECRAWL_API_KEY" in env_on.read_text()

        env_off = tmp_path / "env_off"
        create_env_file(str(env_off), db_postgres=False, mcp_firecrawl=False)
        assert "FIRECRAWL_API_KEY" not in env_off.read_text()

    def test_exclusive_create_prevents_overwrite(self, tmp_path: Path) -> None:
        """Second creation raises FileExistsError (O_EXCL semantics)."""
        env_path = tmp_path / ".sandbox.env"
        create_env_file(str(env_path), db_postgres=False, mcp_firecrawl=False)

        with pytest.raises(FileExistsError):
            create_env_file(str(env_path), db_postgres=False, mcp_firecrawl=False)


class TestApplyDefaultAcls:
    @patch("subprocess.run")
    def test_calls_setfacl_on_required_dirs(self, mock_run: MagicMock) -> None:
        """setfacl -d -m called on log/, cache/, and user_project_root."""
        mock_run.return_value = MagicMock(returncode=0)

        apply_default_acls(
            instance_dir="/sandboxes/myproject-abc123",
            user_project_root="/home/dev/myproject",
            dev_user="dev",
        )

        expected_calls = [
            call(
                ["setfacl", "-d", "-m", "u:dev:rwx", "/sandboxes/myproject-abc123/log/"],
                check=True,
            ),
            call(
                ["setfacl", "-d", "-m", "u:dev:rwx", "/sandboxes/myproject-abc123/cache/"],
                check=True,
            ),
            call(
                ["setfacl", "-d", "-m", "u:dev:rwx", "/home/dev/myproject"],
                check=True,
            ),
        ]
        mock_run.assert_has_calls(expected_calls, any_order=False)


class TestPromptSecrets:
    @patch("sys.stdin")
    @patch("getpass.getpass")
    def test_interactive_tty_prompts(
        self, mock_getpass: MagicMock, mock_stdin: MagicMock, tmp_path: Path
    ) -> None:
        """In TTY mode, prompts for each required secret and writes to env file."""
        mock_stdin.isatty.return_value = True
        mock_getpass.side_effect = ["sk-ant-xxx", "ghp_yyy"]

        env_path = tmp_path / ".sandbox.env"
        env_path.write_text('CORE_ANTHROPIC_API_KEY=""\nCORE_GITHUB_TOKEN=""\n')

        required_secrets = [
            ("CORE_ANTHROPIC_API_KEY", "Anthropic API key"),
            ("CORE_GITHUB_TOKEN", "GitHub personal access token"),
        ]
        prompt_secrets(str(env_path), required_secrets)

        content = env_path.read_text()
        assert 'CORE_ANTHROPIC_API_KEY="sk-ant-xxx"' in content
        assert 'CORE_GITHUB_TOKEN="ghp_yyy"' in content

    @patch("sys.stdin")
    def test_no_tty_raises(self, mock_stdin: MagicMock, tmp_path: Path) -> None:
        """Non-TTY context raises RuntimeError with env file path."""
        mock_stdin.isatty.return_value = False

        env_path = tmp_path / ".sandbox.env"
        env_path.write_text('CORE_ANTHROPIC_API_KEY=""\n')

        with pytest.raises(RuntimeError, match=str(env_path)):
            prompt_secrets(str(env_path), [("CORE_ANTHROPIC_API_KEY", "key")])


class TestWriteInitializedSentinel:
    def test_sentinel_written(self, tmp_path: Path) -> None:
        """`.initialized` sentinel file is created in instance_dir."""
        instance_dir = tmp_path / "sandboxes" / "myproject-abc123"
        instance_dir.mkdir(parents=True)

        write_initialized_sentinel(str(instance_dir))

        sentinel = instance_dir / ".initialized"
        assert sentinel.exists()
        assert sentinel.read_text() == ""

    def test_sentinel_idempotent(self, tmp_path: Path) -> None:
        """Writing sentinel twice does not raise."""
        instance_dir = tmp_path / "sandboxes" / "myproject-abc123"
        instance_dir.mkdir(parents=True)

        write_initialized_sentinel(str(instance_dir))
        write_initialized_sentinel(str(instance_dir))  # Should not raise
