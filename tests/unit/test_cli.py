"""CLI lifecycle unit tests — start, stop, attach, destroy.

All subprocess interactions are mocked at the core.executor.Executor.run boundary.
Tests validate the full phase sequencing per the orchestrator design spec.
"""

import contextlib
import json
import os
import stat
import subprocess
import typing
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, call, patch

import pytest
from typer.testing import CliRunner

if TYPE_CHECKING:
    from tests.unit.conftest import HostConfigFactory

# ── Fixtures ─────────────────────────────────────────────────────────────────

SANDBOX_AI_HOME = "/fake/sandbox-ai"
PROJECT_DIR = "/home/dev/myproject"
INSTANCE_ID = "myproject-abc123"
INSTANCE_DIR = f"{SANDBOX_AI_HOME}/sandboxes/{INSTANCE_ID}"
STATE_DIR = f"{SANDBOX_AI_HOME}/.state"
REGISTRY_PATH = f"{STATE_DIR}/instances.json"
IPAM_PATH = f"{STATE_DIR}/ipam.json"
SANDBOX_TOML = f"{INSTANCE_DIR}/sandbox.toml"
HOST_USER = "sandbox"


VALID_TOML_CONTENT = b"""
[instance]
name = "myproject"
user_project_root = "/home/dev/myproject"
host_uid = "1000"
warmup_prompt = ""

[core]
shm_size = "2gb"
pids_limit = 400
base_image = "cgr.dev/chainguard/wolfi-base:latest"
base_distro_family = "wolfi"
git_user = ""
git_email = ""

[admin]
shm_size = "2gb"
pids_limit = 400
base_image = "debian:trixie-slim"
base_distro_family = "debian"

[runtimes]
python = true
typescript = true
rust = true
go = false

[runtimes.node]
version = "20.12.2"
nvm_version = "0.39.7"

[components]
mcp_firecrawl = false
mcp_puppeteer = false

[components.db_postgres]
enabled = true
expose_host_ports = [5432]

[components.ingress]
web_ports = [3000, 8080]

[proxy.whitelist]
domains = [".github.com"]
"""

RENAMED_TOML_CONTENT = VALID_TOML_CONTENT.replace(b'name = "myproject"', b'name = "renamed-instance"')

WARMUP_TOML_CONTENT = VALID_TOML_CONTENT.replace(b'warmup_prompt = ""', b'warmup_prompt = "bootstrap the project"')


# Capture the real seeder before any autouse patching can replace it. Tests that
# want the real seeder to run apply ``wraps=_REAL_SEED_HOST_CONFIG`` so the
# autouse no-op below is effectively bypassed.
import cli.main as _cli_main_module  # noqa: E402

_REAL_SEED_HOST_CONFIG = _cli_main_module._seed_host_config_if_absent


@pytest.fixture(autouse=True)
def _noop_init_seeder() -> typing.Iterator[None]:
    """Skip the interactive host-config seeder during init tests.

    The seeder normally prompts (TTY) or fails (non-TTY) when
    ``<home>/config/sandbox-ai.toml`` is missing. CLI tests provide host
    config via the ``_default_project_config`` autouse mock instead.
    """
    with patch("cli.main._seed_host_config_if_absent"):
        yield


@pytest.fixture(autouse=True)
def _default_bridge_gid() -> typing.Iterator[None]:
    """Auto-supply workspace bridge gid + subuid resolution so build_jinja_context
    and the helper-mkdir plan don't require real /etc/subgid + sb-ws on the host."""
    with (
        patch("core.hydration.workspace_bridge_gid", return_value=200000),
        patch("core.hydration.in_container_gid_for_host_gid", return_value=1000),
        patch("cli.main.host_id_for_in_container", return_value=100999),
        patch("cli.main.host_gid_for_in_container", return_value=200999),
    ):
        yield


@pytest.fixture(autouse=True)
def _default_project_config() -> typing.Iterator[None]:
    """Auto-supply a default sandbox-ai.toml so post-init commands don't exit early.

    `_resolve_host_config` now requires `sandbox-ai.toml` (no fallback). Tests
    that are not specifically about the missing-config path get a default
    `HostConfig` injected here. Tests that need a different value or want to
    exercise the FileNotFoundError path can re-patch in their own `with patch(...)`
    block — pytest's mock stacks override this autouse fixture.
    """
    from core.host_config import HostConfig

    default = HostConfig.model_validate(
        {"host": {"docker_unprivileged_user": HOST_USER, "machinectl_authentication": "sudo"}}
    )
    with patch("cli.main.HostConfig.from_toml", return_value=default):
        yield


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def mock_sandbox_ai_home(tmp_path: Path) -> Path:
    """Create a realistic SANDBOX_AI_HOME layout in tmp_path."""
    home = tmp_path / "sandbox-ai"
    state = home / ".state"
    state.mkdir(parents=True)
    (state / "instances.json").write_text("{}")
    (state / "ipam.json").write_text("{}")
    # Create minimal tooling plane dirs
    (home / ".docker" / "core").mkdir(parents=True)
    (home / ".docker" / "admin").mkdir(parents=True)
    (home / ".config" / "proxy").mkdir(parents=True)
    return home


def _user_home() -> Path:
    """Resolve the per-user home from SANDBOX_AI_USER_HOME (autouse fixture sets it)."""
    return Path(os.environ["SANDBOX_AI_USER_HOME"])


def _seed_registry(home: Path) -> None:
    """Mark the per-user state tree as initialized for hard-fail bypass."""
    state_dir = home / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    registry = state_dir / "instances.json"
    if not registry.exists():
        registry.write_text("{}")


def _register_instance(home: Path, project_dir: str, instance_id: str) -> Path:
    """Helper: register an instance and create its directory structure."""
    state_dir = _user_home() / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    reg = state_dir / "instances.json"
    reg.write_text(json.dumps({project_dir: instance_id}))
    inst = home / "sandboxes" / instance_id
    (inst / "docker" / "core").mkdir(parents=True)
    (inst / "docker" / "admin").mkdir(parents=True)
    (inst / "docker" / "extras").mkdir(parents=True)
    (inst / "config" / "proxy").mkdir(parents=True)
    (inst / "config" / "admin").mkdir(parents=True)
    (inst / "config" / "core").mkdir(parents=True)
    (inst / "config" / "coredns").mkdir(parents=True)
    (inst / "config" / "dnsdist").mkdir(parents=True)
    (inst / "log" / "orchestrator").mkdir(parents=True)
    # Write sandbox.toml
    (inst / "sandbox.toml").write_bytes(VALID_TOML_CONTENT)
    # Write .sandbox.env
    (inst / ".sandbox.env").write_text('CORE_ANTHROPIC_API_KEY="test"')
    # Write .initialized sentinel
    (inst / ".initialized").write_text("")
    return inst


def _write_ipam(instance_id: str, base_index: int) -> None:
    """Helper: write an IPAM entry to ``<user_home>/state/ipam.json``."""
    state_dir = _user_home() / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    ipam = state_dir / "ipam.json"
    ipam.write_text(json.dumps({instance_id: base_index}))


# ── sandbox start ────────────────────────────────────────────────────────────


class TestStartHappyPath:
    """Task 8.2: sandbox start happy path — all phases sequenced correctly."""

    def test_start_existing_instance_full_sequence(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """Existing instance: full start sequence (registry hit -> compose -> handover)."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)
        _write_ipam(instance_id, 0)

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="proxypass123"),
            patch("cli.main._phase_hydrate") as mock_hydrate,
            patch("cli.main._phase_acl_grant") as mock_acl,
            patch("cli.main._phase_credential_ownership"),
            patch("cli.main._phase_helper_mkdir_chown_cache_log"),
            patch("cli.main._phase_compose_up") as mock_compose,
            patch("cli.main._phase_handover") as mock_handover,
            patch("cli.main._release_lock"),
        ):
            result = runner.invoke(app, ["start"])
            assert result.exit_code == 0
            mock_hydrate.assert_called_once()
            mock_acl.assert_called_once()
            mock_compose.assert_called_once()
            mock_handover.assert_called_once()

    def test_start_no_init_errors(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """New project: registry miss → error with init guidance."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/newproject"

        from cli.main import app

        _seed_registry(_user_home())
        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
        ):
            result = runner.invoke(app, ["start"])
            assert result.exit_code == 1
            assert "sandbox init" in result.output.lower()

    def test_start_partial_init_errors(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """Instance registered but .initialized sentinel missing → error."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        inst = _register_instance(home, project_dir, instance_id)
        # Remove the sentinel
        (inst / ".initialized").unlink()

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
        ):
            result = runner.invoke(app, ["start"])
            assert result.exit_code == 1
            assert "partially initialized" in result.output.lower() or "destroy" in result.output.lower()

    def test_start_progress_output(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """Start shows phase progress indicators."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)
        _write_ipam(instance_id, 0)

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="proxypass123"),
            patch("cli.main._phase_hydrate"),
            patch("cli.main._phase_acl_grant"),
            patch("cli.main._phase_credential_ownership"),
            patch("cli.main._phase_helper_mkdir_chown_cache_log"),
            patch("cli.main._phase_compose_up"),
            patch("cli.main._phase_handover"),
            patch("cli.main._release_lock"),
        ):
            result = runner.invoke(app, ["start"])
            assert result.exit_code == 0
            out = result.output.lower()
            # Should show progress indicators
            assert "ipam" in out or "network" in out
            assert "compose" in out or "containers" in out

    def test_start_handover_indication(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """Start shows handover indication before PTY exec."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)
        _write_ipam(instance_id, 0)

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="pass"),
            patch("cli.main._phase_hydrate"),
            patch("cli.main._phase_acl_grant"),
            patch("cli.main._phase_credential_ownership"),
            patch("cli.main._phase_helper_mkdir_chown_cache_log"),
            patch("cli.main._phase_compose_up"),
            patch("cli.main._phase_handover"),
            patch("cli.main._release_lock"),
        ):
            result = runner.invoke(app, ["start"])
            assert result.exit_code == 0
            out = result.output.lower()
            assert "handing over" in out or "handover" in out or "admin shell" in out


class TestStartSecretCompletenessGate:
    """Task 4.4a: secret completeness gate — must run before _acquire_state_lock."""

    def test_start_exits_on_missing_secrets(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """start exits code 1 when _check_secrets returns missing secrets."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._check_secrets", return_value=["PG_PASSWORD"]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock") as mock_lock,
        ):
            result = runner.invoke(app, ["start"])
            assert result.exit_code == 1
            assert "pg_password" in result.output.lower()
            # Gate must fire BEFORE lock acquisition
            mock_lock.assert_not_called()


class TestStartDoctorChain1PreFlight:
    """Task 4.5a: doctor Chain 1 (Privilege Boundary) pre-flight in start."""

    def test_start_exits_on_doctor_chain1_failure(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """start exits code 1 when run_check_subset returns fail for Privilege Boundary."""
        from core.doctor import CheckResult

        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)

        failed_results = [
            CheckResult(status="fail", name="machinectl", detail="not configured", remediation="fix sudoers"),
        ]

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=failed_results),
            patch("cli.main.render_results") as mock_render,
            patch("cli.main._warm_check") as mock_warm,
        ):
            result = runner.invoke(app, ["start"])
            assert result.exit_code == 1
            mock_render.assert_called_once()
            # Gate must fire BEFORE warm check
            mock_warm.assert_not_called()


