"""Tests for core/host_config.py — HostConfig model and machinectl_cmd builder."""

import os
from pathlib import Path

import pytest
from core.host_config import (
    HostConfig,
    HostSettings,
    MachinectlAuth,
    machinectl_cmd,
    sandbox_ai_user_home,
)
from pydantic import ValidationError


class TestSandboxAiUserHome:
    """sandbox_ai_user_home() canonical-path resolver."""

    def test_env_unset_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With SANDBOX_AI_USER_HOME unset, returns ~/.sandbox-ai."""
        monkeypatch.delenv("SANDBOX_AI_USER_HOME", raising=False)
        result = sandbox_ai_user_home()
        assert result == Path(os.path.expanduser("~/.sandbox-ai"))

    def test_env_set_returns_env_value(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """With SANDBOX_AI_USER_HOME set, returns that path."""
        monkeypatch.setenv("SANDBOX_AI_USER_HOME", str(tmp_path / "custom"))
        result = sandbox_ai_user_home()
        assert result == tmp_path / "custom"

    def test_idempotent(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Repeated calls return equal paths."""
        monkeypatch.setenv("SANDBOX_AI_USER_HOME", str(tmp_path))
        assert sandbox_ai_user_home() == sandbox_ai_user_home()

# ─── Task 1.2: HostConfig.from_toml() ─────────────────────────────────────

VALID_PROJECT_TOML = """\
[host]
docker_unprivileged_user = "sandbox"
machinectl_authentication = "sudo"
"""

VALID_PROJECT_TOML_POLKIT = """\
[host]
docker_unprivileged_user = "sandbox"
machinectl_authentication = "polkit"
"""

VALID_PROJECT_TOML_NO_AUTH = """\
[host]
docker_unprivileged_user = "sandbox"
"""


def _seed_host_config(home: Path, body: str) -> Path:
    """Write `body` to ``<home>/config/sandbox-ai.toml`` and return the file path."""
    config_dir = home / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "sandbox-ai.toml"
    path.write_text(body)
    return path


class TestHostConfigFromToml:
    """HostConfig.from_toml() parsing and validation."""

    def test_valid_config_parsed(self, isolated_sandbox_ai_user_home: Path) -> None:
        """Valid sandbox-ai.toml parses into HostConfig without errors."""
        _seed_host_config(isolated_sandbox_ai_user_home, VALID_PROJECT_TOML)
        config = HostConfig.from_toml()
        assert config.host.docker_unprivileged_user == "sandbox"
        assert config.host.machinectl_authentication == MachinectlAuth.SUDO

    def test_polkit_mode_parsed(self, isolated_sandbox_ai_user_home: Path) -> None:
        """machinectl_authentication = 'polkit' parses to MachinectlAuth.POLKIT."""
        _seed_host_config(isolated_sandbox_ai_user_home, VALID_PROJECT_TOML_POLKIT)
        config = HostConfig.from_toml()
        assert config.host.machinectl_authentication == MachinectlAuth.POLKIT

    def test_missing_file_raises_file_not_found(self, isolated_sandbox_ai_user_home: Path) -> None:
        """Missing sandbox-ai.toml raises FileNotFoundError with canonical path."""
        with pytest.raises(FileNotFoundError, match="Run sandbox init"):
            HostConfig.from_toml()

    def test_missing_required_field_raises_validation_error(self, isolated_sandbox_ai_user_home: Path) -> None:
        """Missing docker_unprivileged_user raises ValidationError."""
        _seed_host_config(isolated_sandbox_ai_user_home, '[host]\nmachinectl_authentication = "sudo"\n')
        with pytest.raises(ValidationError):
            HostConfig.from_toml()

    def test_invalid_enum_value_raises_validation_error(self, isolated_sandbox_ai_user_home: Path) -> None:
        """Invalid machinectl_authentication value raises ValidationError."""
        bad_toml = '[host]\ndocker_unprivileged_user = "sandbox"\nmachinectl_authentication = "pkexec"\n'
        _seed_host_config(isolated_sandbox_ai_user_home, bad_toml)
        with pytest.raises(ValidationError):
            HostConfig.from_toml()

    def test_default_auth_mode_is_sudo(self, isolated_sandbox_ai_user_home: Path) -> None:
        """Omitted machinectl_authentication defaults to 'sudo'."""
        _seed_host_config(isolated_sandbox_ai_user_home, VALID_PROJECT_TOML_NO_AUTH)
        config = HostConfig.from_toml()
        assert config.host.machinectl_authentication == MachinectlAuth.SUDO

    def test_loader_honors_env_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Loader reads from SANDBOX_AI_USER_HOME-resolved path."""
        custom_home = tmp_path / "custom"
        monkeypatch.setenv("SANDBOX_AI_USER_HOME", str(custom_home))
        _seed_host_config(custom_home, VALID_PROJECT_TOML)
        config = HostConfig.from_toml()
        assert config.host.docker_unprivileged_user == "sandbox"

    def test_malformed_toml_raises(self, isolated_sandbox_ai_user_home: Path) -> None:
        """Malformed TOML syntax raises before any state changes."""
        _seed_host_config(isolated_sandbox_ai_user_home, "[host\ninvalid toml")
        with pytest.raises(Exception):  # tomllib.TOMLDecodeError
            HostConfig.from_toml()


class TestHostSettingsModel:
    """HostSettings nested model structure."""

    def test_host_config_attributes(self) -> None:
        """HostSettings exposes docker_unprivileged_user and machinectl_authentication."""
        hc = HostSettings(docker_unprivileged_user="sandbox", machinectl_authentication=MachinectlAuth.POLKIT)
        assert hc.docker_unprivileged_user == "sandbox"
        assert hc.machinectl_authentication == MachinectlAuth.POLKIT


class TestMachinectlAuthEnum:
    """MachinectlAuth StrEnum members."""

    def test_exactly_two_members(self) -> None:
        """MachinectlAuth contains exactly SUDO and POLKIT."""
        members = list(MachinectlAuth)
        assert len(members) == 2
        assert MachinectlAuth.SUDO in members
        assert MachinectlAuth.POLKIT in members

    def test_string_values(self) -> None:
        """Enum values are the expected strings."""
        assert MachinectlAuth.SUDO.value == "sudo"
        assert MachinectlAuth.POLKIT.value == "polkit"


# ─── Task 1.3: machinectl_cmd() ──────────────────────────────────────────────


class TestMachinectlCmd:
    """machinectl_cmd() command prefix builder."""

    def test_sudo_mode_returns_sudo_prefix(self) -> None:
        """Sudo mode: ['sudo', 'machinectl', 'shell', '<user>@.host']."""
        result = machinectl_cmd("sandbox", MachinectlAuth.SUDO)
        assert result == ["sudo", "machinectl", "shell", "sandbox@.host"]

    def test_polkit_mode_returns_no_sudo_prefix(self) -> None:
        """Polkit mode: ['machinectl', 'shell', '<user>@.host']."""
        result = machinectl_cmd("sandbox", MachinectlAuth.POLKIT)
        assert result == ["machinectl", "shell", "sandbox@.host"]

    def test_custom_user(self) -> None:
        """User parameter is interpolated correctly."""
        result = machinectl_cmd("devuser", MachinectlAuth.SUDO)
        assert result == ["sudo", "machinectl", "shell", "devuser@.host"]

    def test_returns_new_list_each_call(self) -> None:
        """Each call returns a fresh list (no shared mutable state)."""
        a = machinectl_cmd("sandbox", MachinectlAuth.SUDO)
        b = machinectl_cmd("sandbox", MachinectlAuth.SUDO)
        assert a == b
        assert a is not b
