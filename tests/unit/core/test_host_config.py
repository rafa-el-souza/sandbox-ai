"""Tests for core/host_config.py — HostConfig model and machinectl_cmd builder."""

import os
import stat
from pathlib import Path

import pytest
from core.host_config import (
    HostConfig,
    HostSettings,
    MachinectlAuth,
    NoFreeGidInSubgidRangeError,
    NoSubgidRangeError,
    NoSubuidRangeError,
    SubgidOutOfRangeError,
    SubuidOutOfRangeError,
    WorkspaceBridgeGroupMissingError,
    autodetect_workspace_bridge_gid_recommendation,
    ensure_per_user_state,
    host_gid_for_in_container,
    host_id_for_in_container,
    in_container_gid_for_host_gid,
    machinectl_cmd,
    parse_subgid_for_user,
    parse_subuid_for_user,
    sandbox_ai_home,
    workspace_bridge_gid,
)
from pydantic import ValidationError


class TestSandboxAiUserHome:
    """sandbox_ai_home() canonical-path resolver."""

    def test_env_unset_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With SANDBOX_AI_HOME unset, returns ~/.sandbox-ai."""
        monkeypatch.delenv("SANDBOX_AI_HOME", raising=False)
        result = sandbox_ai_home()
        assert result == Path(os.path.expanduser("~/.sandbox-ai"))

    def test_env_set_returns_env_value(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """With SANDBOX_AI_HOME set, returns that path."""
        monkeypatch.setenv("SANDBOX_AI_HOME", str(tmp_path / "custom"))
        result = sandbox_ai_home()
        assert result == tmp_path / "custom"

    def test_idempotent(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Repeated calls return equal paths."""
        monkeypatch.setenv("SANDBOX_AI_HOME", str(tmp_path))
        assert sandbox_ai_home() == sandbox_ai_home()