class TestStartComposeSpinner:
    """Task 4.6a: console.status() spinner during compose-up phase."""

    def test_start_uses_console_status_for_compose(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """start uses console.status() context manager during compose-up."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)
        _write_ipam(instance_id, 0)

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="pass"),
            patch("cli.main._phase_hydrate"),
            patch("cli.main._phase_acl_grant"),
            patch("cli.main._phase_credential_ownership"),
            patch("cli.main._phase_helper_mkdir_chown_cache_log"),
            patch("cli.main._phase_compose_up"),
            patch("cli.main._phase_handover"),
            patch("cli.main._release_lock"),
            patch.object(
                __import__("cli.main", fromlist=["console"]).console,
                "status",
            ) as mock_status,
        ):
            result = runner.invoke(app, ["start"])
            assert result.exit_code == 0
            mock_status.assert_called_once()
            # Verify __enter__ was called (context manager was used)
            mock_status.return_value.__enter__.assert_called()


class TestStartWarmExit:
    """Task 8.2: pre-lock warm-exit — no locks acquired."""

    def test_warm_instance_exits_before_locking(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=True),
            patch("cli.main._acquire_state_lock") as mock_lock,
        ):
            result = runner.invoke(app, ["start"])
            assert result.exit_code == 0
            assert "already running" in result.output.lower()
            mock_lock.assert_not_called()


class TestStartLockContention:
    """Task 8.2: lock contention exit."""

    def test_lock_contention_exits_with_message(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", side_effect=BlockingIOError("locked")),
        ):
            result = runner.invoke(app, ["start"])
            assert result.exit_code == 1
            assert "already in progress" in result.output.lower()


class TestStartIPAMExhausted:
    """Task 8.2: IPAM exhausted exit."""

    def test_ipam_exhausted_releases_lock_and_exits(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")

        from cli.main import app
        from core.ipam import IPAMExhaustedError

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", side_effect=IPAMExhaustedError("full")),
            patch("cli.main._release_lock") as mock_release,
        ):
            result = runner.invoke(app, ["start"])
            assert result.exit_code == 1
            mock_release.assert_called_once()


class TestStartWorkspaceBridgeMissing:
    """Task 5.2: hydration aborts when workspace bridge group is missing."""

    def test_start_aborts_with_remediation_hint(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        from cli.main import app
        from core.host_config import WorkspaceBridgeGroupMissingError

        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="pass"),
            patch(
                "cli.main._phase_hydrate",
                side_effect=WorkspaceBridgeGroupMissingError("group 'sb-ws' does not exist"),
            ),
            patch("cli.main._release_lock") as mock_release,
        ):
            result = runner.invoke(app, ["start"])
            assert result.exit_code == 1
            assert "sb-ws" in result.output
            assert "sandbox doctor" in result.output
            mock_release.assert_called_once()

    def test_dry_run_aborts_with_remediation_hint(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        from cli.main import app
        from core.host_config import WorkspaceBridgeGroupMissingError

        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam("myproject-abc123", 0)
        _create_tooling_plane(home)

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch(
                "cli.main.build_jinja_context",
                side_effect=WorkspaceBridgeGroupMissingError("group 'sb-ws' does not exist"),
            ),
        ):
            result = runner.invoke(app, ["start", "--dry-run"])
            assert result.exit_code == 1
            assert "sb-ws" in result.output
            assert "sandbox doctor" in result.output


class TestStartComposeUnhealthy:
    """Task 8.2: compose unhealthy exit."""

    def test_unhealthy_compose_releases_lock_and_exits(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")

        from cli.main import app
        from core.exceptions import SandboxExecutionError

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="pass"),
            patch("cli.main._phase_hydrate"),
            patch("cli.main._phase_acl_grant"),
            patch("cli.main._phase_credential_ownership"),
            patch("cli.main._phase_helper_mkdir_chown_cache_log"),
            patch("cli.main._phase_compose_up", side_effect=SandboxExecutionError("unhealthy")),
            patch("cli.main._release_lock") as mock_release,
        ):
            result = runner.invoke(app, ["start"])
            assert result.exit_code == 1
            mock_release.assert_called_once()


class TestStartSshKeypairGeneration:
    """11.T: _phase_credentials generates SSH keypairs and IPC setup is removed."""

    def test_phase_credentials_generates_ssh_keys(self, tmp_path: Path) -> None:
        """_phase_credentials calls generate_ssh_keypair for auth and host."""
        from cli.main import _phase_credentials

        proxy_dir = tmp_path / "config" / "proxy"
        proxy_dir.mkdir(parents=True)
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir(parents=True)

        with (
            patch("cli.main.write_htpasswd"),
            patch("cli.main.generate_ssh_keypair") as mock_ssh,
        ):
            _phase_credentials(str(tmp_path), core_ipc_ip="10.100.6.3")
            assert mock_ssh.call_count == 2
            calls = mock_ssh.call_args_list
            assert calls[0] == call(str(tmp_path), "auth")
            assert calls[1] == call(str(tmp_path), "host", core_ipc_ip="10.100.6.3")

    def test_phase_ipc_setup_not_in_start(self) -> None:
        """_phase_ipc_setup function no longer exists in cli.main."""
        import cli.main

        assert not hasattr(cli.main, "_phase_ipc_setup")

    def test_acl_grant_plan_includes_secrets(self, tmp_path: Path) -> None:
        """_acl_grant_plan includes at least one entry targeting secrets/ directory."""
        from cli.main import _acl_grant_plan

        plan = _acl_grant_plan(str(tmp_path), "sandbox")
        secrets_entries = [desc for _, desc in plan if "secrets" in desc]
        assert len(secrets_entries) >= 1


class TestStartInstanceNameImmutabilityWarning:
    """Spec: sandbox-toml-schema — instance.name immutability warning."""

    def test_divergent_name_emits_warning(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """WHEN instance.name differs from instance_id name component, THEN warning is emitted."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        inst = _register_instance(home, project_dir, instance_id)
        # Overwrite sandbox.toml with renamed instance.name
        (inst / "sandbox.toml").write_bytes(RENAMED_TOML_CONTENT)

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="pass"),
            patch("cli.main._phase_hydrate"),
            patch("cli.main._phase_acl_grant"),
            patch("cli.main._phase_credential_ownership"),
            patch("cli.main._phase_helper_mkdir_chown_cache_log"),
            patch("cli.main._phase_compose_up"),
            patch("cli.main._phase_handover"),
            patch("cli.main._release_lock"),
        ):
            result = runner.invoke(app, ["start"])
            assert result.exit_code == 0
            assert "instance.name has changed" in result.output
            assert "COMPOSE_PROJECT_NAME" in result.output

    def test_matching_name_no_warning(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """WHEN instance.name matches instance_id name component, THEN no warning."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)
        _write_ipam(instance_id, 0)

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="pass"),
            patch("cli.main._phase_hydrate"),
            patch("cli.main._phase_acl_grant"),
            patch("cli.main._phase_credential_ownership"),
            patch("cli.main._phase_helper_mkdir_chown_cache_log"),
            patch("cli.main._phase_compose_up"),
            patch("cli.main._phase_handover"),
            patch("cli.main._release_lock"),
        ):
            result = runner.invoke(app, ["start"])
            assert result.exit_code == 0
            assert "instance.name has changed" not in result.output


# ── sandbox stop ─────────────────────────────────────────────────────────────


class TestStopWarm:
    """Task 8.2: sandbox stop — warm instance."""

    def test_stop_warm_instance_composes_down_and_revokes_acl(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._warm_check", return_value=True),
            patch("cli.main._compose_down") as mock_down,
            patch("cli.main._revoke_acls") as mock_revoke,
        ):
            result = runner.invoke(app, ["stop"])
            assert result.exit_code == 0
            mock_down.assert_called_once()
            # Verify -v flag NOT passed (plain stop)
            down_args = mock_down.call_args
            assert down_args[1].get("volumes", False) is False or "-v" not in str(down_args)
            mock_revoke.assert_called_once()


class TestStopCold:
    """Task 8.2: sandbox stop — cold instance."""

    def test_stop_cold_instance_warns_and_exits(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._compose_down") as mock_down,
        ):
            result = runner.invoke(app, ["stop"])
            assert result.exit_code == 0
            assert "not running" in result.output.lower()
            mock_down.assert_not_called()


class TestStopClean:
    """Task 8.2: sandbox stop --clean removes volumes."""

    def test_stop_clean_passes_volume_flag(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._warm_check", return_value=True),
            patch("cli.main._compose_down") as mock_down,
            patch("cli.main._revoke_acls"),
        ):
            result = runner.invoke(app, ["stop", "--clean"])
            assert result.exit_code == 0
            mock_down.assert_called_once()
            down_call = mock_down.call_args
            assert down_call[1].get("volumes") is True


# ── sandbox attach ───────────────────────────────────────────────────────────


class TestAttachWarm:
    """Task 8.2: sandbox attach — warm pass."""

    def test_attach_warm_instance_hands_over_terminal(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._warm_check", return_value=True),
            patch("cli.main._phase_handover") as mock_handover,
        ):
            result = runner.invoke(app, ["attach"])
            assert result.exit_code == 0
            mock_handover.assert_called_once()


class TestAttachCold:
    """Task 8.2: sandbox attach — cold reject."""

    def test_attach_cold_instance_rejects(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._warm_check", return_value=False),
        ):
            result = runner.invoke(app, ["attach"])
            assert result.exit_code == 1
            assert "not running" in result.output.lower()


# ── sandbox destroy ──────────────────────────────────────────────────────────


class TestDestroyConfirmation:
    """Task 8.2: sandbox destroy — confirmation accepted/rejected."""

    def test_destroy_accepted_performs_full_teardown(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)
        _write_ipam(instance_id, 0)

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._compose_down") as mock_down,
            patch("cli.main._revoke_acls"),
            patch("cli.main._release_lock"),
            patch("shutil.rmtree") as mock_rmtree,
        ):
            # User types correct name to confirm
            result = runner.invoke(app, ["destroy"], input="myproject\n")
            assert result.exit_code == 0
            mock_down.assert_called_once()
            mock_rmtree.assert_called_once()

    def test_destroy_rejected_aborts_silently(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("shutil.rmtree") as mock_rmtree,
        ):
            result = runner.invoke(app, ["destroy"], input="wrong-name\n")
            assert result.exit_code == 0
            mock_rmtree.assert_not_called()

    def test_destroy_force_bypasses_confirmation(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)
        _write_ipam(instance_id, 0)

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._compose_down"),
            patch("cli.main._revoke_acls"),
            patch("cli.main._release_lock"),
            patch("shutil.rmtree") as mock_rmtree,
        ):
            result = runner.invoke(app, ["destroy", "--force"])
            assert result.exit_code == 0
            mock_rmtree.assert_called_once()


class TestDestroyPrefixGuard:
    """Task 8.2: sandbox destroy — prefix guard triggered."""

    def test_destroy_rejects_path_outside_sandboxes(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        # Manually write a corrupted registry pointing outside sandboxes/
        reg = home / ".state" / "instances.json"
        reg.write_text(json.dumps({project_dir: "../../../etc"}))
        _write_ipam("../../../etc", 0)

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("shutil.rmtree") as mock_rmtree,
        ):
            result = runner.invoke(app, ["destroy", "--force"])
            assert result.exit_code == 1
            mock_rmtree.assert_not_called()


class TestDestroyIPAMAndRegistryCleanup:
    """Task 8.2: IPAM+registry cleared after destroy."""

    def test_destroy_clears_ipam_and_registry(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)
        _write_ipam(instance_id, 5)

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._compose_down"),
            patch("cli.main._revoke_acls"),
            patch("cli.main._release_lock"),
            patch("shutil.rmtree"),
        ):
            result = runner.invoke(app, ["destroy", "--force"])
            assert result.exit_code == 0

            # Verify IPAM entry removed
            ipam_data = json.loads((home / ".state" / "ipam.json").read_text())
            assert instance_id not in ipam_data

            # Verify registry entry removed
            reg_data = json.loads((home / ".state" / "instances.json").read_text())
            assert project_dir not in reg_data


class TestDestroyRmtree:
    """Task 8.2: shutil.rmtree called on instance_dir."""

    def test_destroy_removes_instance_directory(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)
        _write_ipam(instance_id, 0)

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._compose_down"),
            patch("cli.main._revoke_acls"),
            patch("cli.main._release_lock"),
            patch("shutil.rmtree") as mock_rmtree,
        ):
            runner.invoke(app, ["destroy", "--force"])
            expected_dir = str(home / "sandboxes" / instance_id)
            mock_rmtree.assert_called_once_with(expected_dir)


# ── Helper function unit tests (coverage) ────────────────────────────────────


class TestResolveHelpers:
    """Direct tests for _resolve_sandbox_ai_home and _resolve_project_dir."""

    def test_resolve_sandbox_ai_home_returns_repo_root(self) -> None:
        from cli.main import _resolve_sandbox_ai_home

        result = _resolve_sandbox_ai_home()
        assert os.path.isabs(result)
        # Should be the repo root (parent of src/)
        assert os.path.isdir(os.path.join(result, "src", "cli"))

    def test_resolve_project_dir_returns_cwd(self) -> None:
        from cli.main import _resolve_project_dir

        result = _resolve_project_dir()
        assert result == os.path.abspath(os.getcwd())

    def test_resolve_instance_found(self, mock_sandbox_ai_home: Path) -> None:
        from cli.main import _resolve_instance

        home = mock_sandbox_ai_home
        _register_instance(home, "/some/dir", "inst-abc")
        idir, iid = _resolve_instance(str(home), "/some/dir")
        assert iid == "inst-abc"
        assert idir is not None
        assert idir.endswith("sandboxes/inst-abc")

    def test_resolve_instance_not_found(self, mock_sandbox_ai_home: Path) -> None:
        from cli.main import _resolve_instance

        idir, iid = _resolve_instance(str(mock_sandbox_ai_home), "/nonexistent")
        assert idir is None
        assert iid is None

    def test_load_config(self, mock_sandbox_ai_home: Path) -> None:
        from cli.main import _load_config

        home = mock_sandbox_ai_home
        inst = _register_instance(home, "/some/dir", "inst-abc")
        config = _load_config(str(inst))
        assert config.instance.name == "myproject"


class TestWarmCheckDirect:
    """Direct tests for _warm_check — delegates to _container_status (D-3)."""

    def test_warm_check_no_compose_file(self, tmp_path: Path) -> None:
        from cli.main import _warm_check

        assert _warm_check(str(tmp_path), "name", "sandbox") is False

    def test_warm_check_returns_true_when_containers_present(self, tmp_path: Path) -> None:
        from cli.main import ContainerInfo, _warm_check

        compose = tmp_path / "docker" / "compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("version: '3'")

        containers = [
            ContainerInfo(name="t-core-1", service="core", state="running", health="healthy", status="Up"),
        ]
        with (
            patch("cli.main._load_config"),
            patch("cli.main._container_status", return_value=containers),
        ):
            assert _warm_check(str(tmp_path), "name", "sandbox") is True

    def test_warm_check_returns_false_when_empty(self, tmp_path: Path) -> None:
        from cli.main import _warm_check

        compose = tmp_path / "docker" / "compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("version: '3'")

        with (
            patch("cli.main._load_config"),
            patch("cli.main._container_status", return_value=[]),
        ):
            assert _warm_check(str(tmp_path), "name", "sandbox") is False

    def test_warm_check_returns_false_on_container_status_error(self, tmp_path: Path) -> None:
        from cli.main import _warm_check

        compose = tmp_path / "docker" / "compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("version: '3'")

        # _container_status returns [] on error internally, so _warm_check sees empty
        with (
            patch("cli.main._load_config"),
            patch("cli.main._container_status", return_value=[]),
        ):
            assert _warm_check(str(tmp_path), "name", "sandbox") is False


class TestLockingDirect:
    """Direct tests for _acquire_state_lock and _release_lock."""

    def test_acquire_and_release(self, tmp_path: Path) -> None:
        from cli.main import _acquire_state_lock, _release_lock

        fd = _acquire_state_lock(str(tmp_path))
        assert isinstance(fd, int)
        _release_lock(fd)

    def test_acquire_contention(self, isolated_sandbox_ai_user_home: Path) -> None:
        import fcntl as _fcntl

        from cli.main import _acquire_state_lock
        from core.host_config import state_lock_path

        lock_path = state_lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        held_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        _fcntl.flock(held_fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        try:
            with pytest.raises(BlockingIOError):
                _acquire_state_lock(str(isolated_sandbox_ai_user_home))
        finally:
            _fcntl.flock(held_fd, _fcntl.LOCK_UN)
            os.close(held_fd)

    def test_release_lock_handles_bad_fd(self) -> None:
        from cli.main import _release_lock

        # Should not raise even with invalid fd
        _release_lock(999999)


class TestPhaseIPAMDirect:
    """Direct test for _phase_ipam."""

    def test_phase_ipam_allocates(self, mock_sandbox_ai_home: Path) -> None:
        from cli.main import _phase_ipam

        idx = _phase_ipam(str(mock_sandbox_ai_home), "test-instance")
        assert idx == 0


class TestPhaseCredentialsDirect:
    """Direct test for _phase_credentials."""

    def test_phase_credentials_writes_htpasswd(self, tmp_path: Path) -> None:
        from cli.main import _phase_credentials

        proxy_dir = tmp_path / "config" / "proxy"
        proxy_dir.mkdir(parents=True)
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir(parents=True)

        with (
            patch("cli.main.write_htpasswd") as mock_write,
            patch("cli.main.generate_ssh_keypair"),
        ):
            password = _phase_credentials(str(tmp_path), core_ipc_ip="10.100.6.3")
            assert len(password) > 0
            mock_write.assert_called_once()
            line = mock_write.call_args[0][1]
            assert line.startswith("proxyuser:$2b$")


class TestCredentialOwnershipMatching:
    """10.T: _phase_credential_ownership invokes helper container for credential ownership.

    Implements: cli-start/spec.md §Instance Pre-Flight Checks,
                ssh-ipc-transport/spec.md §Credential Ownership Matching
    """

    def test_phase_credential_ownership_invokes_chown_via_machinectl(self, tmp_path: Path) -> None:
        """_phase_credential_ownership runs docker chown via machinectl."""
        from cli.main import _phase_credential_ownership

        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir(parents=True)

        mock_executor_instance = MagicMock()
        mock_executor_instance.run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )

        with patch("cli.main.Executor", return_value=mock_executor_instance):
            _phase_credential_ownership(
                str(tmp_path),
                host_user="claude-sandbox",
                secrets_dir=str(secrets_dir),
            )
            # Executor.run must be called once for the chown operation
            mock_executor_instance.run.assert_called_once()

    def test_chown_command_targets_all_four_secret_files(self, tmp_path: Path) -> None:
        """Helper container chowns all four IPC secret files."""
        from cli.main import _phase_credential_ownership

        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir(parents=True)

        mock_executor_instance = MagicMock()
        mock_executor_instance.run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )

        with patch("cli.main.Executor", return_value=mock_executor_instance):
            _phase_credential_ownership(
                str(tmp_path),
                host_user="claude-sandbox",
                secrets_dir=str(secrets_dir),
            )
            call_args = mock_executor_instance.run.call_args[0][0]
            cmd_str = " ".join(call_args)
            # Must reference all four secret files
            for secret in ("ipc_host_key", "authorized_keys", "ipc_ssh_key", "ipc_known_hosts"):
                assert secret in cmd_str, f"chown command must target {secret}, got: {cmd_str}"

    def test_chown_uses_docker_run_with_runc_runtime(self, tmp_path: Path) -> None:
        """Helper container uses docker run --rm --runtime=runc busybox."""
        from cli.main import _phase_credential_ownership

        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir(parents=True)

        mock_executor_instance = MagicMock()
        mock_executor_instance.run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )

        with patch("cli.main.Executor", return_value=mock_executor_instance):
            _phase_credential_ownership(
                str(tmp_path),
                host_user="claude-sandbox",
                secrets_dir=str(secrets_dir),
            )
            call_args = mock_executor_instance.run.call_args[0][0]
            cmd_str = " ".join(call_args)
            assert "docker run" in cmd_str, f"Must use docker run, got: {cmd_str}"
            assert "--rm" in cmd_str, f"Must use --rm, got: {cmd_str}"
            assert "--runtime=runc" in cmd_str, f"Must use --runtime=runc, got: {cmd_str}"
            assert "busybox" in cmd_str, f"Must use busybox image, got: {cmd_str}"
            assert "chown 1000:1000" in cmd_str, f"Must chown to 1000:1000, got: {cmd_str}"

    def test_chown_executed_via_machinectl_shell(self, tmp_path: Path) -> None:
        """Chown command is executed via machinectl shell as docker_unprivileged_user."""
        from cli.main import _phase_credential_ownership

        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir(parents=True)

        mock_executor_instance = MagicMock()
        mock_executor_instance.run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )

        with patch("cli.main.Executor", return_value=mock_executor_instance):
            _phase_credential_ownership(
                str(tmp_path),
                host_user="claude-sandbox",
                secrets_dir=str(secrets_dir),
            )
            call_args = mock_executor_instance.run.call_args[0][0]
            assert "sudo" in call_args, "Must use sudo"
            assert "machinectl" in call_args, "Must use machinectl"
            assert "shell" in call_args, "Must use shell subcommand"
            assert "claude-sandbox@.host" in call_args, "Must execute as docker_unprivileged_user"

    def test_chown_failure_raises_execution_error(self, tmp_path: Path) -> None:
        """Helper container failure wraps and re-raises SandboxExecutionError."""
        from cli.main import _phase_credential_ownership
        from core.exceptions import SandboxExecutionError

        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir(parents=True)

        mock_executor_instance = MagicMock()
        mock_executor_instance.run.side_effect = SandboxExecutionError("chown failed")

        with (
            patch("cli.main.Executor", return_value=mock_executor_instance),
            pytest.raises(SandboxExecutionError, match="Credential ownership matching failed"),
        ):
            _phase_credential_ownership(
                str(tmp_path),
                host_user="claude-sandbox",
                secrets_dir=str(secrets_dir),
            )


class TestPhaseCredentialOwnership:
    """Tasks 1.1, 1.2 RED: _phase_credential_ownership() unit tests.

    Implements: ssh-ipc-transport/spec.md §Credential Ownership Matching
    """

    def test_invokes_chown_via_machinectl_with_runc(self, tmp_path: Path) -> None:
        """_phase_credential_ownership calls Executor.run with machinectl shell + docker run
        --rm --runtime=runc busybox chown 1000:1000 targeting all four secret files."""
        from cli.main import _phase_credential_ownership

        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir(parents=True)

        mock_executor_instance = MagicMock()
        mock_executor_instance.run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )

        with (
            patch("cli.main.Executor", return_value=mock_executor_instance),
            patch("subprocess.run") as mock_run,
        ):
            _phase_credential_ownership(
                str(tmp_path),
                host_user="claude-sandbox",
                secrets_dir=str(secrets_dir),
            )

            assert mock_run.call_count == 2
            escalation_call = mock_run.call_args_list[0]
            downgrade_call = mock_run.call_args_list[1]
            assert escalation_call[0][0] == ["setfacl", "-m", "u:claude-sandbox:rwX", str(secrets_dir)]
            assert downgrade_call[0][0] == ["setfacl", "-m", "u:claude-sandbox:rX", str(secrets_dir)]

            mock_executor_instance.run.assert_called_once()
            call_args = mock_executor_instance.run.call_args[0][0]
            cmd_str = " ".join(call_args)

            # machinectl shell envelope
            assert "sudo" in call_args, "Must use sudo"
            assert "machinectl" in call_args, "Must use machinectl"
            assert "shell" in call_args, "Must use shell subcommand"
            assert "claude-sandbox@.host" in call_args, "Must execute as docker_unprivileged_user"
            # docker run structure
            assert "docker run" in cmd_str, f"Must use docker run, got: {cmd_str}"
            assert "--rm" in cmd_str, f"Must use --rm, got: {cmd_str}"
            assert "--runtime=runc" in cmd_str, f"Must use --runtime=runc, got: {cmd_str}"
            assert "busybox" in cmd_str, f"Must use busybox image, got: {cmd_str}"
            assert f"-v {secrets_dir}:/secrets" in cmd_str, f"Must bind-mount secrets directory, got: {cmd_str}"
            assert "cp /secrets/" in cmd_str, f"Must use copy-replace mutator, got: {cmd_str}"
            assert "chown 1000:1000" in cmd_str, f"Must chown to 1000:1000, got: {cmd_str}"
            # All four secret files
            for secret in ("ipc_host_key", "authorized_keys", "ipc_ssh_key", "ipc_known_hosts"):
                assert secret in cmd_str, f"chown command must target {secret}, got: {cmd_str}"

    def test_failure_raises_execution_error_with_prefix(self, tmp_path: Path) -> None:
        """Helper container failure raises SandboxExecutionError with
        'Credential ownership matching failed:' prefix."""
        from cli.main import _phase_credential_ownership
        from core.exceptions import SandboxExecutionError

        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir(parents=True)

        mock_executor_instance = MagicMock()
        mock_executor_instance.run.side_effect = SandboxExecutionError("chown failed")

        with (
            patch("cli.main.Executor", return_value=mock_executor_instance),
            patch("subprocess.run") as mock_run,
        ):
            with pytest.raises(SandboxExecutionError, match="Credential ownership matching failed"):
                _phase_credential_ownership(
                    str(tmp_path),
                    host_user="claude-sandbox",
                    secrets_dir=str(secrets_dir),
                )
            assert mock_run.call_count == 2

    def test_acl_escalation_failure_raises_error(self, tmp_path: Path) -> None:
        from cli.main import _phase_credential_ownership
        from core.exceptions import SandboxExecutionError

        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir(parents=True)

        with (
            patch("cli.main.Executor"),
            patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "setfacl")),
            pytest.raises(SandboxExecutionError, match="Failed to escalate ACLs for secrets directory"),
        ):
            _phase_credential_ownership(
                str(tmp_path),
                host_user="claude-sandbox",
                secrets_dir=str(secrets_dir),
            )

    def test_acl_downgrade_failure_raises_fix_diagnostic(self, tmp_path: Path) -> None:
        import re

        from cli.main import _phase_credential_ownership
        from core.exceptions import SandboxExecutionError

        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir(parents=True)

        def mock_run(cmd: list[str], *args: typing.Any, **kwargs: typing.Any) -> MagicMock:
            if cmd[2] == "u:claude-sandbox:rX":
                raise subprocess.CalledProcessError(1, cmd)
            return MagicMock()

        expected_match = re.escape(f"Fix: sudo setfacl -m u:claude-sandbox:rX {secrets_dir}")
        with (
            patch("cli.main.Executor"),
            patch("subprocess.run", side_effect=mock_run),
            pytest.raises(SandboxExecutionError, match=expected_match),
        ):
            _phase_credential_ownership(
                str(tmp_path),
                host_user="claude-sandbox",
                secrets_dir=str(secrets_dir),
            )


class TestPhaseHydrateDirect:
    """Direct test for _phase_hydrate."""

    def test_phase_hydrate_calls_render(self) -> None:
        from cli.main import _phase_hydrate
        from core.host_config import HostSettings
        from core.hydration import InstanceConfig

        mock_config = InstanceConfig.model_validate(
            {
                "instance": {
                    "name": "test",
                    "user_project_root": "/home/dev/test",
                    "host_uid": "1000",
                }
            }
        )
        host = HostSettings(docker_unprivileged_user="claude-sandbox")

        with (
            patch("cli.main.build_jinja_context", return_value={}) as mock_ctx,
            patch("cli.main.render_templates") as mock_render,
        ):
            _phase_hydrate(mock_config, 0, "pass", "/home", "/inst", host)
            mock_ctx.assert_called_once()
            mock_render.assert_called_once()


class TestPhaseACLDirect:
    """Direct test for _phase_acl_grant."""

    def test_phase_acl_grant_calls_setfacl_for_all_plan_entries(self, tmp_path: Path) -> None:
        from cli.main import _phase_acl_grant

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)
        (instance_dir / "docker").mkdir()
        (instance_dir / "config").mkdir()
        (instance_dir / ".sandbox.env").write_text("")

        with patch("subprocess.run") as mock_run:
            _phase_acl_grant(str(instance_dir), "sandbox")
            # At minimum: instance root + docker/ + config/ + .sandbox.env = 4
            # Plus ancestors (varies by tmp_path depth)
            assert mock_run.call_count >= 4
            # Verify setfacl is called in every invocation
            for call in mock_run.call_args_list:
                assert call[0][0][0] == "setfacl"


class TestBuildComposeFiles:
    """Direct test for _build_compose_files."""

    def test_base_only(self) -> None:
        from cli.main import _build_compose_files
        from core.hydration import InstanceConfig

        config = InstanceConfig.model_validate(
            {
                "instance": {
                    "name": "t",
                    "user_project_root": "/x",
                    "host_uid": "1000",
                },
                "components": {"mcp_firecrawl": False, "mcp_puppeteer": False},
                "components_db_postgres": {"enabled": False},
            }
        )
        files = _build_compose_files("/inst", config)
        assert len(files) == 2  # -f, path

    def test_with_extras(self) -> None:
        from cli.main import _build_compose_files
        from core.hydration import InstanceConfig

        config = InstanceConfig.model_validate(
            {
                "instance": {
                    "name": "t",
                    "user_project_root": "/x",
                    "host_uid": "1000",
                },
                "components": {"mcp_firecrawl": True, "mcp_puppeteer": False},
                "components_db_postgres": {"enabled": True},
            }
        )
        files = _build_compose_files("/inst", config)
        assert len(files) == 6  # base + postgres + firecrawl


class TestPhaseComposeUpDirect:
    """Direct test for _phase_compose_up."""

    def test_compose_up_calls_executor(self) -> None:
        from cli.main import _phase_compose_up
        from core.hydration import InstanceConfig

        config = InstanceConfig.model_validate(
            {
                "instance": {
                    "name": "t",
                    "user_project_root": "/x",
                    "host_uid": "1000",
                },
            }
        )

        with patch("cli.main.Executor") as MockExec:
            _phase_compose_up("/inst", "myproj", "sandbox", config)
            MockExec.return_value.run.assert_called_once()
            cmd_args = MockExec.return_value.run.call_args[0][0]
            assert "machinectl" in cmd_args
            assert "up -d --build --wait" in cmd_args[-1]


class TestPhaseHandoverDirect:
    """Direct test for _phase_handover."""

    def test_handover_without_warmup(self) -> None:
        from cli.main import _phase_handover

        with patch("cli.main.Executor") as MockExec:
            _phase_handover("myproj", "sandbox")
            cmd = MockExec.return_value.run.call_args[0][0]
            assert "/usr/bin/docker" in cmd
            assert "-it" in cmd
            assert "SANDBOX_WARMUP_PROMPT" not in " ".join(cmd)

    def test_handover_with_warmup(self) -> None:
        from cli.main import _phase_handover

        with patch("cli.main.Executor") as MockExec:
            _phase_handover("myproj", "sandbox", warmup_prompt="do things")
            cmd = MockExec.return_value.run.call_args[0][0]
            assert any("SANDBOX_WARMUP_PROMPT" in arg for arg in cmd)


class TestComposeDownDirect:
    """Direct test for _compose_down."""

    def test_compose_down_plain(self) -> None:
        from cli.main import _compose_down
        from core.hydration import InstanceConfig

        config = InstanceConfig.model_validate(
            {
                "instance": {
                    "name": "t",
                    "user_project_root": "/x",
                    "host_uid": "1000",
                },
            }
        )

        with patch("cli.main.Executor") as MockExec:
            _compose_down("/inst", "myproj", "sandbox", config, volumes=False)
            cmd_str = MockExec.return_value.run.call_args[0][0][-1]
            assert "down" in cmd_str
            assert " -v" not in cmd_str

    def test_compose_down_volumes(self) -> None:
        from cli.main import _compose_down
        from core.hydration import InstanceConfig

        config = InstanceConfig.model_validate(
            {
                "instance": {
                    "name": "t",
                    "user_project_root": "/x",
                    "host_uid": "1000",
                },
            }
        )

        with patch("cli.main.Executor") as MockExec:
            _compose_down("/inst", "myproj", "sandbox", config, volumes=True)
            cmd_str = MockExec.return_value.run.call_args[0][0][-1]
            assert "down -v" in cmd_str


class TestRevokeACLsDirect:
    """Task 4.3: Fault-isolated _revoke_acls — partial failure, all targets, warnings.

    Post-acl-ownership-recipes: cache/log Option-B grants are gone; workspace
    named-ACL is now revoked. Plan entries when user_project_root is provided:
    instance root + docker/ + config/ + .sandbox.env + secrets/ + workspace
    (effective + default named-entry) = 7.
    """

    def test_revoke_acls_calls_setfacl_for_all_plan_entries(self) -> None:
        from cli.main import _revoke_acls

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")
            warnings = _revoke_acls("/inst", "sandbox", "/home/dev/proj")
            assert mock_run.call_count == 7
            assert warnings == []

    def test_partial_failure_continues_and_collects_warnings(self) -> None:
        """WHEN one setfacl fails, THEN remaining targets still attempted."""
        from cli.main import _revoke_acls

        call_count = 0

        def side_effect(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return subprocess.CompletedProcess([], 1, "", "No such file")
            return subprocess.CompletedProcess([], 0, "", "")

        with patch("subprocess.run", side_effect=side_effect):
            warnings = _revoke_acls("/inst", "sandbox", "/home/dev/proj")

        assert call_count == 7
        assert len(warnings) == 1
        assert "No such file" in warnings[0]

    def test_oserror_collected_as_warning(self) -> None:
        """WHEN setfacl raises OSError, THEN collected as warning, not raised."""
        from cli.main import _revoke_acls

        with patch("subprocess.run", side_effect=OSError("setfacl not found")):
            warnings = _revoke_acls("/inst", "sandbox", "/home/dev/proj")

        assert len(warnings) == 7
        assert all("setfacl not found" in w for w in warnings)


class TestInitScaffoldDirect:
    """Task 9.1: init command creates full instance (migrated from _scaffold_instance)."""

    def test_init_creates_full_instance(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        from cli.main import app
        from core.hydration import InstanceConfig

        home = mock_sandbox_ai_home
        project_dir = "/home/dev/newproject"

        mock_config = InstanceConfig.model_validate(
            {
                "instance": {
                    "name": "newproject",
                    "user_project_root": project_dir,
                    "host_uid": "1000",
                },
            }
        )

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._detect_git_config", return_value=("Jane", "j@e.com")),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "ok\n", "")),
            patch("cli.main.create_instance_dirs") as mock_dirs,
            patch("cli.main.write_sandbox_toml") as mock_toml,
            patch("cli.main._load_config", return_value=mock_config),
            patch("cli.main.create_env_file") as mock_env,
            patch("cli.main.apply_default_acls") as mock_acls,
            patch("cli.main.prompt_secrets") as mock_secrets,
            patch("cli.main.write_initialized_sentinel") as mock_sentinel,
        ):
            result = runner.invoke(app, ["init"])
            assert result.exit_code == 0
            mock_dirs.assert_called_once()
            mock_toml.assert_called_once()
            mock_env.assert_called_once()
            mock_acls.assert_called_once()
            from unittest.mock import ANY

            mock_secrets.assert_called_once_with(
                ANY,
                [
                    ("CORE_ANTHROPIC_API_KEY", "Anthropic API key"),
                    ("CORE_GITHUB_TOKEN", "GitHub personal access token"),
                ],
                prompt_func=ANY,
            )
            mock_sentinel.assert_called_once()


# ── Edge case tests for remaining coverage ───────────────────────────────────


class TestStopNoInstance:
    """Cover stop with unregistered instance."""

    def test_stop_no_instance_exits(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(mock_sandbox_ai_home)),
            patch("cli.main._resolve_project_dir", return_value="/nonexistent"),
        ):
            result = runner.invoke(app, ["stop"])
            assert result.exit_code == 1
            assert "no sandbox" in result.output.lower()


class TestAttachNoInstance:
    """Cover attach with unregistered instance."""

    def test_attach_no_instance_exits(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(mock_sandbox_ai_home)),
            patch("cli.main._resolve_project_dir", return_value="/nonexistent"),
        ):
            result = runner.invoke(app, ["attach"])
            assert result.exit_code == 1
            assert "no sandbox" in result.output.lower()


class TestDestroyNoInstance:
    """Cover destroy with unregistered instance."""

    def test_destroy_no_instance_exits(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(mock_sandbox_ai_home)),
            patch("cli.main._resolve_project_dir", return_value="/nonexistent"),
        ):
            result = runner.invoke(app, ["destroy", "--force"])
            assert result.exit_code == 1
            assert "no sandbox" in result.output.lower()


class TestDestroyPrefixGuardInternal:
    """Cover the internal prefix guard path (not mocked)."""

    def test_prefix_guard_rejects_bad_path(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        home = mock_sandbox_ai_home

        from cli.main import app

        _seed_registry(_user_home())
        # Mock _resolve_instance to return a path outside sandboxes/
        bad_path = str(home / "somewhere_else" / "evil")
        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value="/some/dir"),
            patch("cli.main._resolve_instance", return_value=(bad_path, "evil")),
        ):
            result = runner.invoke(app, ["destroy", "--force"])
            assert result.exit_code == 1
            assert "prefix guard" in result.output.lower()


class TestInitFirecrawl:
    """Task 9.1: init command's firecrawl branch (migrated from _scaffold_instance)."""

    def test_init_with_firecrawl_includes_secret(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        from cli.main import app
        from core.hydration import InstanceConfig

        home = mock_sandbox_ai_home
        project_dir = "/home/dev/fc-project"

        mock_config = InstanceConfig.model_validate(
            {
                "instance": {
                    "name": "fc-project",
                    "user_project_root": project_dir,
                    "host_uid": "1000",
                },
                "components": {"mcp_firecrawl": True, "mcp_puppeteer": False},
                "components_db_postgres": {"enabled": True},
            }
        )

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._detect_git_config", return_value=("", "")),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "ok\n", "")),
            patch("cli.main.create_instance_dirs"),
            patch("cli.main.write_sandbox_toml"),
            patch("cli.main._load_config", return_value=mock_config),
            patch("cli.main.create_env_file"),
            patch("cli.main.apply_default_acls"),
            patch("cli.main.prompt_secrets") as mock_prompt,
            patch("cli.main.write_initialized_sentinel"),
        ):
            result = runner.invoke(app, ["init"])
            assert result.exit_code == 0
            # Verify firecrawl secret was included in prompt_secrets call
            call_args = mock_prompt.call_args[0]
            secret_names = [s[0] for s in call_args[1]]
            assert "FIRECRAWL_API_KEY" in secret_names
            # Task 5.2/5.7: PG_PASSWORD must NOT be in prompt list
            assert "PG_PASSWORD" not in secret_names


# ── sandbox doctor ───────────────────────────────────────────────────────────


class TestDoctorAllPass:
    """Task 10.1: sandbox doctor --user — all checks pass."""

    def test_doctor_all_pass_exit_code_0(self, runner: CliRunner) -> None:
        from cli.main import app
        from core.doctor import CheckResult

        all_pass = [CheckResult(status="pass", name=f"check-{i}", detail="ok") for i in range(12)]
        with (
            patch("cli.main.detect_distro", return_value="debian"),
            patch("cli.main.build_check_registry", return_value=[]),
            patch("cli.main.run_checks", return_value=all_pass),
            patch("cli.main.render_results"),
        ):
            result = runner.invoke(app, ["doctor", "--user", "sandbox"])
            assert result.exit_code == 0


class TestDoctorPerUserHomeDisplay:
    """Doctor output displays the resolved per-user home (env var visibility)."""

    def test_displays_resolved_home(self, runner: CliRunner, isolated_sandbox_ai_user_home: Path) -> None:
        from cli.main import app
        from core.doctor import CheckResult

        with (
            patch("cli.main.detect_distro", return_value="debian"),
            patch("cli.main.build_check_registry", return_value=[]),
            patch("cli.main.run_checks", return_value=[CheckResult(status="pass", name="x", detail="")]),
            patch("cli.main.render_results"),
        ):
            result = runner.invoke(app, ["doctor", "--user", "sandbox"])
        assert result.exit_code == 0
        assert "Per-user home:" in result.output
        assert str(isolated_sandbox_ai_user_home) in result.output


class TestDoctorAnyFail:
    """Task 10.1: sandbox doctor --user — some checks fail."""

    def test_doctor_any_fail_exit_code_1(self, runner: CliRunner) -> None:
        from cli.main import app
        from core.doctor import CheckResult

        mixed = [
            CheckResult(status="pass", name="a", detail="ok"),
            CheckResult(status="fail", name="b", detail="bad", remediation="fix"),
        ]
        with (
            patch("cli.main.detect_distro", return_value=None),
            patch("cli.main.build_check_registry", return_value=[]),
            patch("cli.main.run_checks", return_value=mixed),
            patch("cli.main.render_results"),
        ):
            result = runner.invoke(app, ["doctor", "--user", "sandbox"])
            assert result.exit_code == 1


class TestDoctorMissingUser:
    """Task 10.1: sandbox doctor without --user errors."""

    def test_doctor_missing_user_exits_error(self, runner: CliRunner) -> None:
        from cli.main import app

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code != 0


class TestDoctorHostConfig:
    """Section 5 (host-config-machinectl-auth): doctor reads sandbox-ai.toml."""

    def test_doctor_resolves_user_from_host_config(self, runner: CliRunner) -> None:
        from cli.main import app
        from core.doctor import CheckResult
        from core.host_config import HostConfig, MachinectlAuth

        mock_pc = HostConfig.model_validate(
            {"host": {"docker_unprivileged_user": "fromtoml", "machinectl_authentication": "polkit"}}
        )
        results = [CheckResult(status="pass", name="ok", detail="")]
        with (
            patch("cli.main.HostConfig.from_toml", return_value=mock_pc),
            patch("cli.main.detect_distro", return_value=None),
            patch("cli.main.build_check_registry", return_value=[]) as mock_reg,
            patch("cli.main.run_checks", return_value=results) as mock_run,
            patch("cli.main.render_results"),
        ):
            r = runner.invoke(app, ["doctor"])
            assert r.exit_code == 0
            mock_reg.assert_called_once_with(MachinectlAuth.POLKIT)
            mock_run.assert_called_once_with([], "fromtoml", None)

    def test_doctor_user_flag_overrides_project_config(self, runner: CliRunner) -> None:
        from cli.main import app
        from core.doctor import CheckResult
        from core.host_config import HostConfig, MachinectlAuth

        mock_pc = HostConfig.model_validate(
            {"host": {"docker_unprivileged_user": "fromtoml", "machinectl_authentication": "sudo"}}
        )
        results = [CheckResult(status="pass", name="ok", detail="")]
        with (
            patch("cli.main.HostConfig.from_toml", return_value=mock_pc),
            patch("cli.main.detect_distro", return_value=None),
            patch("cli.main.build_check_registry", return_value=[]) as mock_reg,
            patch("cli.main.run_checks", return_value=results) as mock_run,
            patch("cli.main.render_results"),
        ):
            r = runner.invoke(app, ["doctor", "--user", "cliuser", "--machinectl-auth", "polkit"])
            assert r.exit_code == 0
            mock_reg.assert_called_once_with(MachinectlAuth.POLKIT)
            mock_run.assert_called_once_with([], "cliuser", None)

    def test_doctor_no_config_no_flag_errors(self, runner: CliRunner) -> None:
        from cli.main import app

        with patch("cli.main.HostConfig.from_toml", side_effect=FileNotFoundError):
            r = runner.invoke(app, ["doctor"])
        assert r.exit_code == 1
        assert "no user specified" in r.output.lower()

    def test_doctor_invalid_auth_mode_errors(self, runner: CliRunner) -> None:
        from cli.main import app

        with patch("cli.main.HostConfig.from_toml", side_effect=FileNotFoundError):
            r = runner.invoke(app, ["doctor", "--user", "sandbox", "--machinectl-auth", "bogus"])
        assert r.exit_code == 1
        assert "invalid" in r.output.lower()

    def test_doctor_defaults_auth_to_sudo_when_no_config(self, runner: CliRunner) -> None:
        from cli.main import app
        from core.doctor import CheckResult
        from core.host_config import MachinectlAuth

        results = [CheckResult(status="pass", name="ok", detail="")]
        with (
            patch("cli.main.detect_distro", return_value=None),
            patch("cli.main.build_check_registry", return_value=[]) as mock_reg,
            patch("cli.main.run_checks", return_value=results),
            patch("cli.main.render_results"),
            patch("cli.main.HostConfig.from_toml", side_effect=FileNotFoundError),
        ):
            r = runner.invoke(app, ["doctor", "--user", "sandbox"])
            assert r.exit_code == 0
            mock_reg.assert_called_once_with(MachinectlAuth.SUDO)


class TestDoctorRunnerInvoked:
    """Task 10.1: verify runner is invoked with correct arguments."""

    def test_runner_receives_user_and_distro(self, runner: CliRunner) -> None:
        from cli.main import app
        from core.doctor import CheckResult

        results = [CheckResult(status="pass", name="a", detail="ok")]
        with (
            patch("cli.main.detect_distro", return_value="fedora") as mock_distro,
            patch("cli.main.build_check_registry", return_value=["check_obj"]) as mock_reg,
            patch("cli.main.run_checks", return_value=results) as mock_run,
            patch("cli.main.render_results") as mock_render,
        ):
            runner.invoke(app, ["doctor", "--user", "testuser"])
            mock_distro.assert_called_once()
            mock_reg.assert_called_once()
            mock_run.assert_called_once_with(["check_obj"], "testuser", "fedora")
            mock_render.assert_called_once()


# ── sandbox init ─────────────────────────────────────────────────────────────


class TestInitHappyPath:
    """Task 3.1: sandbox init --user — happy path scaffold."""

    def test_init_creates_instance(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """init scaffolds a new instance successfully."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/newproject"

        from cli.main import app
        from core.hydration import InstanceConfig

        mock_config = InstanceConfig.model_validate(
            {
                "instance": {
                    "name": "newproject",
                    "user_project_root": project_dir,
                    "host_uid": "1000",
                },
            }
        )

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._detect_git_config", return_value=("Jane", "j@e.com")),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "ok\n", "")),
            patch("cli.main.create_instance_dirs"),
            patch("cli.main.write_sandbox_toml"),
            patch("cli.main._load_config", return_value=mock_config),
            patch("cli.main.create_env_file"),
            patch("cli.main.apply_default_acls"),
            patch("cli.main.prompt_secrets"),
            patch("cli.main.write_initialized_sentinel"),
        ):
            result = runner.invoke(app, ["init"])
            assert result.exit_code == 0, result.output


class TestInitPerUserTreeCreation:
    """init creates `<home>/{config,state}/` with mode 0700 (idempotent)."""

    def _common_patches(
        self,
        home: Path,
        project_dir: str,
        mock_config: object,
    ) -> list[typing.Any]:
        return [
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._detect_git_config", return_value=("", "")),
            patch("cli.main.run_check_subset", return_value=[]),
            patch(
                "cli.main.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, "ok\n", ""),
            ),
            patch("cli.main.create_instance_dirs"),
            patch("cli.main.write_sandbox_toml"),
            patch("cli.main._load_config", return_value=mock_config),
            patch("cli.main.create_env_file"),
            patch("cli.main.apply_default_acls"),
            patch("cli.main.prompt_secrets"),
            patch("cli.main.write_initialized_sentinel"),
        ]

    def _make_mock_config(self, project_dir: str) -> object:
        from core.hydration import InstanceConfig

        return InstanceConfig.model_validate(
            {
                "instance": {
                    "name": "p",
                    "user_project_root": project_dir,
                    "host_uid": "1000",
                },
            }
        )

    def test_clean_install_creates_tree(
        self,
        runner: CliRunner,
        mock_sandbox_ai_home: Path,
        isolated_sandbox_ai_user_home: Path,
    ) -> None:
        """init on a clean host creates <home>/, <home>/config/, <home>/state/."""
        from cli.main import app

        project_dir = "/home/dev/p"
        mock_config = self._make_mock_config(project_dir)
        with contextlib.ExitStack() as es:
            for p in self._common_patches(mock_sandbox_ai_home, project_dir, mock_config):
                es.enter_context(p)
            result = runner.invoke(app, ["init"])
        assert result.exit_code == 0, result.output
        assert isolated_sandbox_ai_user_home.is_dir()
        assert (isolated_sandbox_ai_user_home / "config").is_dir()
        assert (isolated_sandbox_ai_user_home / "state").is_dir()
        assert stat.S_IMODE(isolated_sandbox_ai_user_home.stat().st_mode) == 0o700

    def test_idempotent_on_existing_tree(
        self,
        runner: CliRunner,
        mock_sandbox_ai_home: Path,
        isolated_sandbox_ai_user_home: Path,
    ) -> None:
        """Re-running init against an existing tree does not error."""
        from cli.main import app
        from core.scaffold import ensure_per_user_tree

        ensure_per_user_tree(isolated_sandbox_ai_user_home)
        project_dir = "/home/dev/p"
        mock_config = self._make_mock_config(project_dir)
        with contextlib.ExitStack() as es:
            for p in self._common_patches(mock_sandbox_ai_home, project_dir, mock_config):
                es.enter_context(p)
            result = runner.invoke(app, ["init"])
        assert result.exit_code == 0, result.output

    def test_existing_registry_not_overwritten(
        self,
        runner: CliRunner,
        mock_sandbox_ai_home: Path,
        isolated_sandbox_ai_user_home: Path,
    ) -> None:
        """A pre-populated registry is preserved; init only ensures presence."""
        from cli.main import app
        from core.scaffold import ensure_per_user_tree

        ensure_per_user_tree(isolated_sandbox_ai_user_home)
        registry = isolated_sandbox_ai_user_home / "state" / "instances.json"
        registry.write_text('{"/x": "x-aaa"}')

        project_dir = "/home/dev/p"
        mock_config = self._make_mock_config(project_dir)
        with contextlib.ExitStack() as es:
            for p in self._common_patches(mock_sandbox_ai_home, project_dir, mock_config):
                es.enter_context(p)
            result = runner.invoke(app, ["init"])
        assert result.exit_code == 0, result.output
        # Pre-existing entry preserved
        data = json.loads(registry.read_text())
        assert data["/x"] == "x-aaa"
        # New entry added too
        assert project_dir in data

    def test_sandbox_ai_user_home_redirect(
        self,
        runner: CliRunner,
        mock_sandbox_ai_home: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Setting SANDBOX_AI_USER_HOME redirects creation to that path."""
        from cli.main import app

        custom_home = tmp_path / "alt-home"
        monkeypatch.setenv("SANDBOX_AI_USER_HOME", str(custom_home))
        project_dir = "/home/dev/p"
        mock_config = self._make_mock_config(project_dir)
        with contextlib.ExitStack() as es:
            for p in self._common_patches(mock_sandbox_ai_home, project_dir, mock_config):
                es.enter_context(p)
            result = runner.invoke(app, ["init"])
        assert result.exit_code == 0, result.output
        assert custom_home.is_dir()
        assert (custom_home / "config").is_dir()
        assert (custom_home / "state").is_dir()


class TestInitReInitRejection:
    """Task 3.1: sandbox init — re-init rejected."""

    def test_reinit_rejected(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """init errors when instance already exists."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
        ):
            result = runner.invoke(app, ["init"])
            assert result.exit_code == 1
            assert "already initialized" in result.output.lower() or "destroy" in result.output.lower()


class TestInitDryRun:
    """Task 3.1: sandbox init --dry-run previews without writing."""

    def test_init_dry_run_no_state(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """init --dry-run does not create any files or registry entries."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/newproject"

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._detect_git_config", return_value=("", "")),
        ):
            result = runner.invoke(app, ["init", "--dry-run"])
            assert result.exit_code == 0

        # Registry should be unmodified
        reg_data = json.loads((home / ".state" / "instances.json").read_text())
        assert project_dir not in reg_data


class TestInitDoctorPreFlightFailure:
    """Task 3.1: sandbox init — doctor pre-flight failure aborts."""

    def test_init_aborts_on_doctor_failure(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """init aborts when doctor pre-flight checks fail."""
        from core.doctor import CheckResult

        home = mock_sandbox_ai_home
        project_dir = "/home/dev/newproject"

        from cli.main import app

        failed_results = [CheckResult(status="fail", name="setfacl", detail="not found", remediation="install acl")]
        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "ok\n", "")),
            patch("cli.main.run_check_subset", return_value=failed_results),
            patch("cli.main.render_results"),
        ):
            result = runner.invoke(app, ["init"])
            assert result.exit_code == 1


