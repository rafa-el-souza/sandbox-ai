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
    "config/coredns",
    "config/dnsdist",
    "config/proxy",
    "log/admin",
    "log/core",
    "log/proxy",
    "log/orchestrator",
    "cache/core/.claude",
    "cache/admin/tmux_resurrect",
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
            instance_name="myproject",
            project_dir="/home/dev/myproject",
        )

        toml_path = instance_dir / "sandbox.toml"
        assert toml_path.exists()
        content = toml_path.read_text()

        # Verify key fields present
        assert 'name = "myproject"' in content
        assert 'user_project_root = "/home/dev/myproject"' in content
        assert "host_unprivileged_user" not in content
        assert "host_uid" in content

    def test_host_uid_auto_detected(self, tmp_path: Path) -> None:
        """host_uid matches the current process UID."""
        instance_dir = tmp_path / "sandboxes" / "myproject-abc123"
        instance_dir.mkdir(parents=True)

        write_sandbox_toml(
            instance_dir=str(instance_dir),
            instance_name="myproject",
            project_dir="/home/dev/myproject",
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

    def test_postgres_pg_user_default(self, tmp_path: Path) -> None:
        """Task 3.2: PG_USER defaults to 'sandbox' (aligned with DbPostgresConfig)."""
        env_path = tmp_path / "env_pg_user"
        create_env_file(str(env_path), db_postgres=True, mcp_firecrawl=False)
        content = env_path.read_text()
        assert 'PG_USER="sandbox"' in content

    def test_postgres_pg_password_auto_generated(self, tmp_path: Path) -> None:
        """Task 5.1: PG_PASSWORD is auto-generated with a non-empty value."""
        env_path = tmp_path / "env_pg_auto"
        create_env_file(str(env_path), db_postgres=True, mcp_firecrawl=False)
        content = env_path.read_text()
        # Should contain PG_PASSWORD with a non-empty value
        assert 'PG_PASSWORD=""' not in content
        # Extract the value
        for line in content.splitlines():
            if line.startswith("PG_PASSWORD="):
                val = line.split("=", 1)[1].strip('"')
                assert len(val) >= 32, f"PG_PASSWORD too short: {val}"
                break
        else:
            pytest.fail("PG_PASSWORD not found in env file")

    def test_postgres_pg_password_non_empty(self, tmp_path: Path) -> None:
        """Task 5.6: Existing test updated — value is non-empty."""
        env_path = tmp_path / "env_pg_ne"
        create_env_file(str(env_path), db_postgres=True, mcp_firecrawl=False)
        content = env_path.read_text()
        assert "PG_PASSWORD" in content
        assert 'PG_PASSWORD=""' not in content

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
    def test_interactive_tty_prompts(self, mock_stdin: MagicMock, tmp_path: Path) -> None:
        """In TTY mode, prompts for each required secret and writes to env file."""
        mock_stdin.isatty.return_value = True
        mock_prompt = MagicMock(side_effect=["sk-ant-xxx", "ghp_yyy"])

        env_path = tmp_path / ".sandbox.env"
        env_path.write_text('CORE_ANTHROPIC_API_KEY=""\nCORE_GITHUB_TOKEN=""\n')

        required_secrets = [
            ("CORE_ANTHROPIC_API_KEY", "Anthropic API key"),
            ("CORE_GITHUB_TOKEN", "GitHub personal access token"),
        ]
        prompt_secrets(str(env_path), required_secrets, mock_prompt)

        content = env_path.read_text()
        assert 'CORE_ANTHROPIC_API_KEY="sk-ant-xxx"' in content
        assert 'CORE_GITHUB_TOKEN="ghp_yyy"' in content

    @patch("sys.stdin")
    def test_no_tty_prints_guidance(self, mock_stdin: MagicMock, tmp_path: Path) -> None:
        """Non-TTY context prints guidance instead of raising."""
        mock_stdin.isatty.return_value = False

        env_path = tmp_path / ".sandbox.env"
        env_path.write_text('CORE_ANTHROPIC_API_KEY=""\n')

        # Should not raise — prints guidance instead
        prompt_secrets(str(env_path), [("CORE_ANTHROPIC_API_KEY", "key")], MagicMock())


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


# ─── Task 2.1: write_sandbox_toml with git_user/git_email ────────────────────


class TestWriteSandboxTomlGitFields:
    """Task 2.1: git_user/git_email params interpolated into sandbox.toml."""

    def test_git_user_interpolated(self, tmp_path: Path) -> None:
        """git_user value is written into sandbox.toml."""
        instance_dir = tmp_path / "sandboxes" / "test"
        instance_dir.mkdir(parents=True)

        write_sandbox_toml(
            instance_dir=str(instance_dir),
            instance_name="test",
            project_dir="/dev/test",
            git_user="Jane Doe",
            git_email="jane@example.com",
        )

        content = (instance_dir / "sandbox.toml").read_text()
        assert 'git_user = "Jane Doe"' in content
        assert 'git_email = "jane@example.com"' in content

    def test_git_fields_default_empty(self, tmp_path: Path) -> None:
        """git_user/git_email default to empty strings when not provided."""
        instance_dir = tmp_path / "sandboxes" / "test"
        instance_dir.mkdir(parents=True)

        write_sandbox_toml(
            instance_dir=str(instance_dir),
            instance_name="test",
            project_dir="/dev/test",
        )

        content = (instance_dir / "sandbox.toml").read_text()
        assert 'git_user = ""' in content
        assert 'git_email = ""' in content


# ─── Task 2.2: _detect_git_config ────────────────────────────────────────────


class TestDetectGitConfig:
    """Task 2.2: auto-detect git user.name and user.email from global config."""

    def test_git_config_detected(self) -> None:
        """Returns (name, email) when git config is available."""
        from unittest.mock import patch

        from core.scaffold import _detect_git_config

        def mock_run(cmd: list[str], **kwargs: object) -> object:
            import subprocess

            if "user.name" in cmd:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="Jane Doe\n", stderr="")
            if "user.email" in cmd:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="jane@example.com\n", stderr="")
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")

        with patch("subprocess.run", side_effect=mock_run):
            name, email = _detect_git_config()
            assert name == "Jane Doe"
            assert email == "jane@example.com"

    def test_git_not_installed(self) -> None:
        """Returns ('', '') when git is not on PATH."""
        from unittest.mock import patch

        from core.scaffold import _detect_git_config

        with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
            name, email = _detect_git_config()
            assert name == ""
            assert email == ""

    def test_git_config_unset(self) -> None:
        """Returns ('', '') when git config values are unset."""
        import subprocess
        from unittest.mock import patch

        from core.scaffold import _detect_git_config

        mock_result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            name, email = _detect_git_config()
            assert name == ""
            assert email == ""