class TestEnsurePerUserState:
    """ensure_per_user_state(): creates the 5-dir per-user tree with mode 0700, idempotent."""

    EXPECTED_SUBDIRS = ("config", "state", "instances", "workspaces")

    def test_creates_full_tree_with_mode_0700(self, tmp_path: Path) -> None:
        home = tmp_path / ".sandbox-ai"
        ensure_per_user_state(home)
        assert home.is_dir()
        assert stat.S_IMODE(home.stat().st_mode) == 0o700
        for sub in self.EXPECTED_SUBDIRS:
            assert (home / sub).is_dir()
            assert stat.S_IMODE((home / sub).stat().st_mode) == 0o700

    def test_idempotent(self, tmp_path: Path) -> None:
        home = tmp_path / ".sandbox-ai"
        ensure_per_user_state(home)
        ensure_per_user_state(home)  # must not raise

    def test_partial_tree_completion(self, tmp_path: Path) -> None:
        """Missing subdirs are filled in; pre-existing ones are left alone."""
        home = tmp_path / ".sandbox-ai"
        home.mkdir(mode=0o700)
        (home / "config").mkdir(mode=0o700)
        ensure_per_user_state(home)
        for sub in self.EXPECTED_SUBDIRS:
            assert (home / sub).is_dir()

    def test_does_not_modify_existing_mode(self, tmp_path: Path) -> None:
        """Pre-existing dirs keep their mode (exist_ok=True semantics)."""
        home = tmp_path / ".sandbox-ai"
        home.mkdir(mode=0o755)
        (home / "config").mkdir(mode=0o755)
        ensure_per_user_state(home)
        assert stat.S_IMODE(home.stat().st_mode) == 0o755
        assert stat.S_IMODE((home / "config").stat().st_mode) == 0o755


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

    def test_valid_config_parsed(self, isolated_sandbox_ai_home: Path) -> None:
        """Valid sandbox-ai.toml parses into HostConfig without errors."""
        _seed_host_config(isolated_sandbox_ai_home, VALID_PROJECT_TOML)
        config = HostConfig.from_toml()
        assert config.host.docker_unprivileged_user == "sandbox"
        assert config.host.machinectl_authentication == MachinectlAuth.SUDO

    def test_polkit_mode_parsed(self, isolated_sandbox_ai_home: Path) -> None:
        """machinectl_authentication = 'polkit' parses to MachinectlAuth.POLKIT."""
        _seed_host_config(isolated_sandbox_ai_home, VALID_PROJECT_TOML_POLKIT)
        config = HostConfig.from_toml()
        assert config.host.machinectl_authentication == MachinectlAuth.POLKIT

    def test_missing_file_raises_file_not_found(self, isolated_sandbox_ai_home: Path) -> None:
        """Missing sandbox-ai.toml raises FileNotFoundError with canonical path."""
        with pytest.raises(FileNotFoundError, match="Run sandbox init"):
            HostConfig.from_toml()

    def test_missing_required_field_raises_validation_error(self, isolated_sandbox_ai_home: Path) -> None:
        """Missing docker_unprivileged_user raises ValidationError."""
        _seed_host_config(isolated_sandbox_ai_home, '[host]\nmachinectl_authentication = "sudo"\n')
        with pytest.raises(ValidationError):
            HostConfig.from_toml()

    def test_invalid_enum_value_raises_validation_error(self, isolated_sandbox_ai_home: Path) -> None:
        """Invalid machinectl_authentication value raises ValidationError."""
        bad_toml = '[host]\ndocker_unprivileged_user = "sandbox"\nmachinectl_authentication = "pkexec"\n'
        _seed_host_config(isolated_sandbox_ai_home, bad_toml)
        with pytest.raises(ValidationError):
            HostConfig.from_toml()

    def test_default_auth_mode_is_sudo(self, isolated_sandbox_ai_home: Path) -> None:
        """Omitted machinectl_authentication defaults to 'sudo'."""
        _seed_host_config(isolated_sandbox_ai_home, VALID_PROJECT_TOML_NO_AUTH)
        config = HostConfig.from_toml()
        assert config.host.machinectl_authentication == MachinectlAuth.SUDO

    def test_loader_honors_env_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Loader reads from SANDBOX_AI_HOME-resolved path."""
        custom_home = tmp_path / "custom"
        monkeypatch.setenv("SANDBOX_AI_HOME", str(custom_home))
        _seed_host_config(custom_home, VALID_PROJECT_TOML)
        config = HostConfig.from_toml()
        assert config.host.docker_unprivileged_user == "sandbox"

    def test_malformed_toml_raises(self, isolated_sandbox_ai_home: Path) -> None:
        """Malformed TOML syntax raises before any state changes."""
        _seed_host_config(isolated_sandbox_ai_home, "[host\ninvalid toml")
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


# ─── Subuid / subgid resolvers ──────────────────────────────────────────────


def _write_subid_file(path: Path, body: str) -> None:
    path.write_text(body)


def _patch_subuid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str) -> Path:
    """Redirect production's /etc/subuid path constant to a tmp file."""
    f = tmp_path / "subuid"
    f.write_text(body)
    monkeypatch.setattr("core.host_config._SUBUID_PATH", f)
    return f


def _patch_subgid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str) -> Path:
    f = tmp_path / "subgid"
    f.write_text(body)
    monkeypatch.setattr("core.host_config._SUBGID_PATH", f)
    return f