class TestInitNonTTY:
    """Task 3.1: sandbox init in non-TTY environment."""

    def test_init_non_tty_completes(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """init completes in non-TTY mode (prompt_secrets skips)."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/newproject"

        from cli.main import app
        from core.hydration import InstanceConfig

        mock_config = InstanceConfig.model_validate(
            {
                "instance": {
                    "name": "newproject",
                    "user_project_root": project_dir,
                    "host_uid": "1000",
                },
            }
        )

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._detect_git_config", return_value=("", "")),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "ok\n", "")),
            patch("cli.main.create_instance_dirs"),
            patch("cli.main.write_sandbox_toml"),
            patch("cli.main._load_config", return_value=mock_config),
            patch("cli.main.create_env_file"),
            patch("cli.main.apply_default_acls"),
            patch("cli.main.prompt_secrets"),
            patch("cli.main.write_initialized_sentinel"),
        ):
            result = runner.invoke(app, ["init"])
            assert result.exit_code == 0


class TestRequirePerUserStateInitialized:
    """Lifecycle commands fail with canonical error when state tree is uninitialized."""

    @pytest.mark.parametrize("command", ["start", "stop", "attach", "destroy", "status"])
    def test_command_fails_when_uninitialized(
        self, runner: CliRunner, command: str, isolated_sandbox_ai_user_home: Path
    ) -> None:
        from cli.main import app

        # Ensure registry seed file is absent
        registry = isolated_sandbox_ai_user_home / "state" / "instances.json"
        if registry.exists():
            registry.unlink()

        args = [command]
        if command == "destroy":
            args.append("--force")
        result = runner.invoke(app, args)
        assert result.exit_code == 1
        assert "per-user state not initialized" in result.output.lower()
        assert str(isolated_sandbox_ai_user_home) in result.output
        assert "sandbox init" in result.output.lower()


class TestInitFlagRemoval:
    """The legacy ``--user`` flag has been removed; Typer rejects it."""

    def test_user_flag_rejected(self, runner: CliRunner) -> None:
        from cli.main import app

        result = runner.invoke(app, ["init", "--user", "sandbox"])
        assert result.exit_code != 0
        assert "no such option" in result.output.lower() or "--user" in result.output


class TestInitDryRunNoConfigFallback:
    """Dry-run with absent host config falls back to placeholder values."""

    def test_dry_run_without_config_uses_dry_run_user(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(mock_sandbox_ai_home)),
            patch("cli.main._resolve_project_dir", return_value="/home/dev/dryrunfallback"),
            patch("cli.main._detect_git_config", return_value=("", "")),
            patch("cli.main.HostConfig.from_toml", side_effect=FileNotFoundError),
        ):
            result = runner.invoke(app, ["init", "--dry-run"])
        # dry-run path tolerates missing host config; auth defaults to sudo
        assert result.exit_code == 0, result.output


class TestInitNoConfigPostSeedFails:
    """Defensive branch: missing config after seed (e.g. seed mocked)."""

    def test_missing_config_after_seed_in_non_dry_run_exits(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(mock_sandbox_ai_home)),
            patch("cli.main._resolve_project_dir", return_value="/home/dev/missing"),
            patch("cli.main.HostConfig.from_toml", side_effect=FileNotFoundError),
        ):
            result = runner.invoke(app, ["init"])
        assert result.exit_code == 1
        assert "no host config" in result.output.lower()


class TestStdinIsTty:
    def test_returns_isatty_value(self) -> None:
        from cli.main import _stdin_is_tty

        # Just ensure it returns a bool without raising
        assert isinstance(_stdin_is_tty(), bool)


class TestWarnLegacyCwdFiles:
    def test_warns_on_legacy_toml_and_state(self, tmp_path: Path, captured_console: object) -> None:
        from cli import main as cli_main
        from cli.main import _warn_legacy_cwd_files

        (tmp_path / "sandbox-ai.toml").write_text("")
        (tmp_path / ".state").mkdir()

        with patch.object(cli_main, "console", captured_console.console):  # type: ignore[attr-defined]
            _warn_legacy_cwd_files(str(tmp_path), tmp_path / ".sandbox-ai")
        out = captured_console.plain_output  # type: ignore[attr-defined]
        assert "legacy" in out.lower()
        assert "sandbox-ai.toml" in out
        assert ".state" in out


class TestInitHostConfigResolution:
    """Tasks 3.8-3.9: init user resolution via sandbox-ai.toml."""

    def test_init_with_project_config_no_user_flag(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """init succeeds without --user when sandbox-ai.toml provides docker_unprivileged_user."""
        from cli.main import app
        from core.host_config import HostConfig
        from core.hydration import InstanceConfig

        home = mock_sandbox_ai_home
        project_dir = "/home/dev/tomlproject"

        mock_project_config = HostConfig.model_validate(
            {"host": {"docker_unprivileged_user": "sandbox", "machinectl_authentication": "sudo"}}
        )
        mock_config = InstanceConfig.model_validate(
            {"instance": {"name": "tomlproject", "user_project_root": project_dir, "host_uid": "1000"}}
        )

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main.HostConfig.from_toml", return_value=mock_project_config),
            patch("cli.main.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "ok\n", "")),
            patch("cli.main._detect_git_config", return_value=("", "")),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main.create_instance_dirs"),
            patch("cli.main.write_sandbox_toml"),
            patch("cli.main._load_config", return_value=mock_config),
            patch("cli.main.create_env_file"),
            patch("cli.main.apply_default_acls"),
            patch("cli.main.prompt_secrets"),
            patch("cli.main.write_initialized_sentinel"),
        ):
            result = runner.invoke(app, ["init"])
            assert result.exit_code == 0

    def test_init_non_tty_without_config_fails_with_guidance(
        self,
        runner: CliRunner,
        mock_sandbox_ai_home: Path,
        isolated_sandbox_ai_user_home: Path,
    ) -> None:
        """init in non-TTY mode without canonical host config exits with guidance."""
        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(mock_sandbox_ai_home)),
            patch("cli.main._resolve_project_dir", return_value="/home/dev/noconfig"),
            # Disable the autouse no-op so the real seeder runs.
            patch("cli.main._seed_host_config_if_absent", wraps=_REAL_SEED_HOST_CONFIG),
            patch("cli.main._stdin_is_tty", return_value=False),
        ):
            result = runner.invoke(app, ["init"])
        assert result.exit_code == 1, result.output
        assert "non-interactive" in result.output.lower()
        assert "sandbox-ai.toml" in result.output

    def test_init_tty_seeds_host_config(
        self,
        runner: CliRunner,
        mock_sandbox_ai_home: Path,
        isolated_sandbox_ai_user_home: Path,
    ) -> None:
        """init in TTY mode prompts and writes <home>/config/sandbox-ai.toml when absent."""
        from cli.main import app
        from core.hydration import InstanceConfig

        project_dir = "/home/dev/seedproj"
        mock_config = InstanceConfig.model_validate(
            {"instance": {"name": "seedproj", "user_project_root": project_dir, "host_uid": "1000"}}
        )

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(mock_sandbox_ai_home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            # Run the real seeder
            patch(
                "cli.main._seed_host_config_if_absent",
                wraps=_REAL_SEED_HOST_CONFIG,
            ),
            patch("cli.main._stdin_is_tty", return_value=True),
            patch("cli.main.typer.prompt", side_effect=["sandbox-user", "sudo"]),
            patch("cli.main.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "ok\n", "")),
            patch("cli.main._detect_git_config", return_value=("", "")),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main.create_instance_dirs"),
            patch("cli.main.write_sandbox_toml"),
            patch("cli.main._load_config", return_value=mock_config),
            patch("cli.main.create_env_file"),
            patch("cli.main.apply_default_acls"),
            patch("cli.main.prompt_secrets"),
            patch("cli.main.write_initialized_sentinel"),
        ):
            result = runner.invoke(app, ["init"])
        seeded = isolated_sandbox_ai_user_home / "config" / "sandbox-ai.toml"
        assert seeded.exists(), f"output={result.output!r} exit={result.exit_code}"
        body = seeded.read_text()
        assert 'docker_unprivileged_user = "sandbox-user"' in body
        assert 'machinectl_authentication = "sudo"' in body
        assert result.exit_code == 0, result.output

    def test_init_tty_rejects_empty_user(
        self,
        runner: CliRunner,
        mock_sandbox_ai_home: Path,
        isolated_sandbox_ai_user_home: Path,
    ) -> None:
        """Empty docker_unprivileged_user is re-prompted until non-empty."""
        from cli.main import app
        from core.hydration import InstanceConfig

        project_dir = "/home/dev/empty"
        mock_config = InstanceConfig.model_validate(
            {"instance": {"name": "empty", "user_project_root": project_dir, "host_uid": "1000"}}
        )

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(mock_sandbox_ai_home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch(
                "cli.main._seed_host_config_if_absent",
                wraps=_REAL_SEED_HOST_CONFIG,
            ),
            patch("cli.main._stdin_is_tty", return_value=True),
            # First prompt returns empty → re-prompt; second is non-empty user; third is auth.
            patch("cli.main.typer.prompt", side_effect=["", "sandbox", "polkit"]),
            patch("cli.main.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "ok\n", "")),
            patch("cli.main._detect_git_config", return_value=("", "")),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main.create_instance_dirs"),
            patch("cli.main.write_sandbox_toml"),
            patch("cli.main._load_config", return_value=mock_config),
            patch("cli.main.create_env_file"),
            patch("cli.main.apply_default_acls"),
            patch("cli.main.prompt_secrets"),
            patch("cli.main.write_initialized_sentinel"),
        ):
            result = runner.invoke(app, ["init"])
        assert result.exit_code == 0, result.output
        seeded = isolated_sandbox_ai_user_home / "config" / "sandbox-ai.toml"
        assert 'docker_unprivileged_user = "sandbox"' in seeded.read_text()

    def test_init_tty_rejects_invalid_auth_value(
        self,
        runner: CliRunner,
        mock_sandbox_ai_home: Path,
        isolated_sandbox_ai_user_home: Path,
    ) -> None:
        """Invalid machinectl_authentication value is rejected."""
        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(mock_sandbox_ai_home)),
            patch("cli.main._resolve_project_dir", return_value="/home/dev/badauth"),
            patch(
                "cli.main._seed_host_config_if_absent",
                wraps=_REAL_SEED_HOST_CONFIG,
            ),
            patch("cli.main._stdin_is_tty", return_value=True),
            patch("cli.main.typer.prompt", side_effect=["sandbox", "weird"]),
        ):
            result = runner.invoke(app, ["init"])
        assert result.exit_code == 1
        assert "invalid machinectl_authentication" in result.output.lower()

    def test_init_existing_host_config_not_overwritten(
        self,
        runner: CliRunner,
        mock_sandbox_ai_home: Path,
        isolated_sandbox_ai_user_home: Path,
    ) -> None:
        """When the canonical host config exists, init does not prompt and does not overwrite."""
        from cli.main import app
        from core.hydration import InstanceConfig
        from core.scaffold import ensure_per_user_tree

        ensure_per_user_tree(isolated_sandbox_ai_user_home)
        existing_body = '[host]\ndocker_unprivileged_user = "preserved"\nmachinectl_authentication = "polkit"\n'
        cfg_path = isolated_sandbox_ai_user_home / "config" / "sandbox-ai.toml"
        cfg_path.write_text(existing_body)

        project_dir = "/home/dev/preserve"
        mock_config = InstanceConfig.model_validate(
            {"instance": {"name": "preserve", "user_project_root": project_dir, "host_uid": "1000"}}
        )

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(mock_sandbox_ai_home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch(
                "cli.main._seed_host_config_if_absent",
                wraps=_REAL_SEED_HOST_CONFIG,
            ),
            patch("cli.main.typer.prompt") as mock_prompt,
            patch("cli.main.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "ok\n", "")),
            patch("cli.main._detect_git_config", return_value=("", "")),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main.create_instance_dirs"),
            patch("cli.main.write_sandbox_toml"),
            patch("cli.main._load_config", return_value=mock_config),
            patch("cli.main.create_env_file"),
            patch("cli.main.apply_default_acls"),
            patch("cli.main.prompt_secrets"),
            patch("cli.main.write_initialized_sentinel"),
        ):
            result = runner.invoke(app, ["init"])
        assert result.exit_code == 0, result.output
        # No prompts issued because file existed
        mock_prompt.assert_not_called()
        # File untouched
        assert cfg_path.read_text() == existing_body


class TestInitAuthProbe:
    """Task 3.10: init-time auth mode probe tests."""

    def test_probe_success_sudo(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """Probe succeeds with sudo mode — init proceeds."""
        from cli.main import app
        from core.hydration import InstanceConfig

        home = mock_sandbox_ai_home
        project_dir = "/home/dev/probeproject"
        mock_config = InstanceConfig.model_validate(
            {"instance": {"name": "probeproject", "user_project_root": project_dir, "host_uid": "1000"}}
        )

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "ok\n", "")) as mock_run,
            patch("cli.main._detect_git_config", return_value=("", "")),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main.create_instance_dirs"),
            patch("cli.main.write_sandbox_toml"),
            patch("cli.main._load_config", return_value=mock_config),
            patch("cli.main.create_env_file"),
            patch("cli.main.apply_default_acls"),
            patch("cli.main.prompt_secrets"),
            patch("cli.main.write_initialized_sentinel"),
        ):
            result = runner.invoke(app, ["init"])
            assert result.exit_code == 0
            # Verify the probe was called with sudo prefix
            probe_call = mock_run.call_args[0][0]
            assert "sudo" in probe_call
            assert "machinectl" in probe_call

    def test_probe_failure_exits_with_remediation(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """Probe failure exits with error and remediation guidance."""
        from cli.main import app

        home = mock_sandbox_ai_home

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value="/home/dev/failprobe"),
            patch(
                "cli.main.subprocess.run",
                return_value=subprocess.CompletedProcess([], 1, "", "permission denied"),
            ),
        ):
            result = runner.invoke(app, ["init"])
            assert result.exit_code == 1
            assert "probe failed" in result.output.lower()
            assert "remediation" in result.output.lower()

    def test_probe_timeout_exits_with_error(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """Probe timeout exits with error."""
        from cli.main import app

        home = mock_sandbox_ai_home

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value="/home/dev/timeout"),
            patch("cli.main.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="test", timeout=5)),
        ):
            result = runner.invoke(app, ["init"])
            assert result.exit_code == 1
            assert "timed out" in result.output.lower()

    def test_probe_polkit_mode_no_sudo(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """Polkit mode probe does not include sudo in command."""
        from cli.main import app
        from core.hydration import InstanceConfig

        home = mock_sandbox_ai_home
        project_dir = "/home/dev/polkit"
        mock_config = InstanceConfig.model_validate(
            {"instance": {"name": "polkit", "user_project_root": project_dir, "host_uid": "1000"}}
        )

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "ok\n", "")) as mock_run,
            patch("cli.main._detect_git_config", return_value=("", "")),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main.create_instance_dirs"),
            patch("cli.main.write_sandbox_toml"),
            patch("cli.main._load_config", return_value=mock_config),
            patch("cli.main.create_env_file"),
            patch("cli.main.apply_default_acls"),
            patch("cli.main.prompt_secrets"),
            patch("cli.main.write_initialized_sentinel"),
        ):
            result = runner.invoke(app, ["init", "--machinectl-auth", "polkit"])
            assert result.exit_code == 0
            probe_call = mock_run.call_args[0][0]
            assert "sudo" not in probe_call
            assert "machinectl" in probe_call

    def test_probe_polkit_failure_shows_polkit_remediation(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """Polkit probe failure shows polkit-specific remediation."""
        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(mock_sandbox_ai_home)),
            patch("cli.main._resolve_project_dir", return_value="/home/dev/polkitfail"),
            patch(
                "cli.main.subprocess.run",
                return_value=subprocess.CompletedProcess([], 1, "", "auth failed"),
            ),
        ):
            result = runner.invoke(app, ["init", "--machinectl-auth", "polkit"])
            assert result.exit_code == 1
            assert "polkit" in result.output.lower()

    def test_invalid_machinectl_auth_value(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """Invalid --machinectl-auth value exits with error."""
        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(mock_sandbox_ai_home)),
            patch("cli.main._resolve_project_dir", return_value="/home/dev/badauth"),
        ):
            result = runner.invoke(app, ["init", "--machinectl-auth", "invalid"])
            assert result.exit_code == 1
            assert "invalid" in result.output.lower()

    def test_probe_file_not_found_exits_with_error(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """Probe FileNotFoundError (command not on PATH) exits with error."""
        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(mock_sandbox_ai_home)),
            patch("cli.main._resolve_project_dir", return_value="/home/dev/nobin"),
            patch("cli.main.subprocess.run", side_effect=FileNotFoundError),
        ):
            result = runner.invoke(app, ["init"])
            assert result.exit_code == 1
            assert "command not found" in result.output.lower()


class TestResolveHostConfig:
    """Coverage: _resolve_host_config with HostConfig present."""

    def test_resolve_host_config_from_project_config(self) -> None:
        """_resolve_host_config returns values from HostConfig when present."""
        from cli.main import _resolve_host_config
        from core.host_config import HostConfig, MachinectlAuth
        from core.hydration import InstanceConfig

        mock_project_config = HostConfig.model_validate(
            {"host": {"docker_unprivileged_user": "fromtoml", "machinectl_authentication": "polkit"}}
        )
        mock_config = InstanceConfig.model_validate(
            {"instance": {"name": "test", "user_project_root": "/tmp/test", "host_uid": "1000"}}
        )

        with patch("cli.main.HostConfig.from_toml", return_value=mock_project_config):
            user, auth = _resolve_host_config("/tmp/test", mock_config)
            assert user == "fromtoml"
            assert auth == MachinectlAuth.POLKIT


class TestDryRunExistingInstance:
    """Task 12.1: --dry-run with existing instance."""

    def test_dry_run_skips_warm_check(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """Warm state check is skipped when --dry-run is set."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam("myproject-abc123", 0)

        # Create tooling plane files for template validation
        _create_tooling_plane(home)

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._warm_check") as mock_warm,
        ):
            result = runner.invoke(app, ["start", "--dry-run"])
            mock_warm.assert_not_called()
            assert result.exit_code == 0

    def test_dry_run_existing_instance_exit_0(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """Existing instance dry-run passes with exit code 0."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam("myproject-abc123", 0)
        _create_tooling_plane(home)

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
        ):
            result = runner.invoke(app, ["start", "--dry-run"])
            assert result.exit_code == 0

    def test_dry_run_shows_ipam_preview(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """IPAM slot is previewed with 'subject to concurrent changes' note."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam("myproject-abc123", 5)
        _create_tooling_plane(home)

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
        ):
            result = runner.invoke(app, ["start", "--dry-run"])
            assert result.exit_code == 0
            # Should mention the IPAM slot
            assert "5" in result.output or "slot" in result.output.lower()

    def test_dry_run_shows_compose_command(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """Compose command is displayed in dry-run output."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam("myproject-abc123", 0)
        _create_tooling_plane(home)

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
        ):
            result = runner.invoke(app, ["start", "--dry-run"])
            assert "docker compose" in result.output.lower() or "compose" in result.output.lower()

    def test_dry_run_template_error_exits_1(
        self, runner: CliRunner, mock_sandbox_ai_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Template error causes exit code 1."""
        import core.hydration as hydration
        import jinja2

        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam("myproject-abc123", 0)
        _create_tooling_plane(home)
        # Build a broken templates root and redirect the packaged loader to it
        templates_root = home / "broken_templates"
        (templates_root / "docker").mkdir(parents=True)
        (templates_root / "docker" / "compose.yml").write_text("{{ undefined_var }}")
        monkeypatch.setattr(hydration, "_resource_files", lambda _name: templates_root)
        monkeypatch.setattr(
            jinja2,
            "PackageLoader",
            lambda *_a, **_k: jinja2.FileSystemLoader(str(templates_root)),
        )

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
        ):
            result = runner.invoke(app, ["start", "--dry-run"])
            assert result.exit_code == 1

    def test_dry_run_missing_env_keys_reported(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """Missing .sandbox.env keys are reported in dry-run."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        inst = _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam("myproject-abc123", 0)
        _create_tooling_plane(home)
        # Write empty env file — secrets missing
        (inst / ".sandbox.env").write_text('CORE_ANTHROPIC_API_KEY=""\nCORE_GITHUB_TOKEN=""')

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
        ):
            result = runner.invoke(app, ["start", "--dry-run"])
            # Should mention missing secrets
            out = result.output.lower()
            assert "missing" in out or "secret" in out or "empty" in out


class TestDryRunNewInstance:
    """Task 5.1: --dry-run with no existing instance → error with guidance."""

    def test_dry_run_no_instance_errors_with_guidance(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """No-instance dry-run errors with init guidance message."""
        home = mock_sandbox_ai_home

        from cli.main import app

        _seed_registry(_user_home())
        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value="/home/dev/newproject"),
        ):
            result = runner.invoke(app, ["start", "--dry-run"])
            assert result.exit_code == 1
            assert "sandbox init" in result.output.lower()


def _create_tooling_plane(home: Path) -> None:
    """Create minimal tooling plane files needed for dry-run template validation."""
    # Jinja2 templates
    docker_dir = home / ".docker"
    (docker_dir / "compose.yml").write_text("# compose for {{ instance_name }}\nversion: '3'\n")
    (docker_dir / "core" / "entrypoint.sh").write_text("#!/bin/bash\n")
    (docker_dir / "core" / "Dockerfile.core.wolfi").write_text("FROM {{ core_base_image }}\n")
    (docker_dir / "admin" / "entrypoint.sh").write_text("#!/bin/bash\n")
    (docker_dir / "admin" / "Dockerfile.admin.debian").write_text("FROM {{ admin_base_image }}\n")
    (docker_dir / "extras").mkdir(parents=True, exist_ok=True)
    (docker_dir / "extras" / "db-postgres.yml").write_text("# postgres\n")
    (docker_dir / "coredns").mkdir(parents=True, exist_ok=True)
    (docker_dir / "coredns" / "Dockerfile.coredns").write_text("FROM busybox\n")

    config_dir = home / ".config"
    (config_dir / "coredns").mkdir(parents=True, exist_ok=True)
    (config_dir / "coredns" / "Corefile").write_text("# Corefile for {{ instance_name }}\n")
    (config_dir / "dnsdist").mkdir(parents=True, exist_ok=True)
    (config_dir / "dnsdist" / "dnsdist.conf").write_text(
        'setLocal("0.0.0.0:53")\nnewServer({address="{{ coredns_dns_ip }}:53"})\n'
    )
    (config_dir / "proxy" / "squid.conf").write_text("# squid for {{ proxy_core_ip }}\n")
    (config_dir / "proxy" / "ERR_SANDBOX_403").write_text("403 Forbidden\n")
    (config_dir / "admin").mkdir(parents=True, exist_ok=True)
    (config_dir / "admin" / ".zshrc").write_text("# zshrc\n")
    (config_dir / "admin" / ".tmux.conf").write_text("# tmux\n")
    (config_dir / "admin" / "gitmux.conf").write_text("# gitmux\n")
    (config_dir / "admin" / "starship.toml").write_text("# starship\n")
    (config_dir / "admin" / ".gitconfig").write_text("# gitconfig\n")
    (config_dir / "core").mkdir(parents=True, exist_ok=True)
    (config_dir / "core" / ".bashrc").write_text("# bashrc\n")
    (config_dir / "core" / ".npmrc").write_text("# npmrc\n")
    (config_dir / "core" / ".gitconfig").write_text("# gitconfig\n")
    (config_dir / "core" / "CLAUDE.md").write_text("# Claude\n")
    (config_dir / "core" / "sshd_config").write_text("# sshd {{ core_ipc_ip }}\n")


class TestDryRunIpamExhausted:
    """Cover IPAM exhaustion path in dry-run pipeline."""

    def test_dry_run_ipam_exhausted_exits_1(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """IPAM exhaustion during dry-run exits with code 1."""
        from core.ipam import IPAMExhaustedError

        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")
        _create_tooling_plane(home)

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch.object(
                __import__("core.ipam", fromlist=["IPAMLedger"]).IPAMLedger,
                "peek_next_slot",
                side_effect=IPAMExhaustedError("All slots consumed"),
            ),
        ):
            result = runner.invoke(app, ["start", "--dry-run"])
            assert result.exit_code == 1


class TestCheckSecretsFirecrawl:
    """Cover firecrawl secret branch in _check_secrets."""

    def test_check_secrets_firecrawl_missing(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """Firecrawl secret reported when mcp_firecrawl=true."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        inst = _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam("myproject-abc123", 0)
        _create_tooling_plane(home)

        # Write a sandbox.toml with mcp_firecrawl=true
        firecrawl_toml = (inst / "sandbox.toml").read_bytes().decode()
        firecrawl_toml = firecrawl_toml.replace("mcp_firecrawl = false", "mcp_firecrawl = true")
        (inst / "sandbox.toml").write_text(firecrawl_toml)

        # Also need mcp-firecrawl.yml in tooling plane
        extras = home / ".docker" / "extras"
        (extras / "mcp-firecrawl.yml").write_text("# firecrawl\n")
        (extras / "Dockerfile.mcp-firecrawl").write_text("FROM node\n")

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
        ):
            result = runner.invoke(app, ["start", "--dry-run"])
            out = result.output.lower()
            assert "firecrawl" in out or "missing" in out or "secret" in out