# ─── Task 2.5: Non-TTY prompt_secrets ─────────────────────────────────────────


class TestPromptSecretsNonTTY:
    """Task 2.5: Non-TTY prompt_secrets skips prompting, prints guidance."""

    def test_non_tty_no_exception(self, tmp_path: Path) -> None:
        """Non-TTY mode does not raise RuntimeError."""
        from unittest.mock import MagicMock, patch

        env_path = tmp_path / ".sandbox.env"
        env_path.write_text('CORE_ANTHROPIC_API_KEY=""\n')

        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = False

        with patch("sys.stdin", mock_stdin):
            # Should not raise — the current code raises RuntimeError,
            # the new implementation should skip and print guidance
            prompt_secrets(str(env_path), [("CORE_ANTHROPIC_API_KEY", "key")], MagicMock())

    def test_non_tty_prints_guidance(self, tmp_path: Path, capsys: object) -> None:
        """Non-TTY mode prints guidance message with env path."""
        from unittest.mock import MagicMock, patch

        env_path = tmp_path / ".sandbox.env"
        env_path.write_text('CORE_ANTHROPIC_API_KEY=""\n')

        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = False

        with patch("sys.stdin", mock_stdin):
            prompt_secrets(str(env_path), [("CORE_ANTHROPIC_API_KEY", "key")], MagicMock())
