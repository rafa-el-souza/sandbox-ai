"""Tests for core/host_config.py — ProjectConfig model and machinectl_cmd builder."""

from pathlib import Path

import pytest
from core.host_config import HostConfig, MachinectlAuth, ProjectConfig, machinectl_cmd
from pydantic import ValidationError

# ─── Task 1.2: ProjectConfig.from_toml() ─────────────────────────────────────

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


class TestProjectConfigFromToml:
    """ProjectConfig.from_toml() parsing and validation."""

    def test_valid_config_parsed(self, tmp_path: Path) -> None:
        """Valid sandbox-ai.toml parses into ProjectConfig without errors."""
        (tmp_path / "sandbox-ai.toml").write_text(VALID_PROJECT_TOML)
        config = ProjectConfig.from_toml(str(tmp_path))
        assert config.host.docker_unprivileged_user == "sandbox"
        assert config.host.machinectl_authentication == MachinectlAuth.SUDO

    def test_polkit_mode_parsed(self, tmp_path: Path) -> None:
        """machinectl_authentication = 'polkit' parses to MachinectlAuth.POLKIT."""
        (tmp_path / "sandbox-ai.toml").write_text(VALID_PROJECT_TOML_POLKIT)
        config = ProjectConfig.from_toml(str(tmp_path))
        assert config.host.machinectl_authentication == MachinectlAuth.POLKIT

    def test_missing_file_raises_file_not_found(self, tmp_path: Path) -> None:
        """Missing sandbox-ai.toml raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            ProjectConfig.from_toml(str(tmp_path))

    def test_missing_required_field_raises_validation_error(self, tmp_path: Path) -> None:
        """Missing docker_unprivileged_user raises ValidationError."""
        (tmp_path / "sandbox-ai.toml").write_text('[host]\nmachinectl_authentication = "sudo"\n')
        with pytest.raises(ValidationError):
            ProjectConfig.from_toml(str(tmp_path))

    def test_invalid_enum_value_raises_validation_error(self, tmp_path: Path) -> None:
        """Invalid machinectl_authentication value raises ValidationError."""
        bad_toml = '[host]\ndocker_unprivileged_user = "sandbox"\nmachinectl_authentication = "pkexec"\n'
        (tmp_path / "sandbox-ai.toml").write_text(bad_toml)
        with pytest.raises(ValidationError):
            ProjectConfig.from_toml(str(tmp_path))

    def test_default_auth_mode_is_sudo(self, tmp_path: Path) -> None:
        """Omitted machinectl_authentication defaults to 'sudo'."""
        (tmp_path / "sandbox-ai.toml").write_text(VALID_PROJECT_TOML_NO_AUTH)
        config = ProjectConfig.from_toml(str(tmp_path))
        assert config.host.machinectl_authentication == MachinectlAuth.SUDO

    def test_loader_uses_provided_path(self, tmp_path: Path) -> None:
        """Loader reads from the given project_dir, not CWD."""
        subdir = tmp_path / "nested" / "project"
        subdir.mkdir(parents=True)
        (subdir / "sandbox-ai.toml").write_text(VALID_PROJECT_TOML)
        config = ProjectConfig.from_toml(str(subdir))
        assert config.host.docker_unprivileged_user == "sandbox"

    def test_malformed_toml_raises(self, tmp_path: Path) -> None:
        """Malformed TOML syntax raises before any state changes."""
        (tmp_path / "sandbox-ai.toml").write_text("[host\ninvalid toml")
        with pytest.raises(Exception):  # tomllib.TOMLDecodeError
            ProjectConfig.from_toml(str(tmp_path))


class TestHostConfigModel:
    """HostConfig nested model structure."""

    def test_host_config_attributes(self) -> None:
        """HostConfig exposes docker_unprivileged_user and machinectl_authentication."""
        hc = HostConfig(docker_unprivileged_user="sandbox", machinectl_authentication=MachinectlAuth.POLKIT)
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