# ── Container Status Function ────────────────────────────────────────────────


class TestContainerStatus:
    """Task 6.1: _container_status NDJSON parsing."""

    def test_parses_ndjson_output(self, tmp_path: Path) -> None:
        """Multiple NDJSON lines are parsed into ContainerInfo list."""
        import subprocess as sp

        from cli.main import ContainerInfo, _container_status
        from core.hydration import InstanceConfig

        config = InstanceConfig.model_validate(
            {
                "instance": {
                    "name": "t",
                    "user_project_root": "/x",
                    "host_uid": "1000",
                },
            }
        )

        ndjson = (
            '{"Name":"t-core-1","Service":"core","State":"running","Health":"healthy","Status":"Up 5s"}\n'
            '{"Name":"t-admin-1","Service":"admin","State":"running","Health":"","Status":"Up 5s"}\n'
        )

        mock_result = sp.CompletedProcess(args=[], returncode=0, stdout=ndjson, stderr="")
        compose = tmp_path / "docker" / "compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("version: '3'")

        with patch("cli.main.Executor") as MockExec:
            MockExec.return_value.run.return_value = mock_result
            containers = _container_status(str(tmp_path), "t", "s", config)

        assert len(containers) == 2
        assert containers[0].name == "t-core-1"
        assert containers[0].service == "core"
        assert containers[0].state == "running"
        assert containers[0].health == "healthy"
        assert isinstance(containers[1], ContainerInfo)

    def test_empty_for_stopped_instance(self, tmp_path: Path) -> None:
        """Stopped instance returns empty container list."""
        import subprocess as sp

        from cli.main import _container_status
        from core.hydration import InstanceConfig

        config = InstanceConfig.model_validate(
            {
                "instance": {
                    "name": "t",
                    "user_project_root": "/x",
                    "host_uid": "1000",
                },
            }
        )

        mock_result = sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        compose = tmp_path / "docker" / "compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("version: '3'")

        with patch("cli.main.Executor") as MockExec:
            MockExec.return_value.run.return_value = mock_result
            containers = _container_status(str(tmp_path), "t", "s", config)

        assert containers == []

    def test_executor_error_returns_empty(self, tmp_path: Path) -> None:
        """Executor error returns empty list instead of raising."""
        from cli.main import _container_status
        from core.exceptions import SandboxExecutionError
        from core.hydration import InstanceConfig

        config = InstanceConfig.model_validate(
            {
                "instance": {
                    "name": "t",
                    "user_project_root": "/x",
                    "host_uid": "1000",
                },
            }
        )

        compose = tmp_path / "docker" / "compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("version: '3'")

        with patch("cli.main.Executor") as MockExec:
            MockExec.return_value.run.side_effect = SandboxExecutionError("fail")
            containers = _container_status(str(tmp_path), "t", "s", config)

        assert containers == []