class TestParseSubidFiles:
    """parse_subuid_for_user / parse_subgid_for_user."""

    def test_single_range(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_subuid(monkeypatch, tmp_path, "claude-sandbox:100000:65536\nother:200000:65536\n")
        assert parse_subuid_for_user("claude-sandbox") == [(100000, 65536)]

    def test_multi_range(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_subuid(monkeypatch, tmp_path, "u:1000:500\nu:2000:1000\nother:9000:10\n")
        assert parse_subuid_for_user("u") == [(1000, 500), (2000, 1000)]

    def test_user_absent_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_subuid(monkeypatch, tmp_path, "other:200000:65536\n")
        assert parse_subuid_for_user("missing") == []

    def test_missing_file_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Point the constant at a path that doesn't exist.
        monkeypatch.setattr("core.host_config._SUBUID_PATH", tmp_path / "nope")
        assert parse_subuid_for_user("anyone") == []

    def test_skips_blank_and_comment_and_malformed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_subuid(
            monkeypatch,
            tmp_path,
            "\n# comment\nclaude-sandbox:100000:65536\nbadrow\nclaude-sandbox:notanint:42\n",
        )
        assert parse_subuid_for_user("claude-sandbox") == [(100000, 65536)]

    def test_subgid_uses_separate_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """parse_subgid_for_user reads ``_SUBGID_PATH``, not ``_SUBUID_PATH``."""
        _patch_subuid(monkeypatch, tmp_path, "u:1:2\n")
        _patch_subgid(monkeypatch, tmp_path, "u:9000:10\n")
        assert parse_subuid_for_user("u") == [(1, 2)]
        assert parse_subgid_for_user("u") == [(9000, 10)]


class TestHostIdForInContainer:
    """host_id_for_in_container forward map."""

    def test_n_zero_returns_primary_uid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Pw:
            pw_uid = 4242
            pw_gid = 4343

        monkeypatch.setattr("core.host_config.pwd.getpwnam", lambda u: _Pw())
        assert host_id_for_in_container(0, "claude-sandbox") == 4242

    def test_n_zero_unknown_user_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(u: str) -> None:
            raise KeyError(u)

        monkeypatch.setattr("core.host_config.pwd.getpwnam", _raise)
        with pytest.raises(NoSubuidRangeError):
            host_id_for_in_container(0, "missing")

    def test_n_one_maps_to_first_allocated(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch_subuid(monkeypatch, tmp_path, "claude-sandbox:100000:65536\n")
        assert host_id_for_in_container(1, "claude-sandbox") == 100000

    def test_n_one_thousand(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch_subuid(monkeypatch, tmp_path, "claude-sandbox:100000:65536\n")
        assert host_id_for_in_container(1000, "claude-sandbox") == 100999

    def test_n_out_of_range_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch_subuid(monkeypatch, tmp_path, "claude-sandbox:100000:10\n")
        with pytest.raises(SubuidOutOfRangeError):
            host_id_for_in_container(11, "claude-sandbox")

    def test_no_range_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch_subuid(monkeypatch, tmp_path, "other:1:1\n")
        with pytest.raises(NoSubuidRangeError):
            host_id_for_in_container(1, "claude-sandbox")

    def test_negative_n_raises(self) -> None:
        with pytest.raises(SubuidOutOfRangeError):
            host_id_for_in_container(-1, "claude-sandbox")

    def test_multi_range_spans_correctly(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch_subuid(monkeypatch, tmp_path, "claude-sandbox:1000:5\nclaude-sandbox:9000:5\n")
        # range 1: 1..5 → 1000..1004
        # range 2: 6..10 → 9000..9004
        assert host_id_for_in_container(5, "claude-sandbox") == 1004
        assert host_id_for_in_container(6, "claude-sandbox") == 9000
        assert host_id_for_in_container(10, "claude-sandbox") == 9004
        with pytest.raises(SubuidOutOfRangeError):
            host_id_for_in_container(11, "claude-sandbox")


class TestHostGidForInContainer:
    """host_gid_for_in_container forward map."""

    def test_n_zero_returns_primary_gid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Pw:
            pw_uid = 1
            pw_gid = 7777

        monkeypatch.setattr("core.host_config.pwd.getpwnam", lambda u: _Pw())
        assert host_gid_for_in_container(0, "claude-sandbox") == 7777

    def test_n_zero_unknown_user_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(u: str) -> None:
            raise KeyError(u)

        monkeypatch.setattr("core.host_config.pwd.getpwnam", _raise)
        with pytest.raises(NoSubgidRangeError):
            host_gid_for_in_container(0, "missing")

    def test_forward_map(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch_subgid(monkeypatch, tmp_path, "claude-sandbox:200000:65536\n")
        assert host_gid_for_in_container(1, "claude-sandbox") == 200000

    def test_negative_raises(self) -> None:
        with pytest.raises(SubgidOutOfRangeError):
            host_gid_for_in_container(-1, "u")

    def test_no_range_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch_subgid(monkeypatch, tmp_path, "other:1:1\n")
        with pytest.raises(NoSubgidRangeError):
            host_gid_for_in_container(1, "claude-sandbox")

    def test_out_of_range_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch_subgid(monkeypatch, tmp_path, "claude-sandbox:200000:5\n")
        with pytest.raises(SubgidOutOfRangeError):
            host_gid_for_in_container(6, "claude-sandbox")


class TestInContainerGidForHostGid:
    """in_container_gid_for_host_gid inverse map."""

    def test_within_range(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch_subgid(monkeypatch, tmp_path, "claude-sandbox:200000:65536\n")
        # host gid 200000 → in-container 1 (first allocated maps to N=1)
        assert in_container_gid_for_host_gid(200000, "claude-sandbox") == 1
        assert in_container_gid_for_host_gid(200999, "claude-sandbox") == 1000

    def test_out_of_range_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch_subgid(monkeypatch, tmp_path, "claude-sandbox:200000:65536\n")
        with pytest.raises(SubgidOutOfRangeError):
            in_container_gid_for_host_gid(50, "claude-sandbox")

    def test_no_range_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch_subgid(monkeypatch, tmp_path, "other:1:1\n")
        with pytest.raises(NoSubgidRangeError):
            in_container_gid_for_host_gid(200000, "claude-sandbox")

    def test_multi_range_inverse(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch_subgid(monkeypatch, tmp_path, "u:1000:5\nu:9000:5\n")
        # 1000 → 1, 1004 → 5, 9000 → 6, 9004 → 10
        assert in_container_gid_for_host_gid(1000, "u") == 1
        assert in_container_gid_for_host_gid(1004, "u") == 5
        assert in_container_gid_for_host_gid(9000, "u") == 6
        assert in_container_gid_for_host_gid(9004, "u") == 10


class TestWorkspaceBridgeGid:
    """workspace_bridge_gid resolver."""

    def test_resolves_and_validates(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch_subgid(monkeypatch, tmp_path, "claude-sandbox:200000:65536\n")

        class _Gr:
            gr_gid = 200500

        monkeypatch.setattr("core.host_config.grp.getgrnam", lambda n: _Gr())
        host = HostSettings(docker_unprivileged_user="claude-sandbox")
        assert workspace_bridge_gid(host) == 200500

    def test_missing_group_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(n: str) -> None:
            raise KeyError(n)

        monkeypatch.setattr("core.host_config.grp.getgrnam", _raise)
        host = HostSettings(docker_unprivileged_user="claude-sandbox")
        with pytest.raises(WorkspaceBridgeGroupMissingError):
            workspace_bridge_gid(host)

    def test_out_of_range_propagates(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch_subgid(monkeypatch, tmp_path, "claude-sandbox:200000:5\n")

        class _Gr:
            gr_gid = 99

        monkeypatch.setattr("core.host_config.grp.getgrnam", lambda n: _Gr())
        host = HostSettings(docker_unprivileged_user="claude-sandbox")
        with pytest.raises(SubgidOutOfRangeError):
            workspace_bridge_gid(host)


class TestAutodetectRecommendation:
    """autodetect_workspace_bridge_gid_recommendation."""

    def test_picks_above_min(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch_subgid(monkeypatch, tmp_path, "claude-sandbox:200000:65536\n")
        monkeypatch.setattr("core.host_config.grp.getgrall", lambda: [])
        # in_container_min=1000 → host gid offset 999 → 200999
        gid = autodetect_workspace_bridge_gid_recommendation("claude-sandbox", in_container_min=1000)
        assert gid == 200999

    def test_skips_used_gids(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch_subgid(monkeypatch, tmp_path, "claude-sandbox:200000:65536\n")

        class _Gr:
            def __init__(self, gid: int) -> None:
                self.gr_gid = gid

        monkeypatch.setattr("core.host_config.grp.getgrall", lambda: [_Gr(200999), _Gr(201000)])
        gid = autodetect_workspace_bridge_gid_recommendation("claude-sandbox", in_container_min=1000)
        assert gid == 201001

    def test_no_range_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch_subgid(monkeypatch, tmp_path, "other:1:1\n")
        monkeypatch.setattr("core.host_config.grp.getgrall", lambda: [])
        with pytest.raises(NoSubgidRangeError):
            autodetect_workspace_bridge_gid_recommendation("claude-sandbox")

    def test_no_free_gid_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch_subgid(monkeypatch, tmp_path, "claude-sandbox:200000:3\n")

        class _Gr:
            def __init__(self, gid: int) -> None:
                self.gr_gid = gid

        # All in-range gids used; only offset 0 maps below in_container_min anyway.
        monkeypatch.setattr(
            "core.host_config.grp.getgrall",
            lambda: [_Gr(200000), _Gr(200001), _Gr(200002)],
        )
        with pytest.raises(NoFreeGidInSubgidRangeError):
            autodetect_workspace_bridge_gid_recommendation("claude-sandbox", in_container_min=1)


class TestHostSettingsBridgeGroup:
    """HostSettings has the new workspace_bridge_group field with default."""

    def test_default(self) -> None:
        h = HostSettings(docker_unprivileged_user="claude-sandbox")
        assert h.workspace_bridge_group == "sb-ws"

    def test_override(self) -> None:
        h = HostSettings(docker_unprivileged_user="claude-sandbox", workspace_bridge_group="bridge")
        assert h.workspace_bridge_group == "bridge"