# ── Status Command ───────────────────────────────────────────────────────────


class TestStatusNoInstance:
    """Task 7.1: sandbox status — no instance error."""

    def test_status_no_instance_exits_1(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(mock_sandbox_ai_home)),
            patch("cli.main._resolve_project_dir", return_value="/nonexistent"),
        ):
            result = runner.invoke(app, ["status"])
            assert result.exit_code == 1
            assert "no sandbox" in result.output.lower()


class TestStatusRunning:
    """Task 7.1: sandbox status — running instance."""

    def test_status_running_shows_state(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam("myproject-abc123", 0)

        from cli.main import ContainerInfo, app

        containers = [
            ContainerInfo(name="t-core-1", service="core", state="running", health="healthy", status="Up 5s"),
            ContainerInfo(name="t-admin-1", service="admin", state="running", health="healthy", status="Up 5s"),
        ]

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._container_status", return_value=containers),
        ):
            result = runner.invoke(app, ["status"])
            assert result.exit_code == 0
            out = result.output.lower()
            assert "running" in out
            assert "core" in out
            # Task 7.5a: container table must include IP addresses
            # For slot 0: core → agent_isolated_ip = 10.100.0.3
            assert "10.100.0.3" in result.output
            assert "network" in out


class TestStatusStopped:
    """Task 7.1: sandbox status — stopped instance."""

    def test_status_stopped_shows_state(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam("myproject-abc123", 0)

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._container_status", return_value=[]),
        ):
            result = runner.invoke(app, ["status"])
            assert result.exit_code == 0
            out = result.output.lower()
            assert "stopped" in out


class TestStatusDegraded:
    """Task 7.1: sandbox status — degraded state."""

    def test_status_degraded_shows_warning(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam("myproject-abc123", 0)

        from cli.main import ContainerInfo, app

        containers = [
            ContainerInfo(name="t-core-1", service="core", state="running", health="healthy", status="Up"),
            ContainerInfo(name="t-admin-1", service="admin", state="running", health="unhealthy", status="Up"),
        ]

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._container_status", return_value=containers),
        ):
            result = runner.invoke(app, ["status"])
            assert result.exit_code == 0
            out = result.output.lower()
            assert "degraded" in out


class TestStatusIPAM:
    """Task 7.1: sandbox status — IPAM display."""

    def test_status_shows_ipam_subnets(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam("myproject-abc123", 3)

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._container_status", return_value=[]),
        ):
            result = runner.invoke(app, ["status"])
            assert result.exit_code == 0
            out = result.output
            # Should display IPAM slot and subnets
            assert "3" in out or "slot" in out.lower()
            assert "10." in out


class TestStatusConfigWarnings:
    """Task 9.2: status output shows ⊘ and missing secret names."""

    def test_status_warns_on_missing_secrets(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam("myproject-abc123", 0)

        # Write env file with empty PG_PASSWORD
        inst_dir = home / "sandboxes" / "myproject-abc123"
        env_path = inst_dir / ".sandbox.env"
        env_path.write_text("CORE_ANTHROPIC_API_KEY=sk-123\nPG_PASSWORD=\n")

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._container_status", return_value=[]),
        ):
            result = runner.invoke(app, ["status"])
            assert result.exit_code == 0
            assert "⊘" in result.output
            assert "PG_PASSWORD" in result.output


# ── ACL Plan Function tests ──────────────────────────────────────────────────


class TestComputeAncestors:
    """Task 2.4: _compute_ancestors — ownership boundary, root exclusion."""

    def test_ownership_boundary_stops_walk(self, tmp_path: Path) -> None:
        """Walk stops at the first dir not owned by current UID."""
        from cli.main import _compute_ancestors

        # tmp_path is owned by current user; its parents (e.g., /tmp) are root-owned
        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)

        result = _compute_ancestors(str(instance_dir))
        # Should include tmp_path and tmp_path/sandboxes (both user-owned)
        # but NOT /tmp or /
        assert str(tmp_path) in result
        assert str(tmp_path / "sandboxes") in result
        assert "/" not in result

    def test_root_excluded(self, tmp_path: Path) -> None:
        """Root directory is never included."""
        from cli.main import _compute_ancestors

        instance_dir = tmp_path / "a"
        instance_dir.mkdir()

        result = _compute_ancestors(str(instance_dir))
        assert "/" not in result

    def test_single_level_path(self, tmp_path: Path) -> None:
        """Instance dir directly under tmp_path — one ancestor."""
        from cli.main import _compute_ancestors

        instance_dir = tmp_path / "inst"
        instance_dir.mkdir()

        result = _compute_ancestors(str(instance_dir))
        assert str(tmp_path) in result

    def test_pathological_root_instance_dir(self) -> None:
        """Pathological case: instance_dir is / — walk produces empty list."""
        from cli.main import _compute_ancestors

        result = _compute_ancestors("/")
        assert result == []

    def test_top_down_order(self, tmp_path: Path) -> None:
        """Ancestors returned in shallowest-first order."""
        from cli.main import _compute_ancestors

        instance_dir = tmp_path / "a" / "b" / "c"
        instance_dir.mkdir(parents=True)

        result = _compute_ancestors(str(instance_dir))
        # Shallowest first
        if len(result) >= 2:
            for i in range(len(result) - 1):
                assert len(result[i]) <= len(result[i + 1])


class TestACLPlanAsymmetry:
    """Task 2.5: grant/revoke plan asymmetry — ancestors in grant, absent from revoke."""

    def test_grant_plan_includes_ancestors(self, tmp_path: Path) -> None:
        """Grant plan includes ancestor --x entries."""
        from cli.main import _acl_grant_plan

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)
        (instance_dir / "docker").mkdir()
        (instance_dir / "config").mkdir()
        (instance_dir / ".sandbox.env").write_text("")

        plan = _acl_grant_plan(str(instance_dir), "sandbox")
        descriptions = [desc for _, desc in plan]
        assert any("ancestor traverse" in d for d in descriptions)

    def test_revoke_plan_excludes_ancestors(self, tmp_path: Path) -> None:
        """Revoke plan does NOT include ancestor entries."""
        from cli.main import _acl_revoke_plan

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)

        plan = _acl_revoke_plan(str(instance_dir), "sandbox")
        descriptions = [desc for _, desc in plan]
        assert not any("ancestor" in d for d in descriptions)

    def test_both_plans_include_env_file(self, tmp_path: Path) -> None:
        """Both plans include .sandbox.env."""
        from cli.main import _acl_grant_plan, _acl_revoke_plan

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)
        (instance_dir / "docker").mkdir()
        (instance_dir / "config").mkdir()
        (instance_dir / ".sandbox.env").write_text("")

        grant = _acl_grant_plan(str(instance_dir), "sandbox")
        revoke = _acl_revoke_plan(str(instance_dir), "sandbox")

        grant_descs = [d for _, d in grant]
        revoke_descs = [d for _, d in revoke]
        assert any("env file" in d for d in grant_descs)
        assert any("env file" in d for d in revoke_descs)

    def test_grant_plan_includes_instance_root(self, tmp_path: Path) -> None:
        """Grant plan includes instance root with r-x."""
        from cli.main import _acl_grant_plan

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)
        (instance_dir / "docker").mkdir()
        (instance_dir / "config").mkdir()
        (instance_dir / ".sandbox.env").write_text("")

        plan = _acl_grant_plan(str(instance_dir), "sandbox")
        descriptions = [d for _, d in plan]
        assert any("instance root" in d for d in descriptions)
        # Verify r-x permission on instance root
        root_entries = [(args, d) for args, d in plan if "instance root" in d]
        assert any("r-x" in " ".join(args) for args, _ in root_entries)

    def test_grant_plan_excludes_cache_log_option_b(self, tmp_path: Path) -> None:
        """Post-acl-ownership-recipes: cache/log Option-B grants are absent from grant plan."""
        from cli.main import _acl_grant_plan

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)
        (instance_dir / "docker").mkdir()
        (instance_dir / "config").mkdir()
        (instance_dir / ".sandbox.env").write_text("")

        plan = _acl_grant_plan(str(instance_dir), "sandbox")
        for args, desc in plan:
            for cache_log in ["cache/core/.claude", "cache/admin/tmux_resurrect", "log/core", "log/admin"]:
                assert cache_log not in desc
                assert not any(cache_log in arg for arg in args)
            assert "rwX" not in " ".join(args), f"rwX Option-B grant survived: {desc}"

    def test_grant_plan_includes_workspace_named_acl(self, tmp_path: Path) -> None:
        """Workspace named-ACL is granted when user_project_root is supplied."""
        from cli.main import _acl_grant_plan

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)
        ws = tmp_path / "myws"
        ws.mkdir()

        plan = _acl_grant_plan(str(instance_dir), "sandbox", str(ws), dev_user="dev")
        descriptions = [d for _, d in plan]
        assert any("workspace named-ACL" in d for d in descriptions)
        assert any("workspace default ACL" in d for d in descriptions)
        # Effective entry contains rwx
        ws_eff = next((a, d) for a, d in plan if "workspace named-ACL" in d)
        assert "u:sandbox:rwx" in " ".join(ws_eff[0])
        # Default entry includes both host_user and dev_user
        ws_def = next((a, d) for a, d in plan if "workspace default ACL" in d)
        joined = " ".join(ws_def[0])
        assert "u:sandbox:rwx" in joined
        assert "u:dev:rwx" in joined

    def test_revoke_plan_excludes_cache_log(self, tmp_path: Path) -> None:
        """Post-acl-ownership-recipes: cache/log entries are absent from revoke plan."""
        from cli.main import _acl_revoke_plan

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)

        plan = _acl_revoke_plan(str(instance_dir), "sandbox")
        for args, desc in plan:
            for cache_log in ["cache/core/.claude", "cache/admin/tmux_resurrect", "log/core", "log/admin"]:
                assert cache_log not in desc
                assert not any(cache_log in arg for arg in args)

    def test_revoke_plan_includes_workspace_named_acl(self, tmp_path: Path) -> None:
        """Workspace named-ACL effective and default-entry revocations."""
        from cli.main import _acl_revoke_plan

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)
        ws = tmp_path / "myws"

        plan = _acl_revoke_plan(str(instance_dir), "sandbox", str(ws))
        descriptions = [d for _, d in plan]
        assert any("workspace named-ACL" in d for d in descriptions)
        assert any("workspace default named entry" in d for d in descriptions)
        # The default revocation uses -d -x
        ws_def = next((a, d) for a, d in plan if "workspace default named entry" in d)
        assert "-d" in ws_def[0]
        assert "-x" in ws_def[0]

    def test_revoke_plan_omits_workspace_when_user_project_root_none(self, tmp_path: Path) -> None:
        """No workspace entries when user_project_root is None (back-compat call shape)."""
        from cli.main import _acl_revoke_plan

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)

        plan = _acl_revoke_plan(str(instance_dir), "sandbox")
        descriptions = [d for _, d in plan]
        # Match description-prefix tokens, not substrings — tmp_path may contain "workspace".
        assert not any(d.startswith("workspace ") for d in descriptions)

    def test_grant_plan_handles_unstattable_workspace_ancestor(self, tmp_path: Path) -> None:
        """_compute_workspace_ancestors stops cleanly at a non-stat-able parent."""
        from cli.main import _acl_grant_plan

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)

        # Workspace under a nonexistent path → os.stat on the parent raises OSError.
        plan = _acl_grant_plan(
            str(instance_dir), "sandbox", "/nonexistent-root/myproj", dev_user="dev"
        )
        # Should not have crashed; the workspace named-ACL is still queued.
        assert any("workspace named-ACL" in d for _, d in plan)

    def test_grant_plan_includes_secrets_traverse(self, tmp_path: Path) -> None:
        """Grant plan retains a dir-level traverse on secrets/."""
        from cli.main import _acl_grant_plan

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)
        (instance_dir / "docker").mkdir()
        (instance_dir / "config").mkdir()
        (instance_dir / ".sandbox.env").write_text("")

        plan = _acl_grant_plan(str(instance_dir), "sandbox")
        descriptions = [d for _, d in plan]
        assert any("secrets dir traverse" in d for d in descriptions)

class TestDryRunHelperMkdirPlanFallback:
    """Dry-run preview reports gracefully when subuid resolver fails."""

    def test_dry_run_handles_no_subuid_range(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        from cli.main import app
        from core.host_config import NoSubuidRangeError

        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam("myproject-abc123", 0)
        _create_tooling_plane(home)

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch(
                "cli.main.host_id_for_in_container",
                side_effect=NoSubuidRangeError("no entry for 'sandbox'"),
            ),
        ):
            result = runner.invoke(app, ["start", "--dry-run"])
            # Dry-run does not crash; reports the unavailability.
            assert "helper-mkdir plan unavailable" in result.output


class TestHelperMkdirChownPlan:
    """Section 7: cache/log helper-mkdir+chown plan + phase."""

    def test_plan_groups_leaves_by_parent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cli.main import _helper_mkdir_chown_plan

        monkeypatch.setattr("cli.main.host_id_for_in_container", lambda n, u: 100999)
        monkeypatch.setattr("cli.main.host_gid_for_in_container", lambda n, u: 200999)
        plan = _helper_mkdir_chown_plan("/inst", "claude-sandbox")
        assert plan == [
            ("/inst/cache/core", (".claude",), 100999, 200999),
            ("/inst/cache/admin", ("tmux_resurrect",), 100999, 200999),
            ("/inst/log", ("core", "admin"), 100999, 200999),
        ]

    def test_phase_sets_default_acl_then_invokes_helper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cli.main import _phase_helper_mkdir_chown_cache_log
        from core.host_config import MachinectlAuth

        monkeypatch.setattr("cli.main.host_id_for_in_container", lambda n, u: 100999)
        monkeypatch.setattr("cli.main.host_gid_for_in_container", lambda n, u: 200999)

        events: list[tuple[str, list[str]]] = []

        def _fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            events.append(("setfacl", list(cmd)))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        def _fake_helper(*a: object, **kw: object) -> None:
            events.append(("helper", []))

        monkeypatch.setattr("cli.main.subprocess.run", _fake_run)
        monkeypatch.setattr("cli.main.helper_mkdir_chown_dirs", _fake_helper)

        _phase_helper_mkdir_chown_cache_log("/inst", "claude-sandbox", MachinectlAuth.SUDO, "dev")

        # Three setfacl + three helper invocations, alternating
        assert [e[0] for e in events] == ["setfacl", "helper", "setfacl", "helper", "setfacl", "helper"]
        # First setfacl applies parent default ACL with u:dev:rwx
        cmd = events[0][1]
        assert cmd[:3] == ["setfacl", "-d", "-m"]
        assert "u:dev:rwx" in cmd[3]
        assert cmd[4] == "/inst/cache/core"

    def test_phase_idempotent_re_invocation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cli.main import _phase_helper_mkdir_chown_cache_log
        from core.host_config import MachinectlAuth

        monkeypatch.setattr("cli.main.host_id_for_in_container", lambda n, u: 1)
        monkeypatch.setattr("cli.main.host_gid_for_in_container", lambda n, u: 2)
        calls = {"setfacl": 0, "helper": 0}

        def _fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            calls["setfacl"] += 1
            return subprocess.CompletedProcess(cmd, 0, "", "")

        def _fake_helper(*a: object, **kw: object) -> None:
            calls["helper"] += 1

        monkeypatch.setattr("cli.main.subprocess.run", _fake_run)
        monkeypatch.setattr("cli.main.helper_mkdir_chown_dirs", _fake_helper)

        for _ in range(2):
            _phase_helper_mkdir_chown_cache_log("/inst", "u", MachinectlAuth.SUDO, "dev")
        assert calls == {"setfacl": 6, "helper": 6}

    def test_phase_setfacl_failure_wrapped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cli.main import _phase_helper_mkdir_chown_cache_log
        from core.exceptions import SandboxExecutionError
        from core.host_config import MachinectlAuth

        monkeypatch.setattr("cli.main.host_id_for_in_container", lambda n, u: 1)
        monkeypatch.setattr("cli.main.host_gid_for_in_container", lambda n, u: 2)

        def _raise(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.CalledProcessError(1, cmd, stderr="permission denied")

        monkeypatch.setattr("cli.main.subprocess.run", _raise)
        monkeypatch.setattr("cli.main.helper_mkdir_chown_dirs", lambda *a, **k: None)

        with pytest.raises(SandboxExecutionError, match="permission denied"):
            _phase_helper_mkdir_chown_cache_log("/inst", "u", MachinectlAuth.SUDO, "dev")


class TestPhaseACLGrantErrorWrapping:
    """Task 3.4: CalledProcessError → SandboxExecutionError with target path context."""

    def test_setfacl_failure_wrapped_in_sandbox_error(self, tmp_path: Path) -> None:
        """WHEN setfacl fails, THEN SandboxExecutionError raised with description."""
        from cli.main import _phase_acl_grant
        from core.exceptions import SandboxExecutionError

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)
        (instance_dir / "docker").mkdir()
        (instance_dir / "config").mkdir()
        (instance_dir / ".sandbox.env").write_text("")

        with (
            patch(
                "subprocess.run",
                side_effect=subprocess.CalledProcessError(1, ["setfacl"], stderr="Operation not supported"),
            ),
            patch("cli.main._diagnose_traverse_failure", return_value=""),
            pytest.raises(SandboxExecutionError) as exc_info,
        ):
            _phase_acl_grant(str(instance_dir), "sandbox")

        error_text = str(exc_info.value)
        assert "ACL grant failed" in error_text

    def test_setfacl_failure_includes_stderr(self, tmp_path: Path) -> None:
        """WHEN setfacl fails with stderr, THEN error includes stderr content."""
        from cli.main import _phase_acl_grant
        from core.exceptions import SandboxExecutionError

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)
        (instance_dir / "docker").mkdir()
        (instance_dir / "config").mkdir()
        (instance_dir / ".sandbox.env").write_text("")

        with (
            patch(
                "subprocess.run",
                side_effect=subprocess.CalledProcessError(1, ["setfacl"], stderr="setfacl: /nonexistent: No such file"),
            ),
            patch("cli.main._diagnose_traverse_failure", return_value=""),
            pytest.raises(SandboxExecutionError) as exc_info,
        ):
            _phase_acl_grant(str(instance_dir), "sandbox")

        assert "No such file" in str(exc_info.value)


class TestDiagnoseTraverseFailure:
    """Task 3.5: _diagnose_traverse_failure — missing --x, fix command, no-failure path.

    All os.stat/pwd.getpwnam calls fully mocked to avoid host filesystem dependency.
    """

    def test_user_not_found(self) -> None:
        """WHEN host_user doesn't exist, THEN diagnostic reports it."""
        from cli.main import _diagnose_traverse_failure

        with patch("pwd.getpwnam", side_effect=KeyError("nonexistent")):
            result = _diagnose_traverse_failure("/some/path", "nonexistent_user_xyz")
            assert "does not exist" in result

    def test_no_failure_found_returns_empty(self) -> None:
        """WHEN all ancestors are traversable, THEN returns empty string."""
        from cli.main import _diagnose_traverse_failure

        fake_pw = MagicMock()
        fake_pw.pw_uid = 2000
        fake_pw.pw_gid = 2000

        traversable = MagicMock(spec=os.stat_result)
        traversable.st_uid = 0
        traversable.st_gid = 0
        traversable.st_mode = 0o755

        # /synthetic/project owned by target user — exercises L413 (user-owner exec)
        user_owned = MagicMock(spec=os.stat_result)
        user_owned.st_uid = 2000
        user_owned.st_gid = 2000
        user_owned.st_mode = 0o700

        def controlled_stat(path: str) -> MagicMock:
            if path == "/synthetic/project":
                return user_owned
            return traversable

        with (
            patch("pwd.getpwnam", return_value=fake_pw),
            patch("os.stat", side_effect=controlled_stat),
        ):
            result = _diagnose_traverse_failure("/synthetic/project", "sandbox")
            assert result == ""

    def test_missing_execute_reported_with_fix_command(self) -> None:
        """WHEN an ancestor lacks --x for the user, THEN fix command is reported."""
        from cli.main import _diagnose_traverse_failure

        fake_pw = MagicMock()
        fake_pw.pw_uid = 2000
        fake_pw.pw_gid = 2000

        # /synthetic: no other-exec (0o700)
        blocked = MagicMock(spec=os.stat_result)
        blocked.st_uid = 0
        blocked.st_gid = 0
        blocked.st_mode = 0o700

        traversable = MagicMock(spec=os.stat_result)
        traversable.st_uid = 0
        traversable.st_gid = 0
        traversable.st_mode = 0o755

        def controlled_stat(path: str) -> MagicMock:
            if path == "/synthetic":
                return blocked
            return traversable

        with (
            patch("pwd.getpwnam", return_value=fake_pw),
            patch("os.stat", side_effect=controlled_stat),
        ):
            result = _diagnose_traverse_failure("/synthetic/project/child", "sandbox")
            assert "lacks execute permission" in result
            assert "setfacl -m" in result
            assert "/synthetic" in result


class TestStartPipelineOrdering:
    """Task 2.1 RED: _phase_credential_ownership called after ACL grants, before compose up."""

    def test_ownership_after_acl_before_compose(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """Integration: phase ordering is ACL → ownership → compose."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)
        _write_ipam(instance_id, 0)

        from cli.main import app

        call_order: list[str] = []

        def track_acl(*a: object, **kw: object) -> None:
            call_order.append("acl_grant")

        def track_ownership(*a: object, **kw: object) -> None:
            call_order.append("credential_ownership")

        def track_compose(*a: object, **kw: object) -> None:
            call_order.append("compose_up")

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="pass"),
            patch("cli.main._phase_hydrate"),
            patch("cli.main._phase_acl_grant", side_effect=track_acl),
            patch("cli.main._phase_helper_mkdir_chown_cache_log"),
            patch("cli.main._phase_credential_ownership", side_effect=track_ownership),
            patch("cli.main._phase_compose_up", side_effect=track_compose),
            patch("cli.main._phase_handover"),
            patch("cli.main._release_lock"),
        ):
            result = runner.invoke(app, ["start"])
            assert result.exit_code == 0
            assert call_order == ["acl_grant", "credential_ownership", "compose_up"], (
                f"Expected ACL → ownership → compose, got: {call_order}"
            )


class TestStartErrorHandlerACLCleanup:
    """Task 5.7: start error handler — ACL cleanup conditional on acl_granted."""

    def test_compose_failure_triggers_revoke(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """WHEN Phase 6 fails, THEN _revoke_acls is called (acl_granted=True)."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)

        from cli.main import app
        from core.exceptions import SandboxExecutionError

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="pass"),
            patch("cli.main._phase_hydrate"),
            patch("cli.main._phase_acl_grant"),
            patch("cli.main._phase_credential_ownership"),
            patch("cli.main._phase_helper_mkdir_chown_cache_log"),
            patch("cli.main._phase_compose_up", side_effect=SandboxExecutionError("unhealthy")),
            patch("cli.main._revoke_acls", return_value=[]) as mock_revoke,
            patch("cli.main._release_lock"),
        ):
            result = runner.invoke(app, ["start"])
            assert result.exit_code == 1
            mock_revoke.assert_called_once()

    def test_phase_5b_failure_triggers_revoke(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """WHEN Phase 5b (_phase_credential_ownership) fails, THEN _revoke_acls is called."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)

        from cli.main import app
        from core.exceptions import SandboxExecutionError

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="pass"),
            patch("cli.main._phase_hydrate"),
            patch("cli.main._phase_acl_grant"),
            patch("cli.main._phase_credential_ownership", side_effect=SandboxExecutionError("chown failed")),
            patch("cli.main._revoke_acls", return_value=[]) as mock_revoke,
            patch("cli.main._release_lock"),
        ):
            result = runner.invoke(app, ["start"])
            assert result.exit_code == 1
            mock_revoke.assert_called_once()

    def test_ipam_failure_does_not_trigger_revoke(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """WHEN IPAM fails (pre-Phase-5), THEN _revoke_acls is NOT called."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")

        from cli.main import app
        from core.ipam import IPAMExhaustedError

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", side_effect=IPAMExhaustedError("full")),
            patch("cli.main._revoke_acls") as mock_revoke,
            patch("cli.main._release_lock"),
        ):
            result = runner.invoke(app, ["start"])
            assert result.exit_code == 1
            mock_revoke.assert_not_called()


class TestStopLock:
    """Task 7.3: stop lock — acquired on success, BlockingIOError fails fast."""

    def test_stop_acquires_lock(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """WHEN stop succeeds, THEN lock is acquired and released."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._warm_check", return_value=True),
            patch("cli.main._acquire_state_lock", return_value=99) as mock_lock,
            patch("cli.main._compose_down"),
            patch("cli.main._revoke_acls", return_value=[]),
            patch("cli.main._release_lock") as mock_release,
        ):
            result = runner.invoke(app, ["stop"])
            assert result.exit_code == 0
            mock_lock.assert_called_once()
            mock_release.assert_called_once()

    def test_stop_lock_contention_exits(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """WHEN BlockingIOError raised, THEN exit code 1."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._warm_check", return_value=True),
            patch("cli.main._acquire_state_lock", side_effect=BlockingIOError("locked")),
            patch("cli.main._compose_down") as mock_down,
        ):
            result = runner.invoke(app, ["stop"])
            assert result.exit_code == 1
            assert "already in progress" in result.output.lower()
            mock_down.assert_not_called()


class TestDestroyFaultIsolation:
    """Task 8.6: destroy fault isolation — each phase independently handled."""

    def test_compose_down_failure_continues(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """WHEN compose-down fails, THEN destroy continues to rmtree/IPAM/registry."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)
        _write_ipam(instance_id, 0)

        from cli.main import app
        from core.exceptions import SandboxExecutionError

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._compose_down", side_effect=SandboxExecutionError("timeout")),
            patch("cli.main._revoke_acls", return_value=[]),
            patch("cli.main._release_lock"),
            patch("shutil.rmtree") as mock_rmtree,
        ):
            result = runner.invoke(app, ["destroy", "--force"])
            assert result.exit_code == 0
            # rmtree still called despite compose failure
            mock_rmtree.assert_called_once()
            # Warning emitted
            assert "warning" in result.output.lower() or "⚠" in result.output

    def test_rmtree_file_not_found_silenced(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """WHEN rmtree raises FileNotFoundError, THEN silenced (idempotent)."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)
        _write_ipam(instance_id, 0)

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._compose_down"),
            patch("cli.main._revoke_acls", return_value=[]),
            patch("cli.main._release_lock"),
            patch("shutil.rmtree", side_effect=FileNotFoundError("gone")),
        ):
            result = runner.invoke(app, ["destroy", "--force"])
            assert result.exit_code == 0


# ── Coverage Gap Tests ──────────────────────────────────────────────────────


class TestContainerStatusEdgeCases:
    """Cover L110 (missing compose.yml), L142 (blank NDJSON), L154-155 (bad JSON)."""

    def test_missing_compose_file_returns_empty(self) -> None:
        """L110: compose.yml absent → empty list immediately."""
        from cli.main import _container_status
        from core.hydration import InstanceConfig

        config = InstanceConfig.model_validate(
            {
                "instance": {
                    "name": "t",
                    "user_project_root": "/x",
                    "host_uid": "1000",
                },
                "components": {"mcp_firecrawl": False, "mcp_puppeteer": False},
                "components_db_postgres": {"enabled": False},
            }
        )
        result = _container_status("/nonexistent/dir", "t", "s", config)
        assert result == []

    def test_blank_and_malformed_ndjson_skipped(self, tmp_path: Path) -> None:
        """L142, L154-155: blank lines and bad JSON are silently skipped."""
        from cli.main import _container_status
        from core.hydration import InstanceConfig

        # Create compose.yml so the early return is bypassed
        docker_dir = tmp_path / "docker"
        docker_dir.mkdir()
        (docker_dir / "compose.yml").write_text("")
        (tmp_path / ".sandbox.env").write_text("")

        config = InstanceConfig.model_validate(
            {
                "instance": {
                    "name": "t",
                    "user_project_root": "/x",
                    "host_uid": "1000",
                },
                "components": {"mcp_firecrawl": False, "mcp_puppeteer": False},
                "components_db_postgres": {"enabled": False},
            }
        )

        # Mix of blank, malformed, and valid NDJSON — blank between content lines survives strip()
        stdout = 'NOT-JSON\n   \n{"Name":"c1","Service":"svc","State":"running","Health":"","Status":"Up"}'
        mock_result = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

        from core.executor import Executor

        with patch.object(Executor, "run", return_value=mock_result):
            containers = _container_status(str(tmp_path), "t", "s", config)
            assert len(containers) == 1
            assert containers[0].name == "c1"


class TestComputeAncestorsOSError:
    """Cover L255-256: _compute_ancestors OSError on stat."""

    def test_oserror_breaks_walk(self) -> None:
        from cli.main import _compute_ancestors

        # Fully mock os.stat: /fake/deep succeeds (owned by us), /fake raises OSError
        fake_stat_ok = MagicMock()
        fake_stat_ok.st_uid = os.getuid()

        def controlled_stat(path: str) -> MagicMock:
            if path == "/fake":
                raise OSError("permission denied")
            return fake_stat_ok

        with patch("os.stat", side_effect=controlled_stat):
            result = _compute_ancestors("/fake/deep/child")
            # /fake/deep/child → parent /fake/deep (ok) → parent /fake (OSError → break)
            assert "/fake" not in result


class TestDiagnoseTraverseFailureEdgeCases:
    """Cover L406-407 (OSError), L415 (group exec), L446 (diag in error msg)."""

    def test_oserror_returns_cannot_stat(self) -> None:
        """L406-407: OSError on stat returns diagnostic."""

        from cli.main import _diagnose_traverse_failure

        fake_pw = MagicMock()
        fake_pw.pw_uid = 2000
        fake_pw.pw_gid = 2000

        traversable = MagicMock(spec=os.stat_result)
        traversable.st_uid = 0
        traversable.st_gid = 0
        traversable.st_mode = 0o755

        def controlled_stat(path: str) -> MagicMock:
            if path == "/vanished":
                raise OSError("gone")
            return traversable

        with (
            patch("pwd.getpwnam", return_value=fake_pw),
            patch("os.stat", side_effect=controlled_stat),
        ):
            result = _diagnose_traverse_failure("/vanished/dir", "sandbox")
            assert "cannot stat" in result.lower()

    def test_diag_appended_to_error_message(self, tmp_path: Path) -> None:
        """L446: when diag returns a non-empty string, it's included in the error."""
        from cli.main import _phase_acl_grant
        from core.exceptions import SandboxExecutionError

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)
        (instance_dir / "docker").mkdir()
        (instance_dir / "config").mkdir()
        (instance_dir / ".sandbox.env").write_text("")

        with (
            patch(
                "subprocess.run",
                side_effect=subprocess.CalledProcessError(1, ["setfacl"], stderr="boom"),
            ),
            patch(
                "cli.main._diagnose_traverse_failure",
                return_value="Diagnosis: user lacks execute on /home",
            ),
            pytest.raises(SandboxExecutionError) as exc_info,
        ):
            _phase_acl_grant(str(instance_dir), "sandbox")

        assert "Diagnosis: user lacks execute" in str(exc_info.value)


class TestStopACLWarningEmission:
    """Cover L947: stop command emits ACL revoke warnings."""

    def test_stop_emits_acl_warnings(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._warm_check", return_value=True),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._compose_down"),
            patch("cli.main._revoke_acls", return_value=["ACL revoke warning for test: fail"]),
            patch("cli.main._release_lock"),
        ):
            result = runner.invoke(app, ["stop"])
            assert result.exit_code == 0
            assert "ACL revoke warning" in result.output


class TestDestroyFaultIsolationWarnings:
    """Cover L1027, L1041-1042, L1049-1050: destroy ACL/IPAM/registry warnings."""

    def test_destroy_emits_acl_warnings(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """L1027: ACL revoke warnings emitted during destroy."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)
        _write_ipam(instance_id, 0)

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._compose_down"),
            patch("cli.main._revoke_acls", return_value=["ACL warning: test"]),
            patch("cli.main._release_lock"),
            patch("shutil.rmtree"),
        ):
            result = runner.invoke(app, ["destroy", "--force"])
            assert result.exit_code == 0
            assert "ACL warning" in result.output

    def test_destroy_ipam_failure_emits_warning(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """L1041-1042: IPAM release failure emits warning, doesn't abort."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._compose_down"),
            patch("cli.main._revoke_acls", return_value=[]),
            patch("cli.main._release_lock"),
            patch("shutil.rmtree"),
            patch("cli.main.IPAMLedger.release", side_effect=RuntimeError("corrupt")),
        ):
            result = runner.invoke(app, ["destroy", "--force"])
            assert result.exit_code == 0
            assert "IPAM release warning" in result.output

    def test_destroy_registry_failure_emits_warning(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """L1049-1050: Registry cleanup failure emits warning, doesn't abort."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)
        _write_ipam(instance_id, 0)

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._compose_down"),
            patch("cli.main._revoke_acls", return_value=[]),
            patch("cli.main._release_lock"),
            patch("shutil.rmtree"),
            patch("cli.main.InstanceRegistry.remove", side_effect=KeyError("not found")),
        ):
            result = runner.invoke(app, ["destroy", "--force"])
            assert result.exit_code == 0
            assert "Registry cleanup warning" in result.output


class TestDiagnoseGroupExecBranch:
    """Cover L415: _diagnose_traverse_failure group exec check branch."""

    def test_group_exec_branch(self) -> None:
        """L415: when file is group-owned by target user, checks S_IXGRP.

        Fully mocked — no real filesystem or user database access.
        """

        from cli.main import _diagnose_traverse_failure

        # Target user: uid=2000, gid=2000
        fake_pw = MagicMock()
        fake_pw.pw_uid = 2000
        fake_pw.pw_gid = 2000

        # /synthetic/grouptest: uid=9999 (not user), gid=2000 (matches user), mode=0o750 (group exec)
        group_match = MagicMock(spec=os.stat_result)
        group_match.st_uid = 9999
        group_match.st_gid = 2000
        group_match.st_mode = 0o750

        # All other dirs: root-owned, o+x
        traversable = MagicMock(spec=os.stat_result)
        traversable.st_uid = 0
        traversable.st_gid = 0
        traversable.st_mode = 0o755

        def controlled_stat(path: str) -> MagicMock:
            if path == "/synthetic/grouptest":
                return group_match
            return traversable

        with (
            patch("pwd.getpwnam", return_value=fake_pw),
            patch("os.stat", side_effect=controlled_stat),
        ):
            result = _diagnose_traverse_failure("/synthetic/grouptest/child", "sandbox")
            # Group exec is set, so /synthetic/grouptest should not be flagged
            assert result == "" or (result is not None and "/synthetic/grouptest" not in result)


class TestStatusIPAMExhausted:
    """Cover L1139-1140, L1171-1172: status IPAMExhaustedError passthrough."""

    def test_status_ipam_exhausted_continues(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """WHEN IPAM is exhausted, THEN status still renders (no crash)."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)

        from cli.main import ContainerInfo, app
        from core.ipam import IPAMExhaustedError

        containers = [
            ContainerInfo(
                name="test-core-1",
                service="core",
                state="running",
                health="healthy",
                status="Up 5 min",
            )
        ]

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._container_status", return_value=containers),
            patch(
                "cli.main.IPAMLedger.peek_next_slot",
                side_effect=IPAMExhaustedError("full"),
            ),
        ):
            result = runner.invoke(app, ["status"])
            assert result.exit_code == 0
            # Status renders despite IPAM exhaustion
            assert "running" in result.output.lower() or "core" in result.output.lower()


# ── Section 7 (host-config-machinectl-auth): auth-mode-aware preview ──────


class TestDryRunAuthModePreview:
    """Task 7.7: --dry-run preview reflects machinectl_authentication mode."""

    def test_dry_run_preview_shows_polkit_command_without_sudo(
        self, runner: CliRunner, mock_sandbox_ai_home: Path, project_config_factory: HostConfigFactory
    ) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam("myproject-abc123", 0)
        _create_tooling_plane(home)

        from cli.main import app

        polkit_cfg = project_config_factory(user="sandbox", auth="polkit")
        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main.HostConfig.from_toml", return_value=polkit_cfg),
        ):
            result = runner.invoke(app, ["start", "--dry-run"])

        assert result.exit_code == 0
        # The previewed compose/handover commands must NOT include sudo
        # but must still invoke machinectl shell.
        assert "machinectl shell sandbox@.host" in result.output
        # Command-preview lines should not start with `sudo machinectl`
        for line in result.output.splitlines():
            stripped = line.strip().lstrip("$").strip()
            if stripped.startswith("sudo machinectl"):
                raise AssertionError(f"polkit dry-run leaked sudo prefix: {line!r}")

    def test_dry_run_preview_shows_sudo_prefix_in_sudo_mode(
        self, runner: CliRunner, mock_sandbox_ai_home: Path, project_config_factory: HostConfigFactory
    ) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam("myproject-abc123", 0)
        _create_tooling_plane(home)

        from cli.main import app

        sudo_cfg = project_config_factory(user="sandbox", auth="sudo")
        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main.HostConfig.from_toml", return_value=sudo_cfg),
        ):
            result = runner.invoke(app, ["start", "--dry-run"])

        assert result.exit_code == 0
        assert "sudo machinectl shell sandbox@.host" in result.output


class TestPolkitEndToEnd:
    """Task 7.8: polkit-mode commands construct subprocess calls without sudo."""

    def test_status_polkit_mode_container_status_call_omits_sudo(
        self, runner: CliRunner, mock_sandbox_ai_home: Path, project_config_factory: HostConfigFactory
    ) -> None:
        """`status` under polkit mode invokes `_container_status` with auth=POLKIT.

        The call list does not include `sudo` as the first argv element.
        """
        from cli.main import app
        from core.host_config import MachinectlAuth

        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam("myproject-abc123", 0)

        polkit_cfg = project_config_factory(user="sandbox", auth="polkit")
        captured: dict[str, object] = {}

        def fake_container_status(
            instance_dir: str,
            name: str,
            host_user: str,
            config: object,
            auth: MachinectlAuth,
        ) -> list[object]:
            captured["host_user"] = host_user
            captured["auth"] = auth
            return []

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main.HostConfig.from_toml", return_value=polkit_cfg),
            patch("cli.main._container_status", side_effect=fake_container_status),
        ):
            result = runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert captured["host_user"] == "sandbox"
        assert captured["auth"] == MachinectlAuth.POLKIT


# ── Post-init commands fail loudly without sandbox-ai.toml ───────────────────


class TestPostInitMissingHostConfig:
    """Verify the 'Post-init command fails without host config' scenario.

    Spec: openspec/changes/host-config-machinectl-auth/specs/orchestrator-cli/spec.md
    """

    @pytest.mark.parametrize("command", ["start", "stop", "attach", "destroy", "status"])
    def test_post_init_command_exits_when_sandbox_ai_toml_missing(
        self, runner: CliRunner, mock_sandbox_ai_home: Path, command: str
    ) -> None:
        from cli.main import app

        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam("myproject-abc123", 0)

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch(
                "cli.main.HostConfig.from_toml",
                side_effect=FileNotFoundError("No sandbox-ai.toml found. Run sandbox init."),
            ),
        ):
            args = [command]
            if command == "destroy":
                args.append("--force")
            result = runner.invoke(app, args)

        assert result.exit_code == 1, f"{command} should exit 1 without sandbox-ai.toml"
        assert "sandbox-ai.toml" in result.output
