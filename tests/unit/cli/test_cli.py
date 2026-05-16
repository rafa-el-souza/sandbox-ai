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

import cli.main as _cli_main_module
import pytest
import typer
from typer.testing import CliRunner

if TYPE_CHECKING:
    from core.dispatch import ProbeOutcome
    from core.hydration import InstanceConfig

    from tests.unit.conftest import HostConfigFactory

# ── Fixtures ─────────────────────────────────────────────────────────────────

SANDBOX_AI_HOME = "/fake/sandbox-ai"
PROJECT_DIR = "/home/user/myproject"
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
host_uid = "1000"

[workspaces.main]
bootstrap_mode = "copy"
source = "/home/user/myproject"
path = "/home/user/myproject"

[core]
shm_size = "2gb"
pids_limit = 400
base_image = "cgr.dev/chainguard/wolfi-base:latest"
base_distro_family = "wolfi"
git_user = ""
git_email = ""

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


# Real seeder captured at import time; the autouse fixture below patches
# ``cli.main._seed_host_config_if_absent`` at test-setup, so tests opt back
# in via ``wraps=_REAL_SEED_HOST_CONFIG``.
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


@pytest.fixture
def stub_bridge_resolution() -> typing.Iterator[None]:
    """Opt-in patch for the workspace bridge gid + subuid resolvers.

    Most CLI tests don't reach the start pipeline and don't need this. Tests
    that drive ``start``/``dry-run`` or directly invoke a helper-recipe phase
    take this fixture explicitly to redirect resolvers away from the real
    /etc/subgid + sb-ws group. Tests that need different values override with
    their own ``with patch(...)`` block.
    """
    with (
        patch("core.hydration.workspace_bridge_gid", return_value=200000),
        patch("core.hydration.in_container_gid_for_host_gid", return_value=1000),
        patch("cli.main.workspace_bridge_gid", return_value=200000),
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
    """Resolve the per-user home from SANDBOX_AI_HOME (autouse fixture sets it)."""
    return Path(os.environ["SANDBOX_AI_HOME"])


def _seed_registry(home: Path) -> None:
    """Mark the per-user state tree as initialized for hard-fail bypass."""
    state_dir = home / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    registry = state_dir / "instances.json"
    if not registry.exists():
        registry.write_text("{}")


def _register_instance(inst: str) -> Path:
    """Register ``inst`` in the per-user registry and scaffold its instance dir.

    The instance dir is ``<user_home>/instances/<inst>/`` per the change-5
    layout. Existing registry entries are preserved; the new entry is merged
    in. Returns the instance dir as a ``Path``.
    """
    state_dir = _user_home() / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    reg = state_dir / "instances.json"
    existing: dict[str, dict[str, str]] = json.loads(reg.read_text()) if reg.exists() else {}
    instance_dir = _user_home() / "instances" / inst
    existing[inst] = {"instance_dir": str(instance_dir), "created_at": "2026-01-01T00:00:00Z"}
    reg.write_text(json.dumps(existing))
    (instance_dir / "docker" / "core").mkdir(parents=True)
    (instance_dir / "docker" / "admin").mkdir(parents=True)
    (instance_dir / "docker" / "extras").mkdir(parents=True)
    (instance_dir / "config" / "proxy").mkdir(parents=True)
    (instance_dir / "config" / "admin").mkdir(parents=True)
    (instance_dir / "config" / "core").mkdir(parents=True)
    (instance_dir / "config" / "coredns").mkdir(parents=True)
    (instance_dir / "config" / "dnsdist").mkdir(parents=True)
    (instance_dir / "log" / "orchestrator").mkdir(parents=True)
    (instance_dir / "sandbox.toml").write_bytes(VALID_TOML_CONTENT)
    (instance_dir / ".sandbox.env").write_text('CORE_ANTHROPIC_API_KEY="test"')
    (instance_dir / ".initialized").write_text("")
    return instance_dir


def _write_ipam(inst: str, base_index: int) -> None:
    """Write a single IPAM entry to ``<user_home>/state/ipam.json``."""
    state_dir = _user_home() / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    ipam = state_dir / "ipam.json"
    ipam.write_text(json.dumps({inst: base_index}))


# ── sandbox start ────────────────────────────────────────────────────────────


class TestStartHappyPath:
    """Task 8.2: sandbox start happy path — all phases sequenced correctly."""

    def test_start_existing_instance_full_sequence(self, runner: CliRunner) -> None:
        """Existing instance: full start sequence (registry hit -> compose -> handover)."""
        inst = "myproject"
        _register_instance(inst)
        _write_ipam(inst, 0)

        from cli.main import app

        with (
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="proxypass123"),
            patch("cli.main._phase_hydrate") as mock_hydrate,
            patch("cli.main._phase_acl_grant") as mock_acl,
            patch("cli.main._phase_helper_cp_chown_ro_files"),
            patch("cli.main._phase_workspace_shared_group"),
            patch("cli.main._phase_helper_mkdir_chown_cache_log"),
            patch("cli.main._phase_grant_post_hydrate_daemon_read"),
            patch("cli.main._phase_compose_up") as mock_compose,
            patch("cli.main._build_attach_argv", return_value=["/bin/true"]),
            patch(
                "cli.main.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["/bin/true"], returncode=0),
            ) as mock_handover,
            patch("cli.main._release_lock"),
            patch("cli.main._stdin_is_tty", return_value=True),
        ):
            result = runner.invoke(app, ["start", inst])
            assert result.exit_code == 0
            mock_hydrate.assert_called_once()
            mock_acl.assert_called_once()
            mock_compose.assert_called_once()
            mock_handover.assert_called_once()

    def test_start_no_init_errors(self, runner: CliRunner) -> None:
        """New project: registry miss → error with init guidance."""
        from cli.main import app

        _seed_registry(_user_home())
        result = runner.invoke(app, ["start", "newproject"])
        assert result.exit_code == 1
        assert "sandbox init" in result.output.lower()

    def test_start_partial_init_errors(self, runner: CliRunner) -> None:
        """Instance registered but .initialized sentinel missing → error."""
        inst = "myproject"
        instance_dir = _register_instance(inst)
        # Remove the sentinel
        (instance_dir / ".initialized").unlink()

        from cli.main import app

        result = runner.invoke(app, ["start", inst])
        assert result.exit_code == 1
        assert "partially initialized" in result.output.lower() or "destroy" in result.output.lower()

    def test_start_progress_output(self, runner: CliRunner) -> None:
        """Start shows phase progress indicators."""
        inst = "myproject"
        _register_instance(inst)
        _write_ipam(inst, 0)

        from cli.main import app

        with (
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="proxypass123"),
            patch("cli.main._phase_hydrate"),
            patch("cli.main._phase_acl_grant"),
            patch("cli.main._phase_helper_cp_chown_ro_files"),
            patch("cli.main._phase_workspace_shared_group"),
            patch("cli.main._phase_helper_mkdir_chown_cache_log"),
            patch("cli.main._phase_grant_post_hydrate_daemon_read"),
            patch("cli.main._phase_compose_up"),
            patch("cli.main._build_attach_argv", return_value=["/bin/true"]),
            patch(
                "cli.main.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["/bin/true"], returncode=0),
            ),
            patch("cli.main._release_lock"),
        ):
            result = runner.invoke(app, ["start", inst])
            assert result.exit_code == 0
            out = result.output.lower()
            # Should show progress indicators
            assert "ipam" in out or "network" in out
            assert "compose" in out or "containers" in out

    def test_start_handover_indication(self, runner: CliRunner) -> None:
        """Start shows handover indication before PTY exec."""
        inst = "myproject"
        _register_instance(inst)
        _write_ipam(inst, 0)

        from cli.main import app

        with (
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="pass"),
            patch("cli.main._phase_hydrate"),
            patch("cli.main._phase_acl_grant"),
            patch("cli.main._phase_helper_cp_chown_ro_files"),
            patch("cli.main._phase_workspace_shared_group"),
            patch("cli.main._phase_helper_mkdir_chown_cache_log"),
            patch("cli.main._phase_grant_post_hydrate_daemon_read"),
            patch("cli.main._phase_compose_up"),
            patch("cli.main._build_attach_argv", return_value=["/bin/true"]),
            patch(
                "cli.main.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["/bin/true"], returncode=0),
            ),
            patch("cli.main._release_lock"),
            patch("cli.main._stdin_is_tty", return_value=True),
        ):
            result = runner.invoke(app, ["start", inst])
            assert result.exit_code == 0
            out = result.output.lower()
            assert "handing over" in out or "handover" in out or "admin shell" in out

    def test_start_skips_handover_when_no_tty(self, runner: CliRunner) -> None:
        """Non-TTY stdin: skip the interactive handover and print attach hint."""
        inst = "myproject"
        _register_instance(inst)
        _write_ipam(inst, 0)

        from cli.main import app

        with (
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="pass"),
            patch("cli.main._phase_hydrate"),
            patch("cli.main._phase_acl_grant"),
            patch("cli.main._phase_helper_cp_chown_ro_files"),
            patch("cli.main._phase_workspace_shared_group"),
            patch("cli.main._phase_helper_mkdir_chown_cache_log"),
            patch("cli.main._phase_compose_up"),
            patch("cli.main._build_attach_argv", return_value=["/bin/true"]),
            patch(
                "cli.main.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["/bin/true"], returncode=0),
            ) as mock_handover,
            patch("cli.main._release_lock"),
            patch("cli.main._stdin_is_tty", return_value=False),
        ):
            result = runner.invoke(app, ["start", inst])
            assert result.exit_code == 0
            mock_handover.assert_not_called()
            assert "attach with" in result.output.lower()

    def test_start_no_handover_flag_skips_handover(self, runner: CliRunner) -> None:
        """--no-handover: skip the interactive handover even with a TTY."""
        inst = "myproject"
        _register_instance(inst)
        _write_ipam(inst, 0)

        from cli.main import app

        with (
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="pass"),
            patch("cli.main._phase_hydrate"),
            patch("cli.main._phase_acl_grant"),
            patch("cli.main._phase_helper_cp_chown_ro_files"),
            patch("cli.main._phase_workspace_shared_group"),
            patch("cli.main._phase_helper_mkdir_chown_cache_log"),
            patch("cli.main._phase_compose_up"),
            patch("cli.main._build_attach_argv", return_value=["/bin/true"]),
            patch(
                "cli.main.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["/bin/true"], returncode=0),
            ) as mock_handover,
            patch("cli.main._release_lock"),
            patch("cli.main._stdin_is_tty", return_value=True),
        ):
            result = runner.invoke(app, ["start", inst, "--no-handover"])
            assert result.exit_code == 0
            mock_handover.assert_not_called()
            assert "attach with" in result.output.lower()

    def test_start_multi_workspace_skips_handover_with_hint(self, runner: CliRunner) -> None:
        """When N>1 workspaces, start prints the attach hint with the workspace
        list and skips the default handover (operator must pick a workspace
        explicitly via `sandbox attach <inst> <ws>`).
        """
        inst = "myproject"
        instance_dir = _register_instance(inst)
        # Replace the single-workspace TOML with one containing two workspaces.
        ws_main = (
            b'[workspaces.main]\nbootstrap_mode = "copy"\n'
            b'source = "/home/user/myproject"\npath = "/home/user/myproject"'
        )
        ws_scratch = (
            b'[workspaces.scratch]\nbootstrap_mode = "empty"\npath = "/home/user/scratch"'
        )
        multi_ws_toml = VALID_TOML_CONTENT.replace(ws_main, ws_main + b"\n\n" + ws_scratch)
        (instance_dir / "sandbox.toml").write_bytes(multi_ws_toml)
        _write_ipam(inst, 0)

        from cli.main import app

        with (
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="proxypass123"),
            patch("cli.main._phase_hydrate"),
            patch("cli.main._phase_acl_grant"),
            patch("cli.main._phase_helper_cp_chown_ro_files"),
            patch("cli.main._phase_workspace_shared_group"),
            patch("cli.main._phase_helper_mkdir_chown_cache_log"),
            patch("cli.main._phase_grant_post_hydrate_daemon_read"),
            patch("cli.main._phase_compose_up"),
            patch("cli.main._build_attach_argv", return_value=["/bin/true"]),
            patch(
                "cli.main.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["/bin/true"], returncode=0),
            ) as mock_handover,
            patch("cli.main._release_lock"),
            patch("cli.main._stdin_is_tty", return_value=True),
        ):
            result = runner.invoke(app, ["start", inst])
            assert result.exit_code == 0
            mock_handover.assert_not_called()
            # Hint mentions multiple workspaces and lists them
            assert "Multiple workspaces" in result.output
            assert "main" in result.output
            assert "scratch" in result.output


class TestStartSecretCompletenessGate:
    """Task 4.4a: secret completeness gate — must run before _acquire_state_lock."""

    def test_start_exits_on_missing_secrets(self, runner: CliRunner) -> None:
        """start exits code 1 when _check_secrets returns missing secrets."""
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app

        with (
            patch("cli.main._check_secrets", return_value=["PG_PASSWORD"]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock") as mock_lock,
        ):
            result = runner.invoke(app, ["start", inst])
            assert result.exit_code == 1
            assert "pg_password" in result.output.lower()
            # Gate must fire BEFORE lock acquisition
            mock_lock.assert_not_called()


class TestStartDoctorChain1PreFlight:
    """Task 4.5a: doctor Chain 1 (Privilege Boundary) pre-flight in start."""

    def test_start_exits_on_doctor_chain1_failure(self, runner: CliRunner) -> None:
        """start exits code 1 when run_check_subset returns fail for Privilege Boundary."""
        from core.doctor import CheckResult

        inst = "myproject"
        _register_instance(inst)

        failed_results = [
            CheckResult(status="fail", name="machinectl", detail="not configured", remediation="fix sudoers"),
        ]

        from cli.main import app

        with (
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=failed_results),
            patch("cli.main.render_results") as mock_render,
            patch("cli.main._warm_check") as mock_warm,
        ):
            result = runner.invoke(app, ["start", inst])
            assert result.exit_code == 1
            mock_render.assert_called_once()
            # Gate must fire BEFORE warm check
            mock_warm.assert_not_called()


class TestStartComposeSpinner:
    """Task 4.6a: console.status() spinner during compose-up phase."""

    def test_start_uses_console_status_for_compose(self, runner: CliRunner) -> None:
        """start uses console.status() context manager during compose-up."""
        inst = "myproject"
        _register_instance(inst)
        _write_ipam(inst, 0)

        from cli.main import app

        with (
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="pass"),
            patch("cli.main._phase_hydrate"),
            patch("cli.main._phase_acl_grant"),
            patch("cli.main._phase_helper_cp_chown_ro_files"),
            patch("cli.main._phase_workspace_shared_group"),
            patch("cli.main._phase_helper_mkdir_chown_cache_log"),
            patch("cli.main._phase_grant_post_hydrate_daemon_read"),
            patch("cli.main._phase_compose_up"),
            patch("cli.main._build_attach_argv", return_value=["/bin/true"]),
            patch(
                "cli.main.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["/bin/true"], returncode=0),
            ),
            patch("cli.main._release_lock"),
            patch.object(
                __import__("cli.main", fromlist=["console"]).console,
                "status",
            ) as mock_status,
        ):
            result = runner.invoke(app, ["start", inst])
            assert result.exit_code == 0
            mock_status.assert_called_once()
            # Verify __enter__ was called (context manager was used)
            mock_status.return_value.__enter__.assert_called()


class TestStartWarmExit:
    """Task 8.2: pre-lock warm-exit — no locks acquired."""

    def test_warm_instance_exits_before_locking(self, runner: CliRunner) -> None:
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app

        with (
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=True),
            patch("cli.main._acquire_state_lock") as mock_lock,
        ):
            result = runner.invoke(app, ["start", inst])
            assert result.exit_code == 0
            assert "already running" in result.output.lower()
            mock_lock.assert_not_called()


class TestStartLockContention:
    """Task 8.2: lock contention exit."""

    def test_lock_contention_exits_with_message(self, runner: CliRunner) -> None:
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app

        with (
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", side_effect=BlockingIOError("locked")),
        ):
            result = runner.invoke(app, ["start", inst])
            assert result.exit_code == 1
            assert "already in progress" in result.output.lower()


class TestStartIPAMExhausted:
    """Task 8.2: IPAM exhausted exit."""

    def test_ipam_exhausted_releases_lock_and_exits(self, runner: CliRunner) -> None:
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app
        from core.ipam import IPAMExhaustedError

        with (
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", side_effect=IPAMExhaustedError("full")),
            patch("cli.main._release_lock") as mock_release,
        ):
            result = runner.invoke(app, ["start", inst])
            assert result.exit_code == 1
            mock_release.assert_called_once()


class TestStartWorkspaceBridgeMissing:
    """Task 5.2: hydration aborts when workspace bridge group is missing."""

    def test_start_aborts_with_remediation_hint(self, runner: CliRunner) -> None:
        from cli.main import app
        from core.host_config import WorkspaceBridgeGroupMissingError

        inst = "myproject"
        _register_instance(inst)

        with (
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch(
                # Post-reorder, _phase_workspace_shared_group runs FIRST among
                # the bridge-group-touching phases (per the
                # "Workspace Shared-Group Phase Ordering" requirement and the
                # "Phase Order Contract" reorder for finding 8.D alt #1), so
                # the WorkspaceBridgeGroupMissingError surfaces here, not from
                # _phase_hydrate.
                "cli.main._phase_workspace_shared_group",
                side_effect=WorkspaceBridgeGroupMissingError("group 'sb-ws' does not exist"),
            ),
            patch("cli.main._release_lock") as mock_release,
        ):
            result = runner.invoke(app, ["start", inst])
            assert result.exit_code == 1
            assert "sb-ws" in result.output
            assert "sandbox doctor" in result.output
            mock_release.assert_called_once()

    def test_dry_run_aborts_with_remediation_hint(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        from cli.main import app
        from core.host_config import WorkspaceBridgeGroupMissingError

        home = mock_sandbox_ai_home
        inst = "myproject"
        _register_instance(inst)
        _write_ipam("myproject", 0)
        _create_tooling_plane(home)

        with (
            patch(
                "cli.main.build_jinja_context",
                side_effect=WorkspaceBridgeGroupMissingError("group 'sb-ws' does not exist"),
            ),
        ):
            result = runner.invoke(app, ["start", inst, "--dry-run"])
            assert result.exit_code == 1
            assert "sb-ws" in result.output
            assert "sandbox doctor" in result.output


class TestStartComposeUnhealthy:
    """Task 8.2: compose unhealthy exit."""

    def test_unhealthy_compose_releases_lock_and_exits(self, runner: CliRunner) -> None:
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app
        from core.exceptions import SandboxExecutionError

        with (
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="pass"),
            patch("cli.main._phase_hydrate"),
            patch("cli.main._phase_acl_grant"),
            patch("cli.main._phase_helper_cp_chown_ro_files"),
            patch("cli.main._phase_workspace_shared_group"),
            patch("cli.main._phase_helper_mkdir_chown_cache_log"),
            patch("cli.main._phase_grant_post_hydrate_daemon_read"),
            patch("cli.main._phase_compose_up", side_effect=SandboxExecutionError("unhealthy")),
            patch("cli.main._release_lock") as mock_release,
        ):
            result = runner.invoke(app, ["start", inst])
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

        # _phase_credentials reconciles ownership on ipc_ssh_key /
        # ipc_known_hosts after generate_ssh_keypair returns; create stubs
        # so the os.stat reconciliation finds them when the keypair
        # generator is mocked out.
        (secrets_dir / "ipc_ssh_key").write_text("")
        (secrets_dir / "ipc_known_hosts").write_text("")

        with (
            patch("cli.main.write_htpasswd"),
            patch("cli.main.generate_ssh_keypair") as mock_ssh,
        ):
            _phase_credentials(str(tmp_path), core_ipc_ip="10.100.6.3")
            assert mock_ssh.call_count == 2
            calls = mock_ssh.call_args_list
            assert calls[0] == call(str(tmp_path), "auth")
            assert calls[1] == call(str(tmp_path), "host", core_ipc_ip="10.100.6.3")

    def test_grant_post_hydrate_daemon_read_setfacls_helper_cp_and_direct_files(
        self, tmp_path: Path
    ) -> None:
        """Post-hydrate setfacl pass covers BOTH helper-cp source files
        AND daemon-read direct files (DAEMON_READ_DIRECT_FILES).

        Iteration 5 of the cluster-1 8.D structural fix. Empirical
        evidence: iteration 4 made phases 1-8 green but compose-up
        failed with ``open /…/docker/compose.yml: permission denied``
        because compose.yml is dev-created (by hydrate's
        write_restricted) and never transferred via helper-cp; the
        previous unified pass only iterated RO_FILE_RECIPES +
        EXEC_FILE_RECIPES.

        The DAEMON_READ_DIRECT_FILES inventory is the single source of
        truth for files the daemon reads in place forever (compose.yml
        + conditional extras). The post-hydrate pass now setfacls each
        existing file across both categories; missing files are skipped
        defensively (covers conditional extras whose presence depends
        on InstanceConfig component flags).
        """
        from cli.main import (
            DAEMON_READ_DIRECT_FILES,
            EXEC_FILE_RECIPES,
            RO_FILE_RECIPES,
            _grant_post_hydrate_daemon_read,
        )

        # Materialize every recipe file on disk so the helper iterates
        # them; leave one helper-cp source file absent and one daemon-
        # read direct file absent to verify the skip-if-missing branch
        # on both code paths.
        absent_recipe: tuple[str, str] | None = None
        for parent, files, _uid, _mode in (*RO_FILE_RECIPES, *EXEC_FILE_RECIPES):
            parent_abs = tmp_path / parent
            parent_abs.mkdir(parents=True, exist_ok=True)
            for fname in files:
                if absent_recipe is None:
                    absent_recipe = (parent, fname)
                    continue
                (parent_abs / fname).write_text("dummy")

        absent_direct: tuple[str, str] | None = None
        for parent, files in DAEMON_READ_DIRECT_FILES:
            parent_abs = tmp_path / parent
            parent_abs.mkdir(parents=True, exist_ok=True)
            for fname in files:
                if absent_direct is None:
                    absent_direct = (parent, fname)
                    continue
                (parent_abs / fname).write_text("dummy")

        recorded: list[list[str]] = []

        def _record(args: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            recorded.append(list(args))
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        with patch("cli.main.subprocess.run", side_effect=_record):
            _grant_post_hydrate_daemon_read(str(tmp_path), "claude-sandbox")

        recorded_paths = {args[-1] for args in recorded}

        # Both absent files were skipped on their respective code paths.
        assert absent_recipe is not None and absent_direct is not None
        absent_recipe_path = str(tmp_path / absent_recipe[0] / absent_recipe[1])
        absent_direct_path = str(tmp_path / absent_direct[0] / absent_direct[1])
        assert absent_recipe_path not in recorded_paths
        assert absent_direct_path not in recorded_paths

        # All present helper-cp source files setfacl'd.
        for parent, files, _u, _m in (*RO_FILE_RECIPES, *EXEC_FILE_RECIPES):
            for fname in files:
                p = str(tmp_path / parent / fname)
                if (parent, fname) == absent_recipe:
                    continue
                assert p in recorded_paths, f"missing setfacl on helper-cp source {p}"

        # All present daemon-read direct files setfacl'd — including
        # compose.yml (the iteration-5 regression).
        for parent, files in DAEMON_READ_DIRECT_FILES:
            for fname in files:
                p = str(tmp_path / parent / fname)
                if (parent, fname) == absent_direct:
                    continue
                assert p in recorded_paths, f"missing setfacl on daemon-read direct {p}"

        # Every invocation has the canonical shape.
        for args in recorded:
            assert args[:3] == ["setfacl", "-m", "u:claude-sandbox:r"], args

    def test_grant_post_hydrate_daemon_read_propagates_setfacl_failure(
        self, tmp_path: Path
    ) -> None:
        """A failed setfacl surfaces as SandboxExecutionError (no silent skip).

        Per admin-reframe (Fix B'), ``ipc_ssh_key`` is no longer a helper-cp
        target, so use ``ipc_host_key`` (still in RO_FILE_RECIPES) to trigger
        the setfacl pass.
        """
        from cli.main import _grant_post_hydrate_daemon_read
        from core.exceptions import SandboxExecutionError

        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        (secrets_dir / "ipc_host_key").write_text("k")

        def _fail(args: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.CalledProcessError(returncode=1, cmd=args, stderr="EPERM")

        with (
            patch("cli.main.subprocess.run", side_effect=_fail),
            pytest.raises(SandboxExecutionError, match="grant daemon read on post-hydrate target"),
        ):
            _grant_post_hydrate_daemon_read(str(tmp_path), "claude-sandbox")

    def test_phase_grant_post_hydrate_daemon_read_is_thin_wrapper(self, tmp_path: Path) -> None:
        """`_phase_grant_post_hydrate_daemon_read` is a thin wrapper.

        Asserts the start command's phase entry-point exists and forwards
        to the helper. The phase delegates so the start command can swap
        in a no-op for tests that patch the wrapper.
        """
        from cli.main import _phase_grant_post_hydrate_daemon_read

        with patch("cli.main._grant_post_hydrate_daemon_read") as mock_helper:
            _phase_grant_post_hydrate_daemon_read(str(tmp_path), "claude-sandbox")
            mock_helper.assert_called_once_with(str(tmp_path), "claude-sandbox")

    def test_daemon_read_direct_files_includes_compose_inputs_and_build_context(self) -> None:
        """DAEMON_READ_DIRECT_FILES is the canonical source-of-truth for the
        post-hydrate setfacl pass on dev-created files the daemon reads in
        place (no helper-cp ownership transfer). Spans two sub-categories:

        1. **Compose YAML inputs** consumed via ``docker compose -f``.
           Must match ``_build_compose_files``'s output: ``compose.yml``
           unconditionally + conditional ``db-postgres.yml`` /
           ``mcp-firecrawl.yml`` extras.
        2. **Build context** consumed by buildkit during
           ``docker compose up --build``: rendered/copied Dockerfiles
           + their local-COPY sources (e.g. ``admin/entrypoint.sh``).
           Must match every ``COPY <src>`` (without ``--from=<stage>``)
           in any Dockerfile under ``src/templates/docker/``.

        If a new compose ``--file`` path is added to
        ``_build_compose_files``, OR a new local-COPY source is added to
        any Dockerfile, this list must be extended in lockstep.
        """
        from cli.main import DAEMON_READ_DIRECT_FILES

        flat = {(parent, fname) for parent, files in DAEMON_READ_DIRECT_FILES for fname in files}

        # Compose YAML inputs.
        assert ("docker", "compose.yml") in flat, (
            "compose.yml is the canonical daemon-read direct file; "
            "removing it re-breaks `compose -f compose.yml up`"
        )
        assert ("docker/extras", "db-postgres.yml") in flat
        assert ("docker/extras", "mcp-firecrawl.yml") in flat

        # Build context — Dockerfiles. Empirical symptom of missing any
        # of these: `target <svc>: failed to solve: the Dockerfile
        # cannot be empty` (buildkit treats an unreadable Dockerfile
        # as empty). All four service Dockerfiles must be present.
        assert ("docker/core", "Dockerfile.core") in flat
        assert ("docker/admin", "Dockerfile.admin") in flat
        assert ("docker/coredns", "Dockerfile.coredns") in flat
        assert ("docker/extras", "Dockerfile.mcp-firecrawl") in flat

        # Build context — local-COPY sources. admin/fwd.go is COPY'd by
        # Dockerfile.admin during the build (the admin image is a static
        # Go forwarder; nothing is bind-mounted at runtime). It is
        # dev-created by hydrate's static _copy_file and needs explicit
        # daemon-read on the host.
        assert ("docker/admin", "fwd.go") in flat, (
            "admin/fwd.go is COPY'd into the admin image during build; "
            "without daemon read, buildkit's COPY step fails"
        )

    def test_phase_ipc_setup_not_in_start(self) -> None:
        """_phase_ipc_setup function no longer exists in cli.main."""
        import cli.main

        assert not hasattr(cli.main, "_phase_ipc_setup")

    def test_acl_grant_plan_includes_secrets(self, tmp_path: Path) -> None:
        """_acl_grant_plan includes at least one entry targeting secrets/ directory."""
        from cli.main import _acl_grant_plan

        plan = _acl_grant_plan(str(tmp_path), "sandbox")
        secrets_entries = [a.description for a in plan if "secrets" in a.description]
        assert len(secrets_entries) >= 1


class TestStartInstanceNameMatchesRegistry:
    """Operator-edited sandbox.toml guard: instance.name should match the registry key.

    Post-change-5, COMPOSE_PROJECT_NAME is derived from the typer ``inst`` arg
    (per Group 7's <dev>-<inst> prefix), not from ``config.instance.name``, so
    mismatch is no longer a correctness hazard. It remains a UX hazard
    (operator-facing displays surface ``instance.name``); start emits a warning.
    """

    def test_divergent_name_emits_warning(self, runner: CliRunner) -> None:
        """instance.name differs from the registry key → warning emitted."""
        inst = "myproject"
        instance_dir = _register_instance(inst)
        # Overwrite sandbox.toml with renamed instance.name
        (instance_dir / "sandbox.toml").write_bytes(RENAMED_TOML_CONTENT)
        _write_ipam(inst, 0)

        from cli.main import app

        with (
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main.is_backup_lock_held", return_value=False),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="pass"),
            patch("cli.main._phase_hydrate"),
            patch("cli.main._phase_acl_grant"),
            patch("cli.main._phase_helper_cp_chown_ro_files"),
            patch("cli.main._phase_workspace_shared_group"),
            patch("cli.main._phase_helper_mkdir_chown_cache_log"),
            patch("cli.main._phase_grant_post_hydrate_daemon_read"),
            patch("cli.main._phase_compose_up"),
            patch("cli.main._build_attach_argv", return_value=["/bin/true"]),
            patch(
                "cli.main.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["/bin/true"], returncode=0),
            ),
            patch("cli.main._release_lock"),
        ):
            result = runner.invoke(app, ["start", inst])
            assert result.exit_code == 0
            assert "instance.name" in result.output
            assert "renamed-instance" in result.output

    def test_matching_name_no_warning(self, runner: CliRunner) -> None:
        """instance.name matches the registry key → no warning."""
        inst = "myproject"
        _register_instance(inst)
        _write_ipam(inst, 0)

        from cli.main import app

        with (
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main.is_backup_lock_held", return_value=False),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="pass"),
            patch("cli.main._phase_hydrate"),
            patch("cli.main._phase_acl_grant"),
            patch("cli.main._phase_helper_cp_chown_ro_files"),
            patch("cli.main._phase_workspace_shared_group"),
            patch("cli.main._phase_helper_mkdir_chown_cache_log"),
            patch("cli.main._phase_grant_post_hydrate_daemon_read"),
            patch("cli.main._phase_compose_up"),
            patch("cli.main._build_attach_argv", return_value=["/bin/true"]),
            patch(
                "cli.main.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["/bin/true"], returncode=0),
            ),
            patch("cli.main._release_lock"),
        ):
            result = runner.invoke(app, ["start", inst])
            assert result.exit_code == 0
            assert "instance.name" not in result.output


# ── sandbox stop ─────────────────────────────────────────────────────────────


class TestStopWarm:
    """Task 8.2: sandbox stop — warm instance."""

    def test_stop_warm_instance_composes_down_and_revokes_acl(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app

        with (
            patch("cli.main._warm_check", return_value=True),
            patch("cli.main._compose_down") as mock_down,
            patch("cli.main._revoke_acls") as mock_revoke,
            patch("cli.main._phase_stop_unlink_consumer_files", return_value=[]),
        ):
            result = runner.invoke(app, ["stop", inst])
            assert result.exit_code == 0
            mock_down.assert_called_once()
            # Verify -v flag NOT passed (plain stop)
            down_args = mock_down.call_args
            assert down_args[1].get("volumes", False) is False or "-v" not in str(down_args)
            mock_revoke.assert_called_once()


class TestStopCold:
    """Task 8.2: sandbox stop — cold instance."""

    def test_stop_cold_instance_warns_and_exits(self, runner: CliRunner) -> None:
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app

        with (
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._compose_down") as mock_down,
        ):
            result = runner.invoke(app, ["stop", inst])
            assert result.exit_code == 0
            assert "not running" in result.output.lower()
            mock_down.assert_not_called()


class TestStopClean:
    """Task 8.2: sandbox stop --clean removes volumes."""

    def test_stop_clean_passes_volume_flag(self, runner: CliRunner) -> None:
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app

        with (
            patch("cli.main._warm_check", return_value=True),
            patch("cli.main._compose_down") as mock_down,
            patch("cli.main._revoke_acls"),
            patch("cli.main._phase_stop_unlink_consumer_files", return_value=[]),
        ):
            result = runner.invoke(app, ["stop", inst, "--clean"])
            assert result.exit_code == 0
            mock_down.assert_called_once()
            down_call = mock_down.call_args
            assert down_call[1].get("volumes") is True


# ── sandbox attach ───────────────────────────────────────────────────────────


class TestAttachWarm:
    """Task 8.2: sandbox attach — warm pass."""

    def test_attach_warm_instance_hands_over_terminal(self, runner: CliRunner) -> None:
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app

        with (
            patch("cli.main._warm_check", return_value=True),
            patch("cli.main._build_attach_argv", return_value=["/bin/true"]),
            patch(
                "cli.main.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["/bin/true"], returncode=0),
            ) as mock_handover,
        ):
            result = runner.invoke(app, ["attach", inst])
            assert result.exit_code == 0
            mock_handover.assert_called_once()


class TestAttachCold:
    """Task 8.2: sandbox attach — cold reject."""

    def test_attach_cold_instance_rejects(self, runner: CliRunner) -> None:
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app

        with (
            patch("cli.main._warm_check", return_value=False),
        ):
            result = runner.invoke(app, ["attach", inst])
            assert result.exit_code == 1
            assert "not running" in result.output.lower()


# ── sandbox destroy ──────────────────────────────────────────────────────────


class TestDestroyConfirmation:
    """Task 8.2: sandbox destroy — confirmation accepted/rejected."""

    def test_destroy_accepted_performs_full_teardown(self, runner: CliRunner) -> None:
        inst = "myproject"
        _register_instance(inst)
        _write_ipam(inst, 0)

        from cli.main import app

        with (
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._compose_down") as mock_down,
            patch("cli.main._revoke_acls"),
            patch("cli.main._phase_stop_unlink_consumer_files", return_value=[]),
            patch("cli.main._release_lock"),
            patch("shutil.rmtree") as mock_rmtree,
        ):
            # User types correct name to confirm
            result = runner.invoke(
                app,
                ["destroy", inst, "--backup-workspaces=none"],
                input="myproject\n",
            )
            assert result.exit_code == 0
            # Phase order: D3 compose down (volumes=False) + D5 compose down -v.
            assert mock_down.call_count == 2
            # rmtree called for instance dir (D7) + each workspace tree (D8).
            assert mock_rmtree.call_count >= 2

    def test_destroy_rejected_aborts_silently(self, runner: CliRunner) -> None:
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app

        with patch("shutil.rmtree") as mock_rmtree:
            result = runner.invoke(app, ["destroy", inst], input="wrong-name\n")
            assert result.exit_code == 0
            mock_rmtree.assert_not_called()

    def test_destroy_force_bypasses_confirmation(self, runner: CliRunner) -> None:
        inst = "myproject"
        _register_instance(inst)
        _write_ipam(inst, 0)

        from cli.main import app

        with (
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._compose_down"),
            patch("cli.main._revoke_acls"),
            patch("cli.main._phase_stop_unlink_consumer_files", return_value=[]),
            patch("cli.main._release_lock"),
            patch("shutil.rmtree") as mock_rmtree,
        ):
            result = runner.invoke(app, ["destroy", inst, "--force", "--backup-workspaces=none"])
            assert result.exit_code == 0
            # D7 instance dir + D8 per-workspace tree(s).
            assert mock_rmtree.call_count >= 2


class TestDestroyPrefixGuard:
    """Task 8.2: sandbox destroy — prefix guard triggered."""

    def test_destroy_rejects_path_outside_instances_root(self, runner: CliRunner) -> None:
        from cli.main import app
        from core.registry import InstanceRegistry

        _seed_registry(_user_home())
        # Register an instance whose dir lives OUTSIDE <user_home>/instances/.
        bad_path = str(_user_home() / "somewhere_else" / "evil")
        InstanceRegistry().register("evil", bad_path)
        with patch("shutil.rmtree") as mock_rmtree:
            result = runner.invoke(app, ["destroy", "evil", "--force"])
            assert result.exit_code == 1
            mock_rmtree.assert_not_called()


class TestDestroyIPAMAndRegistryCleanup:
    """Task 8.2: IPAM+registry cleared after destroy."""

    def test_destroy_clears_ipam_and_registry(self, runner: CliRunner) -> None:
        inst = "myproject"
        _register_instance(inst)
        _write_ipam(inst, 5)

        from cli.main import app

        with (
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._compose_down"),
            patch("cli.main._revoke_acls"),
            patch("cli.main._phase_stop_unlink_consumer_files", return_value=[]),
            patch("cli.main._release_lock"),
            patch("shutil.rmtree"),
        ):
            result = runner.invoke(app, ["destroy", inst, "--force", "--backup-workspaces=none"])
            assert result.exit_code == 0

            # IPAM and registry both live under the per-user home in change-5.
            ipam_data = json.loads((_user_home() / "state" / "ipam.json").read_text())
            assert inst not in ipam_data

            reg_data = json.loads((_user_home() / "state" / "instances.json").read_text())
            assert inst not in reg_data


class TestDestroyRmtree:
    """Task 8.2: shutil.rmtree called on instance_dir."""

    def test_destroy_removes_instance_directory(self, runner: CliRunner) -> None:
        inst = "myproject"
        _register_instance(inst)
        _write_ipam(inst, 0)

        from cli.main import app

        with (
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._compose_down"),
            patch("cli.main._revoke_acls"),
            patch("cli.main._phase_stop_unlink_consumer_files", return_value=[]),
            patch("cli.main._release_lock"),
            patch("shutil.rmtree") as mock_rmtree,
        ):
            runner.invoke(app, ["destroy", inst, "--force", "--backup-workspaces=none"])
            expected_dir = str(_user_home() / "instances" / inst)
            # D7 rmtrees the instance dir; D8 rmtrees each workspace tree.
            calls = [c.args[0] for c in mock_rmtree.call_args_list]
            assert expected_dir in calls


# ── Helper function unit tests (coverage) ────────────────────────────────────


class TestResolveHelpers:
    """Direct tests for the surviving cli.main resolution helpers."""

    def test_lookup_instance_or_exit_returns_dir_for_registered_instance(self) -> None:
        from cli.main import _lookup_instance_or_exit

        _register_instance("foo")
        instance_dir = _lookup_instance_or_exit("foo")
        assert instance_dir.endswith("/instances/foo")

    def test_lookup_instance_or_exit_exits_when_missing(self) -> None:
        from cli.main import _lookup_instance_or_exit

        _seed_registry(_user_home())
        with pytest.raises(typer.Exit) as exc_info:
            _lookup_instance_or_exit("does-not-exist")
        assert exc_info.value.exit_code == 1

    def test_load_config(self) -> None:
        from cli.main import _load_config

        instance_dir = _register_instance("myproject")
        config = _load_config(str(instance_dir))
        assert config.instance.name == "myproject"


@pytest.mark.no_warm_mock
class TestWarmCheckDirect:
    """Direct tests for _warm_check — delegates to _container_status (D-3).

    Marked ``no_warm_mock`` so the cli/conftest.py autouse fixture's global
    patch of ``cli.main._warm_check`` is skipped — these tests exercise the
    real function.
    """

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

    def test_acquire_contention(self, isolated_sandbox_ai_home: Path) -> None:
        import fcntl as _fcntl

        from cli.main import _acquire_state_lock
        from core.host_config import state_lock_path

        lock_path = state_lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        held_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        _fcntl.flock(held_fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        try:
            with pytest.raises(BlockingIOError):
                _acquire_state_lock(str(isolated_sandbox_ai_home))
        finally:
            _fcntl.flock(held_fd, _fcntl.LOCK_UN)
            os.close(held_fd)

    def test_release_lock_handles_bad_fd(self) -> None:
        from cli.main import _release_lock

        # Should not raise even with invalid fd
        _release_lock(999999)


class TestPhaseIPAMDirect:
    """Direct test for _phase_ipam."""

    def test_phase_ipam_allocates(self) -> None:
        from cli.main import _phase_ipam

        idx = _phase_ipam("test-instance")
        assert idx == 0


class TestPhaseCredentialsDirect:
    """Direct test for _phase_credentials."""

    def test_phase_credentials_writes_htpasswd(self, tmp_path: Path) -> None:
        from cli.main import _phase_credentials

        proxy_dir = tmp_path / "config" / "proxy"
        proxy_dir.mkdir(parents=True)
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir(parents=True)
        # Stub keys for the os.stat reconciliation path.
        (secrets_dir / "ipc_ssh_key").write_text("")
        (secrets_dir / "ipc_known_hosts").write_text("")

        with (
            patch("cli.main.write_htpasswd") as mock_write,
            patch("cli.main.generate_ssh_keypair"),
        ):
            password = _phase_credentials(str(tmp_path), core_ipc_ip="10.100.6.3")
            assert len(password) > 0
            mock_write.assert_called_once()
            line = mock_write.call_args[0][1]
            assert line.startswith("proxyuser:$2b$")

    def test_phase_credentials_reconciles_ipc_secret_ownership(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """admin-reframe Fix B' (3.G.11): on every start, ``_phase_credentials``
        reconciles ``ipc_ssh_key`` and ``ipc_known_hosts`` to operator-owned
        mode 0600. Bind-mount inode stability requires in-place mutation
        (os.chown / os.chmod), never os.replace/mv/install.

        Covers both reconciliation branches:
            * uid mismatch -> os.chown
            * mode mismatch -> os.chmod
        """
        from cli.main import _phase_credentials

        (tmp_path / "config" / "proxy").mkdir(parents=True)
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        ssh_key = secrets_dir / "ipc_ssh_key"
        kh = secrets_dir / "ipc_known_hosts"
        ssh_key.write_text("k")
        kh.write_text("h")
        # Simulate legacy mode 0640 (would be wrong post-Fix-B').
        os.chmod(ssh_key, 0o640)
        os.chmod(kh, 0o640)

        chown_calls: list[tuple[str, int, int]] = []
        chmod_calls: list[tuple[str, int]] = []
        real_chmod = os.chmod

        # Pretend the on-disk uid is a non-operator uid so the chown branch
        # fires. We can't actually chown to a foreign uid without privilege,
        # so we monkeypatch os.stat to return a fake st_uid for these inodes.
        operator_uid = os.geteuid()
        legacy_uid = operator_uid + 12345  # any uid != operator_uid

        real_stat = os.stat

        def _fake_stat(path: str | os.PathLike[str], *args: object, **kwargs: object) -> os.stat_result:
            st = real_stat(path)
            if str(path) in {str(ssh_key), str(kh)}:
                # Construct a stat_result with an overridden st_uid via tuple form
                return os.stat_result(
                    (st.st_mode, st.st_ino, st.st_dev, st.st_nlink, legacy_uid, st.st_gid,
                     st.st_size, st.st_atime, st.st_mtime, st.st_ctime)
                )
            return st

        def _record_chown(path: str, uid: int, gid: int) -> None:
            # Don't actually chown (no privilege); record only.
            chown_calls.append((str(path), uid, gid))

        def _record_chmod(path: str, mode: int) -> None:
            chmod_calls.append((str(path), mode))
            real_chmod(path, mode)

        monkeypatch.setattr("cli.main.os.stat", _fake_stat)
        monkeypatch.setattr("cli.main.os.chown", _record_chown)
        monkeypatch.setattr("cli.main.os.chmod", _record_chmod)

        with (
            patch("cli.main.write_htpasswd"),
            patch("cli.main.generate_ssh_keypair"),
        ):
            _phase_credentials(str(tmp_path), core_ipc_ip="10.100.6.3")

        # uid mismatch reconciliation: both files chown'd to operator.
        chown_targets = {p: (uid, gid) for p, uid, gid in chown_calls}
        assert chown_targets[str(ssh_key)] == (operator_uid, os.getegid())
        assert chown_targets[str(kh)] == (operator_uid, os.getegid())

        # Mode-bit reconciliation: both files chmod'd to 0o600.
        chmod_targets = {p: m for p, m in chmod_calls}
        assert chmod_targets[str(ssh_key)] == 0o600
        assert chmod_targets[str(kh)] == 0o600
        assert (ssh_key.stat().st_mode & 0o7777) == 0o600
        assert (kh.stat().st_mode & 0o7777) == 0o600

    def test_phase_credentials_reconciliation_is_noop_when_correct(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When ipc secrets are already operator-owned mode 0600, the
        reconciliation step issues no chown/chmod (idempotent)."""
        from cli.main import _phase_credentials

        (tmp_path / "config" / "proxy").mkdir(parents=True)
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        ssh_key = secrets_dir / "ipc_ssh_key"
        kh = secrets_dir / "ipc_known_hosts"
        ssh_key.write_text("k")
        kh.write_text("h")
        os.chmod(ssh_key, 0o600)
        os.chmod(kh, 0o600)

        chown_calls: list[object] = []
        chmod_calls: list[object] = []
        monkeypatch.setattr(
            "cli.main.os.chown",
            lambda *a, **kw: chown_calls.append((a, kw)),
        )
        monkeypatch.setattr(
            "cli.main.os.chmod",
            lambda *a, **kw: chmod_calls.append((a, kw)),
        )

        with (
            patch("cli.main.write_htpasswd"),
            patch("cli.main.generate_ssh_keypair"),
        ):
            _phase_credentials(str(tmp_path), core_ipc_ip="10.100.6.3")

        assert chown_calls == [], "no chown when uid already matches operator"
        assert chmod_calls == [], "no chmod when mode already 0600"


class TestHelperCpChownRoFiles:
    """Section 8: helper-cp+chown phase replacing _phase_credential_ownership.

    Verifies the per-class consumer mapping (orchestrator-volumes Decision 1)
    is honored: dnsdist 953, coredns 65532, proxy 13, agent/human 1000;
    secrets land at 0600, ro config at 0640.
    """

    def test_plan_covers_all_per_class_consumers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cli.main import _helper_cp_chown_plan

        monkeypatch.setattr("cli.main.host_id_for_in_container", lambda n, u: 100000 + n)
        monkeypatch.setattr("cli.main.host_gid_for_in_container", lambda n, u: 200000 + n)
        plan = _helper_cp_chown_plan("/inst", "claude-sandbox")
        owners = {a.owner_uid: (str(a.parent), a.files, a.mode) for a in plan}
        # Each consumer uid is mapped via subuid resolver
        assert 100000 + 65532 in owners  # coredns
        assert 100000 + 953 in owners  # dnsdist
        assert 100000 + 13 in owners  # proxy
        assert 100000 + 1000 in owners  # agent/human/secrets
        # Modes per design table
        modes_by_uid: dict[int, set[int]] = {}
        for entry in plan:
            modes_by_uid.setdefault(entry.owner_uid, set()).add(entry.mode)
        assert 0o640 in modes_by_uid[100000 + 13]  # proxy ro
        assert 0o640 in modes_by_uid[100000 + 65532]  # coredns ro
        # Secrets at 0600 for the 1000-consumer
        assert 0o600 in modes_by_uid[100000 + 1000]
        # gid pairs with the consumer's host subgid (D6: literal-0 was removed).
        for entry in plan:
            consumer_n = entry.owner_uid - 100000
            assert entry.owner_gid == 200000 + consumer_n

    def test_plan_includes_server_side_ipc_secrets_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Server-side IPC SSH secrets (authorized_keys, ipc_host_key) land at 0600.

        admin-reframe Fix B' (design D3): the client-side files
        ``ipc_ssh_key`` and ``ipc_known_hosts`` are dropped from
        ``RO_FILE_RECIPES`` and remain dev-owned mode 0600 (read directly
        by the host's ssh client during ``sandbox attach``). They must
        NOT appear in the helper-cp plan.
        """
        from cli.main import _helper_cp_chown_plan

        monkeypatch.setattr("cli.main.host_id_for_in_container", lambda n, u: 1000 if n == 1000 else 0)
        monkeypatch.setattr("cli.main.host_gid_for_in_container", lambda n, u: 2000 if n == 1000 else 0)
        plan = _helper_cp_chown_plan("/inst", "claude-sandbox")
        secrets_files: set[str] = set()
        for action in plan:
            if str(action.parent).endswith("/secrets"):
                assert action.owner_uid == 1000
                # gid is the consumer's host subgid (D6), not literal 0.
                assert action.owner_gid == 2000
                assert action.mode == 0o600
                secrets_files.update(action.files)
        assert {"ipc_host_key", "authorized_keys"} <= secrets_files
        assert "ipc_ssh_key" not in secrets_files, "Fix B': ipc_ssh_key must not be in helper-cp plan"
        assert "ipc_known_hosts" not in secrets_files, "Fix B': ipc_known_hosts must not be in helper-cp plan"

    def test_plan_uid_and_gid_both_in_subid_range(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Regression guard for D6: every (uid, gid) pair from `_helper_cp_chown_plan`
        must round-trip through the inverse resolvers without raising. Catches
        any reintroduction of the literal-0 gid pattern (which lies outside the
        subgid range and would raise SubgidOutOfRangeError on every helper-cp).
        """
        from cli.main import _helper_cp_chown_plan
        from core.host_config import in_container_gid_for_host_gid, in_container_uid_for_host_uid

        subuid = tmp_path / "subuid"
        subuid.write_text("claude-sandbox:165536:65536\n")
        monkeypatch.setattr("core.host_config._SUBUID_PATH", subuid)
        subgid = tmp_path / "subgid"
        subgid.write_text("claude-sandbox:165536:65536\n")
        monkeypatch.setattr("core.host_config._SUBGID_PATH", subgid)

        plan = _helper_cp_chown_plan("/inst", "claude-sandbox")
        assert plan, "plan should be non-empty"
        for entry in plan:
            # Typed-field reads per design Decision 1's carve-out for
            # numeric/permission-bit assertions.
            in_uid = in_container_uid_for_host_uid(entry.owner_uid, "claude-sandbox")
            in_gid = in_container_gid_for_host_gid(entry.owner_gid, "claude-sandbox")
            assert in_uid >= 1
            assert in_gid >= 1

    def test_phase_invokes_helper_per_group(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cli.main import _phase_helper_cp_chown_ro_files
        from core.host_config import MachinectlAuth

        monkeypatch.setattr("cli.main.host_id_for_in_container", lambda n, u: 100000 + n)
        monkeypatch.setattr("cli.main.host_gid_for_in_container", lambda n, u: 200000 + n)
        invocations: list[tuple[str, ...]] = []

        def _fake(
            host_user: str,
            parent: str,
            files: typing.Iterable[str],
            uid: int,
            gid: int,
            mode: int,
            auth: object,
            **kw: object,
        ) -> None:
            invocations.append((parent, *files))

        monkeypatch.setattr("core.actions.helper_cp.helper_chown_files", _fake)
        _phase_helper_cp_chown_ro_files("/inst", "claude-sandbox", MachinectlAuth.SUDO)
        # One invocation per (RO_FILE_RECIPES + EXEC_FILE_RECIPES + RW_FILE_RECIPES)
        # entry. Per admin-reframe (Fix B'), the RO table now has 5 entries
        # (coredns, dnsdist, proxy, core, secrets) — the legacy
        # ipc_ssh_key/ipc_known_hosts secret entries no longer participate in
        # the helper-cp recipe; they remain dev-owned mode 0600 and the host
        # ssh client reads them in place. So 5 RO + 1 EXEC + 1 RW = 7 groups.
        assert len(invocations) == 7

    def test_phase_propagates_helper_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cli.main import _phase_helper_cp_chown_ro_files
        from core.exceptions import SandboxExecutionError
        from core.host_config import MachinectlAuth

        monkeypatch.setattr("cli.main.host_id_for_in_container", lambda n, u: 100000 + n)
        monkeypatch.setattr("cli.main.host_gid_for_in_container", lambda n, u: 200000 + n)

        def _raise(*a: object, **kw: object) -> None:
            raise SandboxExecutionError("helper failed")

        monkeypatch.setattr("core.actions.helper_cp.helper_chown_files", _raise)
        with pytest.raises(SandboxExecutionError, match="helper failed"):
            _phase_helper_cp_chown_ro_files("/inst", "u", MachinectlAuth.SUDO)


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
                    "host_uid": "1000",
                },
                "workspaces": {"main": {"bootstrap_mode": "empty", "path": "/home/user/test"}},
            }
        )
        host = HostSettings(docker_unprivileged_user="claude-sandbox")

        with (
            patch("cli.main.build_jinja_context", return_value={}) as mock_ctx,
            patch("cli.main.render_templates") as mock_render,
        ):
            _phase_hydrate(mock_config, 0, "pass", "/inst", host)
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
                    "host_uid": "1000",
                },
                "workspaces": {"main": {"bootstrap_mode": "empty", "path": "/x"}},
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
                    "host_uid": "1000",
                },
                "workspaces": {"main": {"bootstrap_mode": "empty", "path": "/x"}},
                "components": {"mcp_firecrawl": True, "mcp_puppeteer": False},
                "components_db_postgres": {"enabled": True},
            }
        )
        files = _build_compose_files("/inst", config)
        assert len(files) == 6  # base + postgres + firecrawl


class TestPhaseComposeUpDirect:
    """Direct test for _phase_compose_up (routes through core.dispatch.invoke)."""

    def test_compose_up_crosses_boundary_via_dispatch(
        self, isolated_sandbox_ai_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json as _json
        import subprocess
        from typing import cast

        from cli.main import _phase_compose_up
        from core.host_config import MachinectlAuth

        inst_dir = isolated_sandbox_ai_home / "instances" / "t"
        (inst_dir / "docker").mkdir(parents=True, exist_ok=True)
        (inst_dir / "docker" / "compose.yml").write_text("services: {}\n")
        (inst_dir / ".sandbox.env").write_text("")
        (inst_dir / "sandbox.toml").write_text(
            '[instance]\nname = "t"\nhost_uid = "1000"\n\n'
            '[workspaces.main]\nbootstrap_mode = "empty"\n'
            f'path = "{isolated_sandbox_ai_home}/workspaces/t/main"\n'
            "\n[components.db_postgres]\nenabled = false\n"
        )
        state = isolated_sandbox_ai_home / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "instances.json").write_text(
            _json.dumps({"t": {"instance_dir": str(inst_dir), "created_at": "2026-01-01T00:00:00Z"}})
        )

        from core.host_config import HostConfig

        class _FakeHostSettings:
            docker_unprivileged_user = "sandbox"
            machinectl_authentication = MachinectlAuth.SUDO

        class _FakeHostConfig:
            host = _FakeHostSettings()

        captured: dict[str, object] = {}

        def fake_run(
            self: object, cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("core.dispatch.Executor.run", fake_run)
        _phase_compose_up("t", str(inst_dir), cast("HostConfig", _FakeHostConfig()))
        cmd = cast("list[str]", captured["cmd"])
        assert "machinectl" in cmd
        assert cmd[-1].startswith("/usr/local/libexec/sandbox-ai/dispatch compose-up t")


class TestBuildAttachArgv:
    """Structural tests for `_build_attach_argv` (admin-reframe D1 / cli-attach spec).

    The fixture-diff test (admin-reframe task 3.H.13) lands separately; these
    structural tests cover the canonical-shape requirement only.
    """

    def _invoke(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> list[str]:
        from cli.main import _build_attach_argv
        from core.host_config import HostConfig, HostSettings

        # Stable IPAM ledger so peek_next_slot returns a deterministic
        # base_index (the IP set is derived from base_index; structural
        # assertions only need it to be present in the argv).
        ledger_path = tmp_path / "ipam.json"
        ledger_path.write_text('{"myproj": 0}\n')
        monkeypatch.setattr("core.ipam._default_ledger_path", lambda: str(ledger_path))

        # Redirect SANDBOX_AI_HOME so sessions log dir lands under tmp.
        home = tmp_path / "home"
        (home / "instances" / "myproj" / "secrets").mkdir(parents=True)
        monkeypatch.setenv("SANDBOX_AI_HOME", str(home))

        cfg = HostConfig(host=HostSettings(docker_unprivileged_user="sandbox"))
        return _build_attach_argv("myproj", "main", cfg)

    def test_argv_starts_with_tlog_rec_writer_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        argv = self._invoke(monkeypatch, tmp_path)
        assert argv[0] == "tlog-rec"
        assert "--writer=file" in argv
        assert any(a.startswith("--file-path=") for a in argv)
        assert "--" in argv

    def test_argv_invokes_ssh_with_ipc_credentials(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        argv = self._invoke(monkeypatch, tmp_path)
        # ssh -i <secrets>/ipc_ssh_key
        assert "ssh" in argv
        i_idx = argv.index("-i")
        assert argv[i_idx + 1].endswith("/secrets/ipc_ssh_key")
        joined = " ".join(argv)
        assert "UserKnownHostsFile=" in joined
        assert "ipc_known_hosts" in joined
        assert "StrictHostKeyChecking=yes" in joined

    def test_argv_proxy_command_uses_pipe_cmd_not_sudo(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        argv = self._invoke(monkeypatch, tmp_path)
        proxy = next(a for a in argv if a.startswith("ProxyCommand="))
        # pipe_cmd shape: systemd-run -q --pipe --uid=<sbuser>
        assert "systemd-run" in proxy
        assert "--pipe" in proxy
        assert "--uid=sandbox" in proxy
        # MUST NOT be prefixed with sudo (admin-reframe D2 — pipe_cmd is
        # auth-mode-independent and uses polkit's manage-units action).
        assert "sudo" not in proxy
        # /fwd into the admin container.
        assert "/fwd" in proxy
        assert "myproj-admin-1" in proxy
        assert ":9999" in proxy

    def test_argv_target_port_user_and_remote_command(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        argv = self._invoke(monkeypatch, tmp_path)
        assert "-p" in argv
        p_idx = argv.index("-p")
        assert argv[p_idx + 1] == "9999"
        assert "-t" in argv
        # agent@<core_ipc_ip>
        assert any(a.startswith("agent@") for a in argv)
        # Remote command is the final positional, per cli-attach D9.
        assert argv[-1] == "cd /workspaces/main && exec bash -l"


class TestComposeDownDirect:
    """Direct test for _compose_down — routes through core.dispatch.invoke.

    `_compose_down` raised on failure before the dispatcher refactor (no
    branch — `sentinel=True` Executor.run with no try/except); it still does,
    so it uses `invoke()` (raise-on-failure), not `probe()`. The stop-vs-
    destroy distinction is the optional literal `--volumes` typed arg.
    """

    def _config(self) -> InstanceConfig:
        from core.hydration import InstanceConfig

        return InstanceConfig.model_validate(
            {
                "instance": {
                    "name": "t",
                    "host_uid": "1000",
                },
                "workspaces": {"main": {"bootstrap_mode": "empty", "path": "/x"}},
            }
        )

    def test_compose_down_plain(self) -> None:
        from cli.main import _compose_down
        from core.host_config import MachinectlAuth

        with patch("cli.main.dispatch.invoke") as mock_invoke:
            _compose_down("/inst", "myproj", "sandbox", self._config(), volumes=False)
            (op, args, host_config), kwargs = mock_invoke.call_args
            assert op == "compose-down"
            assert args == ["t"]
            assert "--volumes" not in args
            assert host_config.host.docker_unprivileged_user == "sandbox"
            assert host_config.host.machinectl_authentication == MachinectlAuth.SUDO

    def test_compose_down_volumes(self) -> None:
        from cli.main import _compose_down
        from core.host_config import MachinectlAuth

        with patch("cli.main.dispatch.invoke") as mock_invoke:
            _compose_down(
                "/inst", "myproj", "sandbox", self._config(), volumes=True, auth=MachinectlAuth.POLKIT
            )
            (op, args, host_config), kwargs = mock_invoke.call_args
            assert op == "compose-down"
            assert args == ["t", "--volumes"]
            assert host_config.host.machinectl_authentication == MachinectlAuth.POLKIT

    def test_compose_down_raises_on_failure(self) -> None:
        """`invoke()` (not `probe()`) — a non-zero exit propagates as
        SandboxExecutionError exactly as the prior sentinel path did."""
        from cli.main import _compose_down
        from core.exceptions import SandboxExecutionError

        with (
            patch("cli.main.dispatch.invoke", side_effect=SandboxExecutionError("boom")),
            pytest.raises(SandboxExecutionError),
        ):
            _compose_down("/inst", "myproj", "sandbox", self._config(), volumes=False)


class TestRevokeACLsDirect:
    """Task 4.3: Fault-isolated _revoke_acls — partial failure, all targets, warnings.

    Post-cluster-3 (Acl Revoke Plan Excludes Persistent Grants): the
    .sandbox.env named-ACL is now `granted-once, persistent` (Environment
    File Read ACL) and is excluded from revoke. Plan entries when
    workspace_paths is provided: instance root + docker/ + config/ +
    secrets/ dir + secrets/ default ACL + workspace (effective + default
    named-entry) = 7.
    """

    def test_revoke_acls_calls_setfacl_for_all_plan_entries(self) -> None:
        from cli.main import _revoke_acls

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")
            warnings = _revoke_acls("/inst", "sandbox", ["/home/user/proj"])
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
            warnings = _revoke_acls("/inst", "sandbox", ["/home/user/proj"])

        assert call_count == 7
        assert len(warnings) == 1
        assert "No such file" in warnings[0]

    def test_oserror_collected_as_warning(self) -> None:
        """WHEN setfacl raises OSError, THEN collected as warning, not raised."""
        from cli.main import _revoke_acls

        with patch("subprocess.run", side_effect=OSError("setfacl not found")):
            warnings = _revoke_acls("/inst", "sandbox", ["/home/user/proj"])

        assert len(warnings) == 7
        assert all("setfacl not found" in w for w in warnings)


class TestInitScaffoldDirect:
    """Task 9.1: init command creates full instance (migrated from _scaffold_instance)."""

    def test_init_creates_full_instance(self, runner: CliRunner) -> None:
        from cli.main import app
        from core.hydration import InstanceConfig

        project_dir = "/home/user/newproject"

        mock_config = InstanceConfig.model_validate(
            {
                "instance": {
                    "name": "newproject",
                    "host_uid": "1000",
                },
                "workspaces": {"main": {"bootstrap_mode": "empty", "path": project_dir}},
            }
        )

        with (
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
            result = runner.invoke(app, ["init", "newproject"])
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

    def test_init_with_copy_invokes_preflight_and_rsync(
        self, runner: CliRunner, mock_sandbox_ai_home: Path, tmp_path: Path
    ) -> None:
        """``--copy NAME=PATH`` triggers _preflight_workspace_source + copy_workspace."""
        from cli.main import app
        from core.hydration import InstanceConfig

        home = mock_sandbox_ai_home
        src = tmp_path / "src"
        src.mkdir()
        (src / "f").write_text("hi")

        mock_config = InstanceConfig.model_validate(
            {
                "instance": {"name": "newproject", "host_uid": "1000"},
                "workspaces": {
                    "api": {
                        "bootstrap_mode": "copy",
                        "source": str(src),
                        "path": str(home / "workspaces" / "newproject" / "api"),
                    },
                },
            }
        )

        with (
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
            patch("cli.main._preflight_workspace_source") as mock_preflight,
            patch("cli.main.copy_workspace") as mock_copy,
        ):
            result = runner.invoke(app, ["init", "newproject", "--copy", f"api={src}"])
            assert result.exit_code == 0, result.output
            mock_preflight.assert_called_once()
            mock_copy.assert_called_once()
            assert mock_copy.call_args.args[0] == str(src)


# ── Edge case tests for remaining coverage ───────────────────────────────────


class TestStopNoInstance:
    """Cover stop with unregistered instance."""

    def test_stop_no_instance_exits(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        result = runner.invoke(app, ["stop", "missing"])
        assert result.exit_code == 1
        assert "no sandbox" in result.output.lower()


class TestAttachNoInstance:
    """Cover attach with unregistered instance."""

    def test_attach_no_instance_exits(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        result = runner.invoke(app, ["attach", "missing"])
        assert result.exit_code == 1
        assert "no sandbox" in result.output.lower()


class TestDestroyNoInstance:
    """Cover destroy with unregistered instance."""

    def test_destroy_no_instance_exits(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        result = runner.invoke(app, ["destroy", "missing", "--force"])
        assert result.exit_code == 1
        assert "no sandbox" in result.output.lower()


class TestDestroyPrefixGuardInternal:
    """Cover the internal prefix guard path (not mocked)."""

    def test_prefix_guard_rejects_bad_path(self, runner: CliRunner) -> None:

        from cli.main import app
        from core.registry import InstanceRegistry

        _seed_registry(_user_home())
        # Register an instance whose dir lives OUTSIDE <home>/instances/ — the
        # destroy prefix guard MUST reject it.
        bad_path = str(_user_home() / "somewhere_else" / "evil")
        InstanceRegistry().register("evil", bad_path)
        with patch("cli.main._load_config") as mock_load:
            mock_load.return_value.instance.name = "evil"
            mock_load.return_value.workspaces = {}
            result = runner.invoke(app, ["destroy", "evil", "--force"])
            assert result.exit_code == 1
            assert "prefix guard" in result.output.lower()


class TestInitFirecrawl:
    """Task 9.1: init command's firecrawl branch (migrated from _scaffold_instance)."""

    def test_init_with_firecrawl_includes_secret(self, runner: CliRunner) -> None:
        from cli.main import app
        from core.hydration import InstanceConfig

        project_dir = "/home/user/fc-project"

        mock_config = InstanceConfig.model_validate(
            {
                "instance": {
                    "name": "fc-project",
                    "host_uid": "1000",
                },
                "workspaces": {"main": {"bootstrap_mode": "empty", "path": project_dir}},
                "components": {"mcp_firecrawl": True, "mcp_puppeteer": False},
                "components_db_postgres": {"enabled": True},
            }
        )

        with (
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
            result = runner.invoke(app, ["init", "fc-project"])
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

    def test_displays_resolved_home(self, runner: CliRunner, isolated_sandbox_ai_home: Path) -> None:
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
        assert str(isolated_sandbox_ai_home) in result.output


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

    def test_init_creates_instance(self, runner: CliRunner) -> None:
        """init scaffolds a new instance successfully."""
        project_dir = "/home/user/newproject"

        from cli.main import app
        from core.hydration import InstanceConfig

        mock_config = InstanceConfig.model_validate(
            {
                "instance": {
                    "name": "newproject",
                    "host_uid": "1000",
                },
                "workspaces": {"main": {"bootstrap_mode": "empty", "path": project_dir}},
            }
        )

        with (
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
            result = runner.invoke(app, ["init", "newproject"])
            assert result.exit_code == 0, result.output


@pytest.mark.no_seed_registry
class TestInitPerUserTreeCreation:
    """init creates `<home>/{config,state}/` with mode 0700 (idempotent).

    Marked ``no_seed_registry`` so the cli/conftest.py autouse fixture
    does NOT pre-create ``<home>/state/`` with default umask permissions —
    these tests assert ``sandbox init`` builds the tree at mode 0700 from
    a truly empty home.
    """

    def _common_patches(
        self,
        home: Path,
        project_dir: str,
        mock_config: object,
    ) -> list[typing.Any]:
        return [
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
                    "host_uid": "1000",
                },
                "workspaces": {"main": {"bootstrap_mode": "empty", "path": project_dir}},
            }
        )

    def test_clean_install_creates_tree(
        self,
        runner: CliRunner,
        mock_sandbox_ai_home: Path,
        isolated_sandbox_ai_home: Path,
    ) -> None:
        """init on a clean host creates the full per-user tree (config/state/instances/workspaces)."""
        from cli.main import app

        project_dir = "/home/user/p"
        mock_config = self._make_mock_config(project_dir)
        with contextlib.ExitStack() as es:
            for p in self._common_patches(mock_sandbox_ai_home, project_dir, mock_config):
                es.enter_context(p)
            result = runner.invoke(app, ["init", "p"])
        assert result.exit_code == 0, result.output
        assert isolated_sandbox_ai_home.is_dir()
        assert (isolated_sandbox_ai_home / "config").is_dir()
        assert (isolated_sandbox_ai_home / "state").is_dir()
        assert (isolated_sandbox_ai_home / "instances").is_dir()
        assert (isolated_sandbox_ai_home / "workspaces").is_dir()
        assert stat.S_IMODE(isolated_sandbox_ai_home.stat().st_mode) == 0o700

    def test_idempotent_on_existing_tree(
        self,
        runner: CliRunner,
        mock_sandbox_ai_home: Path,
        isolated_sandbox_ai_home: Path,
    ) -> None:
        """Re-running init against an existing tree does not error."""
        from cli.main import app
        from core.host_config import ensure_per_user_state

        ensure_per_user_state(isolated_sandbox_ai_home)
        project_dir = "/home/user/p"
        mock_config = self._make_mock_config(project_dir)
        with contextlib.ExitStack() as es:
            for p in self._common_patches(mock_sandbox_ai_home, project_dir, mock_config):
                es.enter_context(p)
            result = runner.invoke(app, ["init", "p"])
        assert result.exit_code == 0, result.output

    def test_existing_registry_not_overwritten(
        self,
        runner: CliRunner,
        mock_sandbox_ai_home: Path,
        isolated_sandbox_ai_home: Path,
    ) -> None:
        """A pre-populated registry is preserved; init only ensures presence."""
        from cli.main import app
        from core.host_config import ensure_per_user_state

        ensure_per_user_state(isolated_sandbox_ai_home)
        registry = isolated_sandbox_ai_home / "state" / "instances.json"
        registry.write_text(
            json.dumps({"existing": {"instance_dir": "/x/sandboxes/existing", "created_at": "2026-01-01T00:00:00Z"}})
        )

        project_dir = "/home/user/p"
        mock_config = self._make_mock_config(project_dir)
        with contextlib.ExitStack() as es:
            for p in self._common_patches(mock_sandbox_ai_home, project_dir, mock_config):
                es.enter_context(p)
            result = runner.invoke(app, ["init", "p"])
        assert result.exit_code == 0, result.output
        data = json.loads(registry.read_text())
        # Pre-existing entry preserved
        assert "existing" in data
        # New entry added too, keyed by basename
        assert "p" in data

    def test_sandbox_ai_home_redirect(
        self,
        runner: CliRunner,
        mock_sandbox_ai_home: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Setting SANDBOX_AI_HOME redirects creation to that path."""
        from cli.main import app

        custom_home = tmp_path / "alt-home"
        monkeypatch.setenv("SANDBOX_AI_HOME", str(custom_home))
        project_dir = "/home/user/p"
        mock_config = self._make_mock_config(project_dir)
        with contextlib.ExitStack() as es:
            for p in self._common_patches(mock_sandbox_ai_home, project_dir, mock_config):
                es.enter_context(p)
            result = runner.invoke(app, ["init", "p"])
        assert result.exit_code == 0, result.output
        assert custom_home.is_dir()
        assert (custom_home / "config").is_dir()
        assert (custom_home / "state").is_dir()


class TestInitReInitRejection:
    """Task 3.1: sandbox init — re-init rejected."""

    def test_reinit_rejected(self, runner: CliRunner) -> None:
        """init errors when instance already exists."""
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app

        result = runner.invoke(app, ["init", "myproject"])
        assert result.exit_code == 1
        assert "already initialized" in result.output.lower() or "destroy" in result.output.lower()


class TestInitDryRun:
    """Task 3.1: sandbox init --dry-run previews without writing."""

    def test_init_dry_run_no_state(self, runner: CliRunner) -> None:
        """init --dry-run does not create any files or registry entries."""
        from cli.main import app

        with patch("cli.main._detect_git_config", return_value=("", "")):
            result = runner.invoke(app, ["init", "newproject", "--dry-run"])
            assert result.exit_code == 0

        # Registry should be unmodified — per-user state lives under SANDBOX_AI_HOME.
        registry_path = _user_home() / "state" / "instances.json"
        assert not registry_path.exists() or "newproject" not in json.loads(registry_path.read_text())


class TestInitDoctorPreFlightFailure:
    """Task 3.1: sandbox init — doctor pre-flight failure aborts."""

    def test_init_aborts_on_doctor_failure(self, runner: CliRunner) -> None:
        """init aborts when doctor pre-flight checks fail."""
        from cli.main import app
        from core.doctor import CheckResult

        failed_results = [CheckResult(status="fail", name="setfacl", detail="not found", remediation="install acl")]
        with (
            patch("cli.main.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "ok\n", "")),
            patch("cli.main.run_check_subset", return_value=failed_results),
            patch("cli.main.render_results"),
        ):
            result = runner.invoke(app, ["init", "newproject"])
            assert result.exit_code == 1

    def test_init_aborts_on_compose_project_name_collision(self, runner: CliRunner) -> None:
        """12.11: init pre-flight runs compose_project_name_collision; fails on collision."""
        from cli.main import app
        from core.doctor import CheckResult

        collision = CheckResult(
            status="fail",
            name="compose project name collision",
            detail="daemon project already exists",
            remediation="destroy the colliding instance",
            category="Privilege Boundary",
        )
        with (
            patch("cli.main.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "ok\n", "")),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main.check_compose_project_name_collision", return_value=collision),
            patch("cli.main.render_results"),
        ):
            result = runner.invoke(app, ["init", "newproject"])
            assert result.exit_code == 1

    def test_init_proceeds_on_compose_collision_pass(self, runner: CliRunner) -> None:
        """12.11: init pre-flight passes through when collision check passes."""
        from cli.main import app
        from core.doctor import CheckResult

        ok = CheckResult(
            status="pass",
            name="compose project name collision",
            detail="no collision",
            category="Privilege Boundary",
        )
        with (
            patch("cli.main.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "ok\n", "")),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main.check_compose_project_name_collision", return_value=ok),
        ):
            result = runner.invoke(app, ["init", "newinst"])
            # Init proceeds past pre-flight (may fail later for other reasons,
            # but collision check did not abort).
            assert "compose project name collision" not in result.output


class TestInitNonTTY:
    """Task 3.1: sandbox init in non-TTY environment."""

    def test_init_non_tty_completes(self, runner: CliRunner) -> None:
        """init completes in non-TTY mode (prompt_secrets skips)."""
        project_dir = "/home/user/newproject"

        from cli.main import app
        from core.hydration import InstanceConfig

        mock_config = InstanceConfig.model_validate(
            {
                "instance": {
                    "name": "newproject",
                    "host_uid": "1000",
                },
                "workspaces": {"main": {"bootstrap_mode": "empty", "path": project_dir}},
            }
        )

        with (
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
            result = runner.invoke(app, ["init", "newproject"])
            assert result.exit_code == 0


class TestRequirePerUserStateInitialized:
    """Lifecycle commands fail with canonical error when state tree is uninitialized."""

    @pytest.mark.parametrize("command", ["start", "stop", "attach", "destroy", "status"])
    def test_command_fails_when_uninitialized(
        self, runner: CliRunner, command: str, isolated_sandbox_ai_home: Path
    ) -> None:
        from cli.main import app

        # Ensure registry seed file is absent
        registry = isolated_sandbox_ai_home / "state" / "instances.json"
        if registry.exists():
            registry.unlink()

        args = [command, "any-inst"]
        if command == "destroy":
            args.append("--force")
        result = runner.invoke(app, args)
        assert result.exit_code == 1
        assert "per-user state not initialized" in result.output.lower()
        assert str(isolated_sandbox_ai_home) in result.output
        assert "sandbox init" in result.output.lower()


class TestInitFlagRemoval:
    """The legacy ``--user`` flag has been removed; Typer rejects it."""

    def test_user_flag_rejected(self, runner: CliRunner) -> None:
        from cli.main import app

        result = runner.invoke(app, ["init", "newproject", "--user", "sandbox"])
        assert result.exit_code != 0
        assert "no such option" in result.output.lower() or "--user" in result.output


class TestValidateName:
    """``_validate_name`` enforces the regex/length/leading-char/reserved rules."""

    def test_empty_rejected(self) -> None:
        from cli.main import _validate_name

        with pytest.raises(typer.BadParameter):
            _validate_name("", kind="instance", max_len=30)

    def test_too_long_rejected(self) -> None:
        from cli.main import _validate_name

        with pytest.raises(typer.BadParameter):
            _validate_name("a" * 31, kind="instance", max_len=30)

    def test_leading_dash_rejected(self) -> None:
        from cli.main import _validate_name

        with pytest.raises(typer.BadParameter):
            _validate_name("-foo", kind="instance", max_len=30)

    def test_leading_underscore_rejected(self) -> None:
        from cli.main import _validate_name

        with pytest.raises(typer.BadParameter):
            _validate_name("_foo", kind="instance", max_len=30)

    def test_invalid_chars_rejected(self) -> None:
        from cli.main import _validate_name

        with pytest.raises(typer.BadParameter):
            _validate_name("Foo!", kind="instance", max_len=30)

    @pytest.mark.parametrize("name", ["isolated", "all", "_backups", "default", "system"])
    def test_reserved_rejected(self, name: str) -> None:
        from cli.main import _validate_name

        with pytest.raises(typer.BadParameter):
            _validate_name(name, kind="workspace", max_len=32)

    @pytest.mark.parametrize("name", ["main", "backend-api", "scratch_2", "p"])
    def test_valid_name_accepted(self, name: str) -> None:
        from cli.main import _validate_name

        _validate_name(name, kind="workspace", max_len=32)


class TestParseWorkspaceFlags:
    """``_parse_workspace_flags`` builds WorkspaceSpec lists from CLI multi-flags."""

    def test_default_creates_single_empty_main(self, tmp_path: Path) -> None:
        from cli.main import _parse_workspace_flags

        specs = _parse_workspace_flags("inst", tmp_path, [], [])
        assert len(specs) == 1
        assert specs[0].name == "main"
        assert specs[0].bootstrap_mode == "empty"
        assert specs[0].source is None
        assert specs[0].path == str(tmp_path / "workspaces" / "inst" / "main")

    def test_copy_with_path(self, tmp_path: Path) -> None:
        from cli.main import _parse_workspace_flags

        specs = _parse_workspace_flags("inst", tmp_path, ["api=/src/api"], [])
        assert specs[0].name == "api"
        assert specs[0].bootstrap_mode == "copy"
        assert specs[0].source == "/src/api"

    def test_copy_without_equals_rejected(self, tmp_path: Path) -> None:
        from cli.main import _parse_workspace_flags

        with pytest.raises(typer.BadParameter):
            _parse_workspace_flags("inst", tmp_path, ["nope"], [])

    def test_copy_with_empty_name_rejected(self, tmp_path: Path) -> None:
        from cli.main import _parse_workspace_flags

        with pytest.raises(typer.BadParameter):
            _parse_workspace_flags("inst", tmp_path, ["=/path"], [])

    def test_copy_with_empty_path_rejected(self, tmp_path: Path) -> None:
        from cli.main import _parse_workspace_flags

        with pytest.raises(typer.BadParameter):
            _parse_workspace_flags("inst", tmp_path, ["api="], [])

    def test_duplicate_in_copy_rejected(self, tmp_path: Path) -> None:
        from cli.main import _parse_workspace_flags

        with pytest.raises(typer.BadParameter):
            _parse_workspace_flags("inst", tmp_path, ["api=/a", "api=/b"], [])

    def test_duplicate_across_flags_rejected(self, tmp_path: Path) -> None:
        from cli.main import _parse_workspace_flags

        with pytest.raises(typer.BadParameter):
            _parse_workspace_flags("inst", tmp_path, ["api=/a"], ["api"])

    def test_multiple_workspaces_collected(self, tmp_path: Path) -> None:
        from cli.main import _parse_workspace_flags

        specs = _parse_workspace_flags("inst", tmp_path, ["api=/a"], ["scratch", "logs"])
        names = [s.name for s in specs]
        assert names == ["api", "scratch", "logs"]


class TestPreflightWorkspaceSource:
    """``_preflight_workspace_source`` enforces source-side gates before rsync."""

    def test_nonexistent_source_rejected(self, tmp_path: Path) -> None:
        from cli.main import _preflight_workspace_source

        with pytest.raises(typer.BadParameter, match="does not exist"):
            _preflight_workspace_source(str(tmp_path / "missing"), inst="i", user_home=tmp_path)

    def test_unreadable_source_rejected(self, tmp_path: Path) -> None:
        from cli.main import _preflight_workspace_source

        src = tmp_path / "src"
        src.mkdir()
        os.chmod(src, 0o000)
        try:
            with pytest.raises(typer.BadParameter, match="not readable"):
                _preflight_workspace_source(str(src), inst="i", user_home=tmp_path)
        finally:
            os.chmod(src, 0o755)

    def test_boundary_source_rejected(self, tmp_path: Path) -> None:
        from cli.main import _preflight_workspace_source

        with pytest.raises(typer.BadParameter, match="walker boundary"):
            _preflight_workspace_source("/etc", inst="i", user_home=tmp_path)

    def test_cycle_source_rejected(self, tmp_path: Path) -> None:
        from cli.main import _preflight_workspace_source

        ws_root = tmp_path / "workspaces" / "i"
        nested = ws_root / "leak"
        nested.mkdir(parents=True)
        with pytest.raises(typer.BadParameter, match="cycle"):
            _preflight_workspace_source(str(nested), inst="i", user_home=tmp_path)

    def test_clean_source_passes(self, tmp_path: Path) -> None:
        from cli.main import _preflight_workspace_source

        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("hi")
        # No exception raised.
        _preflight_workspace_source(str(src), inst="i", user_home=tmp_path)

    def test_size_warning_emitted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from cli import main as cli_main
        from cli.main import _preflight_workspace_source

        src = tmp_path / "src"
        src.mkdir()
        (src / "big").write_text("x")
        monkeypatch.setattr("os.path.getsize", lambda _p: 6 * 1024 * 1024 * 1024)
        warnings: list[str] = []
        monkeypatch.setattr(cli_main.console, "print", lambda *a, **_kw: warnings.append(str(a[0])))
        _preflight_workspace_source(str(src), inst="i", user_home=tmp_path)
        assert any("larger than 5 GB" in w for w in warnings)


class TestInitDryRunNoConfigFallback:
    """Dry-run with absent host config falls back to placeholder values."""

    def test_dry_run_without_config_uses_dry_run_user(self, runner: CliRunner) -> None:
        from cli.main import app

        with (
            patch("cli.main._detect_git_config", return_value=("", "")),
            patch("cli.main.HostConfig.from_toml", side_effect=FileNotFoundError),
        ):
            result = runner.invoke(app, ["init", "newproject", "--dry-run"])
        # dry-run path tolerates missing host config; auth defaults to sudo
        assert result.exit_code == 0, result.output


class TestInitNoConfigPostSeedFails:
    """Defensive branch: missing config after seed (e.g. seed mocked)."""

    def test_missing_config_after_seed_in_non_dry_run_exits(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        from cli.main import app

        with (
            patch("cli.main.HostConfig.from_toml", side_effect=FileNotFoundError),
        ):
            result = runner.invoke(app, ["init", "newproject"])
        assert result.exit_code == 1
        assert "no host config" in result.output.lower()


class TestStdinIsTty:
    def test_returns_isatty_value(self) -> None:
        from cli.main import _stdin_is_tty

        # Just ensure it returns a bool without raising
        assert isinstance(_stdin_is_tty(), bool)


class TestInitHostConfigResolution:
    """Tasks 3.8-3.9: init user resolution via sandbox-ai.toml."""

    def test_init_with_project_config_no_user_flag(self, runner: CliRunner) -> None:
        """init succeeds without --user when sandbox-ai.toml provides docker_unprivileged_user."""
        from cli.main import app
        from core.host_config import HostConfig
        from core.hydration import InstanceConfig

        project_dir = "/home/user/tomlproject"

        mock_project_config = HostConfig.model_validate(
            {"host": {"docker_unprivileged_user": "sandbox", "machinectl_authentication": "sudo"}}
        )
        mock_config = InstanceConfig.model_validate(
            {
                "instance": {"name": "tomlproject", "host_uid": "1000"},
                "workspaces": {"main": {"bootstrap_mode": "empty", "path": project_dir}},
            }
        )

        with (
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
            result = runner.invoke(app, ["init", "tomlproject"])
            assert result.exit_code == 0

    def test_init_non_tty_without_config_fails_with_guidance(
        self,
        runner: CliRunner,
        mock_sandbox_ai_home: Path,
        isolated_sandbox_ai_home: Path,
    ) -> None:
        """init in non-TTY mode without canonical host config exits with guidance."""
        from cli.main import app

        with (
            # Disable the autouse no-op so the real seeder runs.
            patch("cli.main._seed_host_config_if_absent", wraps=_REAL_SEED_HOST_CONFIG),
            patch("cli.main._stdin_is_tty", return_value=False),
        ):
            result = runner.invoke(app, ["init", "tomlproject"])
        assert result.exit_code == 1, result.output
        assert "non-interactive" in result.output.lower()
        # Rich may line-wrap long paths; collapse newlines before substring check.
        assert "sandbox-ai.toml" in result.output.replace("\n", "")

    def test_init_tty_seeds_host_config(
        self,
        runner: CliRunner,
        mock_sandbox_ai_home: Path,
        isolated_sandbox_ai_home: Path,
    ) -> None:
        """init in TTY mode prompts and writes <home>/config/sandbox-ai.toml when absent."""
        from cli.main import app
        from core.hydration import InstanceConfig

        project_dir = "/home/user/seedproj"
        mock_config = InstanceConfig.model_validate(
            {
                "instance": {"name": "seedproj", "host_uid": "1000"},
                "workspaces": {"main": {"bootstrap_mode": "empty", "path": project_dir}},
            }
        )

        with (
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
            result = runner.invoke(app, ["init", "seedproj"])
        seeded = isolated_sandbox_ai_home / "config" / "sandbox-ai.toml"
        assert seeded.exists(), f"output={result.output!r} exit={result.exit_code}"
        body = seeded.read_text()
        assert 'docker_unprivileged_user = "sandbox-user"' in body
        assert 'machinectl_authentication = "sudo"' in body
        assert result.exit_code == 0, result.output

    def test_init_tty_rejects_empty_user(
        self,
        runner: CliRunner,
        mock_sandbox_ai_home: Path,
        isolated_sandbox_ai_home: Path,
    ) -> None:
        """Empty docker_unprivileged_user is re-prompted until non-empty."""
        from cli.main import app
        from core.hydration import InstanceConfig

        project_dir = "/home/user/empty"
        mock_config = InstanceConfig.model_validate(
            {
                "instance": {"name": "empty", "host_uid": "1000"},
                "workspaces": {"main": {"bootstrap_mode": "empty", "path": project_dir}},
            }
        )

        with (
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
            result = runner.invoke(app, ["init", "empty"])
        assert result.exit_code == 0, result.output
        seeded = isolated_sandbox_ai_home / "config" / "sandbox-ai.toml"
        assert 'docker_unprivileged_user = "sandbox"' in seeded.read_text()

    def test_init_tty_rejects_invalid_auth_value(
        self,
        runner: CliRunner,
        mock_sandbox_ai_home: Path,
        isolated_sandbox_ai_home: Path,
    ) -> None:
        """Invalid machinectl_authentication value is rejected."""
        from cli.main import app

        with (
            patch(
                "cli.main._seed_host_config_if_absent",
                wraps=_REAL_SEED_HOST_CONFIG,
            ),
            patch("cli.main._stdin_is_tty", return_value=True),
            patch("cli.main.typer.prompt", side_effect=["sandbox", "weird"]),
        ):
            result = runner.invoke(app, ["init", "empty"])
        assert result.exit_code == 1
        assert "invalid machinectl_authentication" in result.output.lower()

    def test_init_existing_host_config_not_overwritten(
        self,
        runner: CliRunner,
        mock_sandbox_ai_home: Path,
        isolated_sandbox_ai_home: Path,
    ) -> None:
        """When the canonical host config exists, init does not prompt and does not overwrite."""
        from cli.main import app
        from core.host_config import ensure_per_user_state
        from core.hydration import InstanceConfig

        ensure_per_user_state(isolated_sandbox_ai_home)
        existing_body = '[host]\ndocker_unprivileged_user = "preserved"\nmachinectl_authentication = "polkit"\n'
        cfg_path = isolated_sandbox_ai_home / "config" / "sandbox-ai.toml"
        cfg_path.write_text(existing_body)

        project_dir = "/home/user/preserve"
        mock_config = InstanceConfig.model_validate(
            {
                "instance": {"name": "preserve", "host_uid": "1000"},
                "workspaces": {"main": {"bootstrap_mode": "empty", "path": project_dir}},
            }
        )

        with (
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
            result = runner.invoke(app, ["init", "preserve"])
        assert result.exit_code == 0, result.output
        # No prompts issued because file existed
        mock_prompt.assert_not_called()
        # File untouched
        assert cfg_path.read_text() == existing_body


class TestInitAuthProbe:
    """Init-time auth mode probe tests.

    The probe is a *branch* callsite (reachable → continue; unreachable →
    guidance + exit 1), so per Q8 it routes through ``core.dispatch.probe``
    (the non-raising typed-outcome entry point), NOT ``invoke``. These tests
    mock ``cli.main.dispatch.probe`` at the boundary and assert the typed
    ``ProbeOutcome`` is mapped to the original control flow / messages 1:1.
    """

    def _ok(self) -> ProbeOutcome:
        from core.dispatch import ProbeOutcome

        return ProbeOutcome(ok=True, timed_out=False, stdout="ok\n", message="")

    def _fail(self, *, timed_out: bool = False, message: str = "[FATAL] probe failed") -> ProbeOutcome:
        from core.dispatch import ProbeOutcome

        return ProbeOutcome(ok=False, timed_out=timed_out, stdout="", message=message)

    def test_probe_success_sudo(self, runner: CliRunner) -> None:
        """Probe succeeds with sudo mode — init proceeds; the probe's
        host_config carries the resolved sudo auth + user."""
        from cli.main import app
        from core.host_config import MachinectlAuth
        from core.hydration import InstanceConfig

        project_dir = "/home/user/probeproject"
        mock_config = InstanceConfig.model_validate(
            {
                "instance": {"name": "probeproject", "host_uid": "1000"},
                "workspaces": {"main": {"bootstrap_mode": "empty", "path": project_dir}},
            }
        )

        with (
            patch("cli.main.dispatch.probe", return_value=self._ok()) as mock_probe,
            patch("cli.main._detect_git_config", return_value=("", "")),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main.check_compose_project_name_collision") as mock_collision,
            patch("cli.main.create_instance_dirs"),
            patch("cli.main.write_sandbox_toml"),
            patch("cli.main._load_config", return_value=mock_config),
            patch("cli.main.create_env_file"),
            patch("cli.main.apply_default_acls"),
            patch("cli.main.prompt_secrets"),
            patch("cli.main.write_initialized_sentinel"),
        ):
            mock_collision.return_value.status = "pass"
            result = runner.invoke(app, ["init", "probeproject"])
            assert result.exit_code == 0
            (op, args, host_config), kwargs = mock_probe.call_args
            assert op == "auth-probe"
            assert args == []
            assert kwargs["timeout"] == 5
            assert host_config.host.machinectl_authentication == MachinectlAuth.SUDO

    def test_probe_failure_exits_with_remediation(self, runner: CliRunner) -> None:
        """Probe failure (not-ok, not-timeout) exits with error + remediation."""
        from cli.main import app

        with patch("cli.main.dispatch.probe", return_value=self._fail(message="boom: exit status 1")):
            result = runner.invoke(app, ["init", "probeproject"])
            assert result.exit_code == 1
            assert "probe failed" in result.output.lower()
            assert "remediation" in result.output.lower()
            # Restored diagnostic fidelity: the real failure context from
            # ProbeOutcome.message is surfaced (not the prior generic string).
            assert "boom: exit status 1" in result.output

    def test_probe_timeout_exits_with_error(self, runner: CliRunner) -> None:
        """Probe timeout (timed_out=True) exits with the exact original
        'probe timed out after 5 seconds' message."""
        from cli.main import app

        with patch("cli.main.dispatch.probe", return_value=self._fail(timed_out=True)):
            result = runner.invoke(app, ["init", "probeproject"])
            assert result.exit_code == 1
            assert "probe timed out after 5 seconds" in result.output.lower()

    def test_probe_polkit_mode_no_sudo(self, runner: CliRunner) -> None:
        """Polkit mode probe — host_config carries POLKIT auth."""
        from cli.main import app
        from core.host_config import MachinectlAuth
        from core.hydration import InstanceConfig

        project_dir = "/home/user/polkit"
        mock_config = InstanceConfig.model_validate(
            {
                "instance": {"name": "polkit", "host_uid": "1000"},
                "workspaces": {"main": {"bootstrap_mode": "empty", "path": project_dir}},
            }
        )

        with (
            patch("cli.main.dispatch.probe", return_value=self._ok()) as mock_probe,
            patch("cli.main._detect_git_config", return_value=("", "")),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main.check_compose_project_name_collision") as mock_collision,
            patch("cli.main.create_instance_dirs"),
            patch("cli.main.write_sandbox_toml"),
            patch("cli.main._load_config", return_value=mock_config),
            patch("cli.main.create_env_file"),
            patch("cli.main.apply_default_acls"),
            patch("cli.main.prompt_secrets"),
            patch("cli.main.write_initialized_sentinel"),
        ):
            mock_collision.return_value.status = "pass"
            result = runner.invoke(app, ["init", "polkit", "--machinectl-auth", "polkit"])
            assert result.exit_code == 0
            (op, args, host_config), kwargs = mock_probe.call_args
            assert op == "auth-probe"
            assert host_config.host.machinectl_authentication == MachinectlAuth.POLKIT

    def test_probe_polkit_failure_shows_polkit_remediation(self, runner: CliRunner) -> None:
        """Polkit probe failure shows polkit-specific remediation."""
        from cli.main import app

        with patch("cli.main.dispatch.probe", return_value=self._fail()):
            result = runner.invoke(app, ["init", "polkit", "--machinectl-auth", "polkit"])
            assert result.exit_code == 1
            assert "polkit" in result.output.lower()

    def test_invalid_machinectl_auth_value(self, runner: CliRunner) -> None:
        """Invalid --machinectl-auth value exits with error."""
        from cli.main import app

        result = runner.invoke(app, ["init", "polkit", "--machinectl-auth", "invalid"])
        assert result.exit_code == 1
        assert "invalid" in result.output.lower()

    def test_probe_file_not_found_consolidated_into_failure_branch(self, runner: CliRunner) -> None:
        """The prior FileNotFoundError ("command not found on PATH") case still
        routes through the not-ok/not-timeout branch (Q8 narrowing —
        ProbeOutcome carries no ENOENT discriminator). Diagnostic fidelity is
        restored: ``Executor`` wraps the ``OSError`` into a distinct
        ``SandboxExecutionError`` text that ``ProbeOutcome.message`` carries, so
        the operator again sees the real ENOENT context; exit code is 1."""
        from cli.main import app

        enoent_msg = "[FATAL] ENOENT-machinectl-missing"
        with patch("cli.main.dispatch.probe", return_value=self._fail(message=enoent_msg)):
            result = runner.invoke(app, ["init", "polkit"])
            assert result.exit_code == 1
            # The distinct ENOENT SandboxExecutionError text reaches the
            # operator via ProbeOutcome.message (restored fidelity).
            assert "ENOENT-machinectl-missing" in result.output
            assert "probe failed" in result.output.lower()


class TestResolveHostConfig:
    """Coverage: _resolve_host_config with HostConfig present."""

    def test_resolve_host_config_from_project_config(self) -> None:
        """_resolve_host_config returns values from HostConfig when present."""
        from cli.main import _resolve_host_config
        from core.host_config import HostConfig, MachinectlAuth

        mock_project_config = HostConfig.model_validate(
            {"host": {"docker_unprivileged_user": "fromtoml", "machinectl_authentication": "polkit"}}
        )

        with patch("cli.main.HostConfig.from_toml", return_value=mock_project_config):
            user, auth = _resolve_host_config()
            assert user == "fromtoml"
            assert auth == MachinectlAuth.POLKIT


@pytest.mark.usefixtures("stub_bridge_resolution")
class TestDryRunExistingInstance:
    """Task 12.1: --dry-run with existing instance."""

    def test_dry_run_skips_warm_check(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """Warm state check is skipped when --dry-run is set."""
        inst = "myproject"
        _register_instance(inst)
        _write_ipam("myproject", 0)

        # Create tooling plane files for template validation
        _create_tooling_plane(mock_sandbox_ai_home)

        from cli.main import app

        with (
            patch("cli.main._warm_check") as mock_warm,
        ):
            result = runner.invoke(app, ["start", inst, "--dry-run"])
            mock_warm.assert_not_called()
            assert result.exit_code == 0

    def test_dry_run_existing_instance_exit_0(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """Existing instance dry-run passes with exit code 0."""
        inst = "myproject"
        _register_instance(inst)
        _write_ipam("myproject", 0)
        _create_tooling_plane(mock_sandbox_ai_home)

        from cli.main import app

        result = runner.invoke(app, ["start", inst, "--dry-run"])
        assert result.exit_code == 0

    def test_dry_run_shows_ipam_preview(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """IPAM slot is previewed with 'subject to concurrent changes' note."""
        inst = "myproject"
        _register_instance(inst)
        _write_ipam("myproject", 5)
        _create_tooling_plane(mock_sandbox_ai_home)

        from cli.main import app

        result = runner.invoke(app, ["start", inst, "--dry-run"])
        assert result.exit_code == 0
        # Should mention the IPAM slot
        assert "5" in result.output or "slot" in result.output.lower()

    def test_dry_run_shows_compose_command(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """Compose command is displayed in dry-run output."""
        inst = "myproject"
        _register_instance(inst)
        _write_ipam("myproject", 0)
        _create_tooling_plane(mock_sandbox_ai_home)

        from cli.main import app

        result = runner.invoke(app, ["start", inst, "--dry-run"])
        assert "docker compose" in result.output.lower() or "compose" in result.output.lower()

    def test_dry_run_template_error_exits_1(
        self, runner: CliRunner, mock_sandbox_ai_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Template error causes exit code 1."""
        import core.hydration as hydration
        import jinja2

        home = mock_sandbox_ai_home
        inst = "myproject"
        _register_instance(inst)
        _write_ipam("myproject", 0)
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

        result = runner.invoke(app, ["start", inst, "--dry-run"])
        assert result.exit_code == 1

    def test_dry_run_missing_env_keys_reported(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """Missing .sandbox.env keys are reported in dry-run."""
        inst = "myproject"
        instance_dir = _register_instance(inst)
        _write_ipam("myproject", 0)
        _create_tooling_plane(mock_sandbox_ai_home)
        # Write empty env file — secrets missing
        (instance_dir / ".sandbox.env").write_text('CORE_ANTHROPIC_API_KEY=""\nCORE_GITHUB_TOKEN=""')

        from cli.main import app

        result = runner.invoke(app, ["start", inst, "--dry-run"])
        # Should mention missing secrets
        out = result.output.lower()
        assert "missing" in out or "secret" in out or "empty" in out


class TestDryRunNewInstance:
    """Task 5.1: --dry-run with no existing instance → error with guidance."""

    def test_dry_run_no_instance_errors_with_guidance(self, runner: CliRunner) -> None:
        """No-instance dry-run errors with init guidance message."""
        from cli.main import app

        _seed_registry(_user_home())
        result = runner.invoke(app, ["start", "missing", "--dry-run"])
        assert result.exit_code == 1
        assert "sandbox init" in result.output.lower()


def _create_tooling_plane(home: Path) -> None:
    """Create minimal tooling plane files needed for dry-run template validation."""
    # Jinja2 templates
    docker_dir = home / ".docker"
    (docker_dir / "compose.yml").write_text("# compose for {{ instance_name }}\nversion: '3'\n")
    (docker_dir / "core" / "entrypoint.sh").write_text("#!/bin/bash\n")
    (docker_dir / "core" / "Dockerfile.core.wolfi").write_text("FROM {{ core_base_image }}\n")
    (docker_dir / "admin").mkdir(parents=True, exist_ok=True)
    (docker_dir / "admin" / "Dockerfile.admin").write_text("FROM scratch\nENTRYPOINT [\"/fwd\"]\n")
    (docker_dir / "admin" / "fwd.go").write_text("package main\nfunc main() {}\n")
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

        inst = "myproject"
        _register_instance(inst)
        _create_tooling_plane(mock_sandbox_ai_home)

        from cli.main import app

        with (
            patch.object(
                __import__("core.ipam", fromlist=["IPAMLedger"]).IPAMLedger,
                "peek_next_slot",
                side_effect=IPAMExhaustedError("All slots consumed"),
            ),
        ):
            result = runner.invoke(app, ["start", inst, "--dry-run"])
            assert result.exit_code == 1


@pytest.mark.usefixtures("stub_bridge_resolution")
class TestCheckSecretsFirecrawl:
    """Cover firecrawl secret branch in _check_secrets."""

    def test_check_secrets_firecrawl_missing(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """Firecrawl secret reported when mcp_firecrawl=true."""
        home = mock_sandbox_ai_home
        inst = "myproject"
        instance_dir = _register_instance(inst)
        _write_ipam("myproject", 0)
        _create_tooling_plane(home)

        # Write a sandbox.toml with mcp_firecrawl=true
        firecrawl_toml = (instance_dir / "sandbox.toml").read_bytes().decode()
        firecrawl_toml = firecrawl_toml.replace("mcp_firecrawl = false", "mcp_firecrawl = true")
        (instance_dir / "sandbox.toml").write_text(firecrawl_toml)

        # Also need mcp-firecrawl.yml in tooling plane
        extras = home / ".docker" / "extras"
        (extras / "mcp-firecrawl.yml").write_text("# firecrawl\n")
        (extras / "Dockerfile.mcp-firecrawl").write_text("FROM node\n")

        from cli.main import app

        result = runner.invoke(app, ["start", inst, "--dry-run"])
        out = result.output.lower()
        assert "firecrawl" in out or "missing" in out or "secret" in out


# ── Container Status Function ────────────────────────────────────────────────


class TestContainerStatus:
    """_container_status NDJSON parsing — routes through core.dispatch.probe.

    `_container_status` *branched* on failure before the refactor (caught
    SandboxExecutionError → returned []); per Q8 it now uses the non-raising
    `probe()` and returns [] when `outcome.ok` is False — 1:1 preservation of
    the empty-on-error contract. The op arg is the typed `[config.instance.name]`.
    """

    def _config(self) -> InstanceConfig:
        from core.hydration import InstanceConfig

        return InstanceConfig.model_validate(
            {
                "instance": {
                    "name": "t",
                    "host_uid": "1000",
                },
                "workspaces": {"main": {"bootstrap_mode": "empty", "path": "/x"}},
            }
        )

    def _outcome(self, *, ok: bool, stdout: str = "") -> ProbeOutcome:
        from core.dispatch import ProbeOutcome

        return ProbeOutcome(ok=ok, timed_out=False, stdout=stdout, message="" if ok else "[FATAL] probe failed")

    def test_parses_ndjson_output(self, tmp_path: Path) -> None:
        """Multiple NDJSON lines are parsed into ContainerInfo list."""
        from cli.main import ContainerInfo, _container_status

        ndjson = (
            '{"Name":"t-core-1","Service":"core","State":"running","Health":"healthy","Status":"Up 5s"}\n'
            '{"Name":"t-admin-1","Service":"admin","State":"running","Health":"","Status":"Up 5s"}\n'
        )
        compose = tmp_path / "docker" / "compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("version: '3'")

        with patch(
            "cli.main.dispatch.probe", return_value=self._outcome(ok=True, stdout=ndjson)
        ) as mock_probe:
            containers = _container_status(str(tmp_path), "t", "s", self._config())

        (op, args, _host_config), _kwargs = mock_probe.call_args
        assert op == "compose-ps"
        assert args == ["t"]
        assert len(containers) == 2
        assert containers[0].name == "t-core-1"
        assert containers[0].service == "core"
        assert containers[0].state == "running"
        assert containers[0].health == "healthy"
        assert isinstance(containers[1], ContainerInfo)

    def test_empty_for_stopped_instance(self, tmp_path: Path) -> None:
        """Stopped instance (ok but empty stdout) returns empty list."""
        from cli.main import _container_status

        compose = tmp_path / "docker" / "compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("version: '3'")

        with patch("cli.main.dispatch.probe", return_value=self._outcome(ok=True, stdout="")):
            containers = _container_status(str(tmp_path), "t", "s", self._config())

        assert containers == []

    def test_executor_error_returns_empty(self, tmp_path: Path) -> None:
        """A failed probe (not-ok) returns empty list instead of raising —
        1:1 with the prior `except SandboxExecutionError: return []`."""
        from cli.main import _container_status

        compose = tmp_path / "docker" / "compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("version: '3'")

        with patch("cli.main.dispatch.probe", return_value=self._outcome(ok=False)):
            containers = _container_status(str(tmp_path), "t", "s", self._config())

        assert containers == []


# ── Status Command ───────────────────────────────────────────────────────────


class TestStatusNoInstance:
    """Task 7.1: sandbox status — no instance error."""

    def test_status_no_instance_exits_1(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        result = runner.invoke(app, ["status", "missing"])
        assert result.exit_code == 1
        assert "no sandbox" in result.output.lower()


class TestStatusRunning:
    """Task 7.1: sandbox status — running instance."""

    def test_status_running_shows_state(self, runner: CliRunner) -> None:
        inst = "myproject"
        _register_instance(inst)
        _write_ipam("myproject", 0)

        from cli.main import ContainerInfo, app

        containers = [
            ContainerInfo(name="t-core-1", service="core", state="running", health="healthy", status="Up 5s"),
            ContainerInfo(name="t-admin-1", service="admin", state="running", health="healthy", status="Up 5s"),
        ]

        with (
            patch("cli.main._container_status", return_value=containers),
        ):
            result = runner.invoke(app, ["status", inst])
            assert result.exit_code == 0
            out = result.output.lower()
            assert "running" in out
            assert "core" in out
            # Task 7.5a: container table must include IP addresses
            # For slot 0: core → agent_isolated_ip = 10.100.0.3
            assert "10.100.0.3" in result.output
            # Admin row IP must come from admin_ipc_ip (post-reframe key).
            # For slot 0: admin_ipc_ip = ipc_base.2 = 10.100.4.2
            from core.ipam import derive_static_ips

            expected_admin_ip = derive_static_ips(0)["admin_ipc_ip"]
            assert expected_admin_ip in result.output
            assert "network" in out


class TestStatusStopped:
    """Task 7.1: sandbox status — stopped instance."""

    def test_status_stopped_shows_state(self, runner: CliRunner) -> None:
        inst = "myproject"
        _register_instance(inst)
        _write_ipam("myproject", 0)

        from cli.main import app

        with (
            patch("cli.main._container_status", return_value=[]),
        ):
            result = runner.invoke(app, ["status", inst])
            assert result.exit_code == 0
            out = result.output.lower()
            assert "stopped" in out


class TestStatusDegraded:
    """Task 7.1: sandbox status — degraded state."""

    def test_status_degraded_shows_warning(self, runner: CliRunner) -> None:
        inst = "myproject"
        _register_instance(inst)
        _write_ipam("myproject", 0)

        from cli.main import ContainerInfo, app

        containers = [
            ContainerInfo(name="t-core-1", service="core", state="running", health="healthy", status="Up"),
            ContainerInfo(name="t-admin-1", service="admin", state="running", health="unhealthy", status="Up"),
        ]

        with (
            patch("cli.main._container_status", return_value=containers),
        ):
            result = runner.invoke(app, ["status", inst])
            assert result.exit_code == 0
            out = result.output.lower()
            assert "degraded" in out


class TestStatusIPAM:
    """Task 7.1: sandbox status — IPAM display."""

    def test_status_shows_ipam_subnets(self, runner: CliRunner) -> None:
        inst = "myproject"
        _register_instance(inst)
        _write_ipam("myproject", 3)

        from cli.main import app

        with (
            patch("cli.main._container_status", return_value=[]),
        ):
            result = runner.invoke(app, ["status", inst])
            assert result.exit_code == 0
            out = result.output
            # Should display IPAM slot and subnets
            assert "3" in out or "slot" in out.lower()
            assert "10." in out


class TestStatusConfigWarnings:
    """Task 9.2: status output shows ⊘ and missing secret names."""

    def test_status_warns_on_missing_secrets(self, runner: CliRunner) -> None:
        inst = "myproject"
        instance_dir = _register_instance(inst)
        _write_ipam("myproject", 0)

        # Write env file with empty PG_PASSWORD
        env_path = instance_dir / ".sandbox.env"
        env_path.write_text("CORE_ANTHROPIC_API_KEY=sk-123\nPG_PASSWORD=\n")

        from cli.main import app

        with (
            patch("cli.main._container_status", return_value=[]),
        ):
            result = runner.invoke(app, ["status", inst])
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
        descriptions = [a.description for a in plan]
        assert any("ancestor traverse" in d for d in descriptions)

    def test_revoke_plan_excludes_ancestors(self, tmp_path: Path) -> None:
        """Revoke plan does NOT include ancestor entries."""
        from cli.main import _acl_revoke_plan

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)

        plan = _acl_revoke_plan(str(instance_dir), "sandbox")
        descriptions = [a.description for a in plan]
        assert not any("ancestor" in d for d in descriptions)

    def test_grant_plan_includes_env_file_revoke_excludes(self, tmp_path: Path) -> None:
        """Grant plan includes .sandbox.env; revoke plan excludes it because
        the named-ACL on .sandbox.env is `granted-once, persistent` (cluster 3
        — Environment File Read ACL). Asymmetry is the structural fix that
        eliminated the env-ACL re-grant rituals in `_container_status` and
        `destroy`'s preflight.
        """
        from cli.main import _acl_grant_plan, _acl_revoke_plan

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)
        (instance_dir / "docker").mkdir()
        (instance_dir / "config").mkdir()
        (instance_dir / ".sandbox.env").write_text("")

        grant = _acl_grant_plan(str(instance_dir), "sandbox")
        revoke = _acl_revoke_plan(str(instance_dir), "sandbox")

        grant_descs = [a.description for a in grant]
        revoke_descs = [a.description for a in revoke]
        assert any("env file" in d for d in grant_descs)
        assert not any("env file" in d for d in revoke_descs)

    def test_grant_plan_includes_instance_root(self, tmp_path: Path) -> None:
        """Grant plan includes instance root with r-x."""
        from cli.main import _acl_grant_plan

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)
        (instance_dir / "docker").mkdir()
        (instance_dir / "config").mkdir()
        (instance_dir / ".sandbox.env").write_text("")

        plan = _acl_grant_plan(str(instance_dir), "sandbox")
        descriptions = [a.description for a in plan]
        assert any("instance root" in d for d in descriptions)
        # Verify r-x permission on instance root
        root_entries = [a for a in plan if "instance root" in a.description]
        assert any("r-x" in " ".join(a.command) for a in root_entries)

    def test_grant_plan_excludes_cache_log_option_b(self, tmp_path: Path) -> None:
        """Post-acl-ownership-recipes: cache/log Option-B grants are absent from grant plan."""
        from cli.main import _acl_grant_plan

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)
        (instance_dir / "docker").mkdir()
        (instance_dir / "config").mkdir()
        (instance_dir / ".sandbox.env").write_text("")

        plan = _acl_grant_plan(str(instance_dir), "sandbox")
        for action in plan:
            for cache_log in ["cache/core/.claude", "cache/admin/tmux_resurrect", "log/core", "log/admin"]:
                assert cache_log not in action.description
                assert not any(cache_log in arg for arg in action.command)

    def test_grant_plan_includes_workspace_named_acl(self, tmp_path: Path) -> None:
        """Workspace named-ACL is granted when user_project_root is supplied."""
        from cli.main import _acl_grant_plan

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)
        ws = tmp_path / "myws"
        ws.mkdir()

        plan = _acl_grant_plan(str(instance_dir), "sandbox", [str(ws)], dev_user="dev")
        descriptions = [a.description for a in plan]
        assert any("workspace named-ACL" in d for d in descriptions)
        assert any("workspace default ACL" in d for d in descriptions)
        # Effective entry contains rwx
        ws_eff = next(a for a in plan if "workspace named-ACL" in a.description)
        assert "u:sandbox:rwx" in " ".join(ws_eff.command)
        # Default entry includes both host_user and dev_user
        ws_def = next(a for a in plan if "workspace default ACL" in a.description)
        joined = " ".join(ws_def.command)
        assert "u:sandbox:rwx" in joined
        assert "u:dev:rwx" in joined

    def test_revoke_plan_excludes_cache_log(self, tmp_path: Path) -> None:
        """Post-acl-ownership-recipes: cache/log entries are absent from revoke plan."""
        from cli.main import _acl_revoke_plan

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)

        plan = _acl_revoke_plan(str(instance_dir), "sandbox")
        for action in plan:
            for cache_log in ["cache/core/.claude", "cache/admin/tmux_resurrect", "log/core", "log/admin"]:
                assert cache_log not in action.description
                assert not any(cache_log in arg for arg in action.command)

    def test_revoke_plan_includes_workspace_named_acl(self, tmp_path: Path) -> None:
        """Workspace named-ACL effective and default-entry revocations."""
        from cli.main import _acl_revoke_plan

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)
        ws = tmp_path / "myws"

        plan = _acl_revoke_plan(str(instance_dir), "sandbox", [str(ws)])
        descriptions = [a.description for a in plan]
        assert any("workspace named-ACL" in d for d in descriptions)
        assert any("workspace default named entry" in d for d in descriptions)
        # The default revocation uses -d -x
        ws_def = next(a for a in plan if "workspace default named entry" in a.description)
        assert "-d" in ws_def.command
        assert "-x" in ws_def.command

    def test_revoke_plan_omits_workspace_when_user_project_root_none(self, tmp_path: Path) -> None:
        """No workspace entries when user_project_root is None (back-compat call shape)."""
        from cli.main import _acl_revoke_plan

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)

        plan = _acl_revoke_plan(str(instance_dir), "sandbox")
        descriptions = [a.description for a in plan]
        # Match description-prefix tokens, not substrings — tmp_path may contain "workspace".
        assert not any(d.startswith("workspace ") for d in descriptions)

    def test_grant_plan_handles_unstattable_workspace_ancestor(self, tmp_path: Path) -> None:
        """_compute_workspace_ancestors stops cleanly at a non-stat-able parent."""
        from cli.main import _acl_grant_plan

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)

        # Workspace under a nonexistent path → os.stat on the parent raises OSError.
        plan = _acl_grant_plan(str(instance_dir), "sandbox", ["/nonexistent-root/myproj"], dev_user="dev")
        # Should not have crashed; the workspace named-ACL is still queued.
        assert any("workspace named-ACL" in a.description for a in plan)

    def test_grant_plan_includes_secrets_provisioning_write(self, tmp_path: Path) -> None:
        """Grant plan emits a dir-level rwx provisioning grant on secrets/.

        See ``test_acl_grant_plan_secrets_dir_is_dir_level_rwx_not_recursive_rwx``
        for the BUG-A/BUG-B partition rationale; this thinner test pins
        that the entry is present so refactors don't quietly drop the
        provisioning grant.
        """
        from cli.main import _acl_grant_plan

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)
        (instance_dir / "docker").mkdir()
        (instance_dir / "config").mkdir()
        (instance_dir / ".sandbox.env").write_text("")

        plan = _acl_grant_plan(str(instance_dir), "sandbox")
        descriptions = [a.description for a in plan]
        assert any("secrets dir provisioning write" in d for d in descriptions)

    def test_grant_plan_dedups_shared_ancestor_across_workspaces(self, tmp_path: Path) -> None:
        """Two workspaces under the same parent dir produce one ancestor-traverse entry per ancestor."""
        from cli.main import _acl_grant_plan

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)
        ws_root = tmp_path / "shared-parent"
        ws_root.mkdir()
        ws_a = ws_root / "a"
        ws_b = ws_root / "b"
        ws_a.mkdir()
        ws_b.mkdir()

        plan = _acl_grant_plan(str(instance_dir), "sandbox", [str(ws_a), str(ws_b)], dev_user="dev")
        ancestor_targets = [a.command[-1] for a in plan if a.description.startswith("workspace ancestor traverse: ")]
        # Shared parent appears at most once even though both workspaces walk through it.
        assert ancestor_targets.count(str(ws_root)) == 1

    def test_acl_grant_plan_includes_helper_parent_grants(self, tmp_path: Path) -> None:
        """Cluster 1 regression: helper-recipe parents (cache/log) get u:<daemon>:rwx + matching default ACL.

        Per orchestrator-volumes' "Helper-Recipe Parent ACL Grants" requirement:
        scaffold creates cache/core, cache/admin, log/ as host dev:dev mode 0775;
        the daemon's userns has dev unmapped, so CAP_DAC_OVERRIDE cannot bypass
        DAC. Without the named-acl grant, the helper sees only "other" (r-x)
        and EPERMs on mkdir for the helper-recipe leaves. The grant plan adds
        an effective ``u:<daemon>:rwx`` plus a matching default ACL on each
        helper-recipe parent so the helper recipe can create leaves inside.
        """
        from cli.main import _acl_grant_plan

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)
        (instance_dir / "docker").mkdir()
        (instance_dir / "config").mkdir()
        (instance_dir / ".sandbox.env").write_text("")

        plan = _acl_grant_plan(str(instance_dir), "sandbox")
        for parent_rel in ("cache/core", "log"):
            parent_abs = str(instance_dir / parent_rel)
            effective = [
                a.command
                for a in plan
                if a.description == f"helper-recipe parent: {parent_abs}"
            ]
            assert effective, f"missing helper-recipe-parent effective grant for {parent_rel}"
            assert any(arg == "u:sandbox:rwx" for arg in effective[0]), (
                f"helper-recipe parent {parent_rel} missing u:<daemon>:rwx"
            )
            default_entries = [
                a.command
                for a in plan
                if a.description == f"helper-recipe parent default ACL: {parent_abs}"
            ]
            assert default_entries, (
                f"missing helper-recipe-parent default ACL grant for {parent_rel}"
            )
            joined = " ".join(default_entries[0])
            assert "-d" in joined and "u:sandbox:rwx" in joined, (
                f"helper-recipe parent {parent_rel} missing default-ACL daemon entry"
            )

    def test_workspace_shared_group_plan_omits_explicit_g_rwx_setfacl(self) -> None:
        """Cluster 1 structural fix for finding 8.N: post-reorder, no explicit g::rwx setfacl.

        Spec source: orchestrator-volumes' "Workspace Shared-Group Phase
        Ordering" requirement. The structural fix runs the shared-group
        recipe BEFORE Phase-5 named-ACL grants, so ``chmod 2770`` lands
        on a non-extended-ACL inode and the ``group::`` entry propagates
        from the mode bits without a separate setfacl call. Any
        ``setfacl -m g::rwx`` step in the plan would indicate the temp
        workaround has resurfaced.
        """
        from cli.main import _workspace_shared_group_plan

        plan = _workspace_shared_group_plan("/ws", 200500, "dev", "claude-sandbox")
        ops = [a.op for a in plan]
        assert not any("setfacl -m g::rwx" in op for op in ops), (
            "workspace shared-group plan must NOT emit explicit g::rwx setfacl post-reorder"
        )

    def test_acl_grant_plan_secrets_dir_is_dir_level_rwx_not_recursive_rwx(
        self, tmp_path: Path
    ) -> None:
        """Cluster 1 structural fix for finding 8.D — partitioned BUG-A vs BUG-B.

        Spec source: orchestrator-volumes' "Helper-CP Source Files
        Daemon-Readable Pre-Recipe" requirement. The temp's recursive
        ``rwX`` widening on ``secrets/`` is replaced by a partitioned
        permission model:

        - **BUG-A** (daemon RUNTIME READ on file contents): per-file
          ``setfacl -m u:<host_user>:r <file>`` granted post-hydrate by
          ``_phase_grant_helper_cp_files_daemon_read``.
        - **BUG-B** (daemon PROVISIONING WRITE on the parent dir): the
          dir-level ``setfacl -m u:<host_user>:rwx <secrets_dir>`` grant
          asserted here. Required by helper-cp's ``mv /tmp/$f /p/$f``,
          which needs write on the destination's parent so the daemon
          can unlink the existing dest and rename the new inode in.

        Together: dir-level ``rwx`` (BUG-B) + per-file ``r`` (BUG-A) is
        strictly narrower than the temp's recursive ``rwX`` because the
        recursive ``rwX`` granted daemon write on file CONTENTS too,
        whereas this partition leaves contents at per-file ``r`` (read
        only).
        """
        from cli.main import _acl_grant_plan

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)
        (instance_dir / "docker").mkdir()
        (instance_dir / "config").mkdir()
        (instance_dir / ".sandbox.env").write_text("")

        plan = _acl_grant_plan(str(instance_dir), "sandbox")
        secrets_entries = [a for a in plan if "secrets dir provisioning write" in a.description]
        assert secrets_entries, "secrets dir provisioning-write entry missing"
        args = secrets_entries[0].command
        joined = " ".join(args)
        assert "-R" not in args, f"secrets/ grant must NOT be recursive: {joined}"
        assert "rwX" not in joined, f"secrets/ grant must NOT carry rwX: {joined}"
        assert "u:sandbox:rwx" in joined, (
            f"secrets/ grant must be dir-level u:<daemon>:rwx (provisioning write for BUG-B): "
            f"{joined}"
        )

        # No recursive grant on secrets/ anywhere in the plan. Match
        # description prefixes that begin "secrets " (avoid false-matching
        # paths that contain "secrets" as a substring).
        for action in plan:
            if action.description.startswith("secrets "):
                assert "-R" not in action.command, (
                    f"secrets/ entry must not be recursive (cluster-1 retires temp 0b35a53): "
                    f"{action.command!r} {action.description!r}"
                )

        # Default ACL granting daemon r on inherited entries — belt-and-
        # suspenders for any future write path that does NOT chmod-after-
        # create (per the requirement description).
        default_entries = [a for a in plan if "secrets default ACL" in a.description]
        assert default_entries, "secrets default ACL entry missing"
        d_args = default_entries[0].command
        assert d_args[:3] == ("setfacl", "-d", "-m"), d_args
        assert any("u:sandbox:r" in tok for tok in d_args), d_args

    def test_exec_file_recipes_table_emits_mode_0500(self) -> None:
        """Cluster 1 regression for findings 8.K + 8.L: EXEC_FILE_RECIPES sibling table.

        Spec source: orchestrator-volumes' "Executable-Script File Recipes"
        requirement. The entrypoint-script kind is structurally distinct
        from RO_FILE_RECIPES (mode 0640 ro configs / 0600 secrets): mode
        0500 owner-only with the consumer as the sole reader/exec. The
        table maps ``docker/core/entrypoint.sh`` to (uid=1000, mode=0o500).
        """
        from cli.main import EXEC_FILE_RECIPES

        # Exactly one entry today: docker/core/entrypoint.sh
        entries = list(EXEC_FILE_RECIPES)
        assert entries, "EXEC_FILE_RECIPES must be non-empty"
        match = next(
            (e for e in entries if e[0] == "docker/core" and "entrypoint.sh" in e[1]),
            None,
        )
        assert match is not None, "docker/core/entrypoint.sh missing from EXEC_FILE_RECIPES"
        parent, files, consumer_uid, mode = match
        assert consumer_uid == 1000
        assert mode == 0o500, f"entrypoint mode must be 0500 owner-only; got 0o{mode:03o}"
        # Confirm executable-script kind is NOT duplicated in RO_FILE_RECIPES.
        from cli.main import RO_FILE_RECIPES

        ro_parents = [(p, files) for p, files, _, _ in RO_FILE_RECIPES]
        assert ("docker/core", ("entrypoint.sh",)) not in ro_parents, (
            "entrypoint.sh must live in EXEC_FILE_RECIPES, not RO_FILE_RECIPES"
        )

    def test_rw_file_recipes_table_includes_claude_json_at_mode_0660(self) -> None:
        """Cluster 5 ADDED requirement: RW Config File Recipes.

        Spec source: orchestrator-volumes' "RW Config File Recipes"
        requirement. Dev-created files compose mounts as ``:rw`` MUST be
        transferred to the consumer's host subuid+subgid via the helper-cp
        recipe with mode 0660 (group-rw matching consumer's primary group).
        Today the only entry is ``config/core/.claude.json`` (consumer
        agent uid 1000 inside the core container).

        Pre-fix symptom: the host file is dev-owned (host uid 1000),
        unmapped in the agent container's userns, so it presents as
        ``nobody:nobody`` ``---`` to the agent → ``echo {} > .claude.json``
        as agent fails with EACCES even though compose mount is ``:rw``.
        """
        from cli.main import EXEC_FILE_RECIPES, RO_FILE_RECIPES, RW_FILE_RECIPES

        entries = list(RW_FILE_RECIPES)
        assert entries, "RW_FILE_RECIPES must be non-empty"
        match = next(
            (e for e in entries if e[0] == "config/core" and ".claude.json" in e[1]),
            None,
        )
        assert match is not None, "config/core/.claude.json missing from RW_FILE_RECIPES"
        _parent, _files, consumer_uid, mode = match
        assert consumer_uid == 1000
        assert mode == 0o660, f".claude.json must be mode 0660 (consumer-rw); got 0o{mode:03o}"
        # RW kind MUST NOT be duplicated in RO_FILE_RECIPES or EXEC_FILE_RECIPES.
        ro_files = {(p, fname) for p, files, _, _ in RO_FILE_RECIPES for fname in files}
        exec_files = {(p, fname) for p, files, _, _ in EXEC_FILE_RECIPES for fname in files}
        assert ("config/core", ".claude.json") not in ro_files, (
            ".claude.json must live in RW_FILE_RECIPES, not RO_FILE_RECIPES"
        )
        assert ("config/core", ".claude.json") not in exec_files, (
            ".claude.json must live in RW_FILE_RECIPES, not EXEC_FILE_RECIPES"
        )

    def test_helper_cp_chown_plan_includes_rw_file_recipes(self) -> None:
        """Cluster 5: ``_helper_cp_chown_plan`` MUST iterate RW_FILE_RECIPES too.

        Without this, ``.claude.json`` stays dev-owned and the agent sees
        ``nobody:nobody`` ``---`` from inside the userns. The plan is the
        single source of truth for both the helper-cp execution phase
        (`_phase_helper_cp_chown_ro_files`) and the stop-time unlink
        (`_phase_stop_unlink_consumer_files`); RW files MUST appear in
        both via the shared plan.
        """
        from cli.main import RW_FILE_RECIPES, _helper_cp_chown_plan

        plan = _helper_cp_chown_plan("/inst", "claude-sandbox")
        # Find the RW entry for config/core/.claude.json in the plan.
        rw_entries = [
            (str(action.parent), action.files, action.mode)
            for action in plan
            if str(action.parent).endswith("/config/core") and ".claude.json" in action.files
        ]
        assert rw_entries, f"RW recipe for .claude.json missing from plan; got {plan}"
        # At least one entry must carry mode 0660 (the RW recipe).
        assert any(mode == 0o660 for _p, _f, mode in rw_entries), (
            f"RW recipe entry must use mode 0660; got {[mode for _p, _f, mode in rw_entries]}"
        )
        # Sanity: the recipe table itself drives the inclusion.
        assert any(parent == "config/core" and ".claude.json" in files for parent, files, _, _ in RW_FILE_RECIPES)

    def test_post_hydrate_daemon_read_targets_includes_rw_recipe_files(self, tmp_path: Path) -> None:
        """Cluster 5 MODIFIED requirement: Helper-CP Source Files Daemon-Readable Pre-Recipe.

        The post-hydrate setfacl pass MUST cover RW recipe source files
        (in addition to RO + EXEC + DAEMON_READ_DIRECT). Otherwise the
        helper-cp ``cp /p/.claude.json /tmp/.claude.json`` step EACCES'es
        because the daemon (host_user) lacks DAC read on the dev-owned
        source after ``write_restricted``'s fchmod resets the ACL mask.
        """
        from cli.main import _post_hydrate_daemon_read_targets

        instance_dir = tmp_path / "inst"
        (instance_dir / "config" / "core").mkdir(parents=True)
        claude_json = instance_dir / "config" / "core" / ".claude.json"
        claude_json.write_text("{}\n")

        targets = _post_hydrate_daemon_read_targets(str(instance_dir))
        assert str(claude_json) in targets, (
            f"RW recipe source .claude.json missing from post-hydrate targets; got {targets}"
        )

    def test_workspace_shared_group_runs_before_named_acl_grant(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        """Cluster 1 phase-order regression for finding 8.N.

        Spec source: orchestrator-volumes' "Workspace Shared-Group Phase
        Ordering" requirement and the MODIFIED "Phase Order Contract for
        Ownership-Sensitive Phases". The shared-group recipe MUST be
        invoked before ``_phase_acl_grant`` so ``chmod 2770`` lands on a
        non-extended-ACL inode and the ``group::`` entry propagates from
        the mode bits.
        """
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app
        from core.exceptions import SandboxExecutionError

        call_order: list[str] = []

        def _record_workspace_shared(*_a: object, **_kw: object) -> None:
            call_order.append("workspace_shared_group")

        def _record_acl_grant(*_a: object, **_kw: object) -> None:
            call_order.append("acl_grant")
            # Abort early so we don't have to mock all subsequent phases.
            raise SandboxExecutionError("stop after acl_grant")

        with (
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="pass"),
            patch("cli.main._phase_hydrate"),
            patch("cli.main._phase_workspace_shared_group", side_effect=_record_workspace_shared),
            patch("cli.main._phase_acl_grant", side_effect=_record_acl_grant),
            patch("cli.main._revoke_acls", return_value=[]),
            patch("cli.main._release_lock"),
        ):
            result = runner.invoke(app, ["start", inst])
            assert result.exit_code == 1
        # Workspace shared-group ran first; acl_grant ran second.
        assert call_order == ["workspace_shared_group", "acl_grant"], call_order

    def test_acl_grant_plan_config_dir_is_recursive_rX_not_rwX(
        self, tmp_path: Path
    ) -> None:
        """Cluster 2 retirement of cluster-1's deferred config/ rwX widening.

        Spec source: orchestrator-volumes' MODIFIED "ACL Grant Plan as
        Single Source of Truth" requirement. Temp commit ``6c3bcb4``
        (cluster 1 cherry-pick) widened ``setfacl -R u:<host_user>:rX``
        to ``rwX`` so the helper-cp cross-fs ``mv`` could host-level
        unlink the destination. With cluster 2's structural fixes:

        - The helper recipe is now ``cp+unlink+cp+chmod+chown`` running
          in-helper as in-container root with ``cap_dac_override``; the
          host DAC bypass occurs inside the helper, not at the daemon's
          host level.
        - Daemon host-level write requirement is narrowed to BUG-B
          (provisioning write on each helper-cp parent dir), satisfied
          by per-parent dir-level ``rwx`` entries (parallel to the
          ``docker/core`` and ``secrets/`` rwx grants).

        The recursive widening MUST be retired: the daemon's DAC on
        config file CONTENTS is read-only post-cluster-2.

        Pre-fix-failure recipe (recorded in tasks.md): on the cluster-1
        baseline (sync v3) ``setfacl -R -m u:<host_user>:rwX`` is the
        ``"config files"`` entry; this test fails on rwX.
        """
        from cli.main import _acl_grant_plan

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)
        (instance_dir / "docker").mkdir()
        (instance_dir / "config").mkdir()
        (instance_dir / ".sandbox.env").write_text("")

        plan = _acl_grant_plan(str(instance_dir), "sandbox")
        config_entries = [a for a in plan if a.description.startswith("config files: ")]
        assert config_entries, "config/ recursive grant entry missing"
        args = config_entries[0].command
        joined = " ".join(args)
        assert "-R" in args, f"config/ grant must be recursive: {joined}"
        assert "rwX" not in joined, (
            f"config/ recursive grant must NOT carry write (rwX retired): {joined}"
        )
        assert "u:sandbox:rX" in joined, (
            f"config/ recursive grant must be u:<daemon>:rX (read-only on contents): {joined}"
        )

    def test_acl_grant_plan_helper_cp_parents_under_config_have_dir_level_rwx(
        self, tmp_path: Path
    ) -> None:
        """Cluster 2 BUG-B grants for helper-cp parent dirs under config/.

        Spec source: orchestrator-volumes' MODIFIED "ACL Grant Plan as
        Single Source of Truth". Each ``config/<subdir>`` listed in
        ``RO_FILE_RECIPES`` must carry an effective dir-level
        ``u:<daemon>:rwx`` so the helper-cp recipe's host-level bind
        mount of ``config/<subdir>`` is mountable read-write into the
        helper, allowing the in-helper unlink+cp pair to land. Parallel
        to the existing ``docker/core`` and ``secrets/`` rwx grants.

        Pre-fix-failure recipe: on the cluster-1 baseline (sync v3)
        helper-cp parents under config/ have NO dir-level rwx entry —
        the recursive ``rwX`` was the only daemon-write coverage. This
        test fails until the per-parent grants are added.
        """
        from cli.main import _acl_grant_plan

        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        instance_dir.mkdir(parents=True)
        (instance_dir / "docker").mkdir()
        (instance_dir / "config").mkdir()
        (instance_dir / ".sandbox.env").write_text("")

        plan = _acl_grant_plan(str(instance_dir), "sandbox")
        for parent_rel in (
            "config/coredns",
            "config/dnsdist",
            "config/proxy",
            "config/core",
        ):
            parent_abs = str(instance_dir / parent_rel)
            entries = [
                a.command
                for a in plan
                if a.description == f"helper-cp parent: {parent_abs}"
            ]
            assert entries, (
                f"missing helper-cp parent dir-level rwx grant for {parent_rel}"
            )
            args = entries[0]
            assert "-R" not in args, (
                f"helper-cp parent {parent_rel} must NOT be recursive: {args!r}"
            )
            assert any(arg == "u:sandbox:rwx" for arg in args), (
                f"helper-cp parent {parent_rel} missing u:<daemon>:rwx (BUG-B)"
            )


@pytest.mark.usefixtures("stub_bridge_resolution")
class TestDryRunHelperMkdirPlanFallback:
    """Dry-run preview reports gracefully when subuid resolver fails."""

    def test_dry_run_handles_no_subuid_range(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        from cli.main import app
        from core.host_config import NoSubuidRangeError

        inst = "myproject"
        _register_instance(inst)
        _write_ipam("myproject", 0)
        _create_tooling_plane(mock_sandbox_ai_home)

        with (
            patch(
                "cli.main.host_id_for_in_container",
                side_effect=NoSubuidRangeError("no entry for 'sandbox'"),
            ),
        ):
            result = runner.invoke(app, ["start", inst, "--dry-run"])
            # Dry-run does not crash; reports the unavailability.
            assert "helper-mkdir plan unavailable" in result.output


class TestWorkspaceSharedGroup:
    """Section 9: workspace shared-group recipe (chgrp + chmod 2770 + setfacl)."""

    def test_drift_detection_first_run_triggers_recursive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cli.main import _phase_workspace_shared_group
        from core.host_config import HostSettings

        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "a.txt").write_text("hello")
        (ws / "sub").mkdir()
        (ws / "sub" / "b.txt").write_text("world")

        monkeypatch.setattr("cli.main.workspace_bridge_gid", lambda h: os.stat(ws).st_gid)
        # Drift: workspace lacks setgid bit initially → recursive path runs.
        monkeypatch.setattr("cli.main._workspace_needs_recursive_setup", lambda *a, **k: True)

        recursive_calls: list[tuple[str, int]] = []

        def _fake_recursive(path: str, gid: int) -> tuple[int, list[str]]:
            recursive_calls.append((path, gid))
            return 0, []

        monkeypatch.setattr("cli.main._workspace_shared_group_recursive", _fake_recursive)
        monkeypatch.setattr("cli.main.subprocess.run", lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""))

        host = HostSettings(docker_unprivileged_user="claude-sandbox")
        _phase_workspace_shared_group(str(ws), host, "dev")
        assert recursive_calls == [(str(ws), os.stat(ws).st_gid)]

    def test_steady_state_skips_recursive(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from cli.main import _phase_workspace_shared_group
        from core.host_config import HostSettings

        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setattr("cli.main.workspace_bridge_gid", lambda h: os.stat(ws).st_gid)
        monkeypatch.setattr("cli.main._workspace_needs_recursive_setup", lambda *a, **k: False)
        called: list[bool] = []

        def _track(*a: object, **k: object) -> tuple[int, list[str]]:
            called.append(True)
            return 0, []

        monkeypatch.setattr("cli.main._workspace_shared_group_recursive", _track)
        monkeypatch.setattr("cli.main.subprocess.run", lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""))

        host = HostSettings(docker_unprivileged_user="claude-sandbox")
        _phase_workspace_shared_group(str(ws), host, "dev")
        assert called == []

    def test_recursive_failure_aggregated_warning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        captured_console: typing.Any,
    ) -> None:
        """Per-file failures from the recursive helper are aggregated into a warning."""
        from cli import main as main_mod
        from cli.main import _phase_workspace_shared_group
        from core.host_config import HostSettings

        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setattr("cli.main.workspace_bridge_gid", lambda h: os.stat(ws).st_gid)
        monkeypatch.setattr("cli.main._workspace_needs_recursive_setup", lambda *a, **k: True)
        monkeypatch.setattr(
            "cli.main._workspace_shared_group_recursive",
            lambda *a, **k: (3, ["/ws/root.bin", "/ws/x", "/ws/y"]),
        )
        monkeypatch.setattr("cli.main.subprocess.run", lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""))
        # Redirect the module-level Rich Console to the test buffer so we can
        # actually inspect the warning text.
        monkeypatch.setattr(main_mod, "console", captured_console.console)

        host = HostSettings(docker_unprivileged_user="claude-sandbox")
        _phase_workspace_shared_group(str(ws), host, "dev")
        out = captured_console.plain_output
        assert "3 file(s) skipped" in out
        # Sample paths surface in the warning.
        assert "/ws/root.bin" in out

    def test_setfacl_failure_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from cli.main import _phase_workspace_shared_group
        from core.exceptions import SandboxExecutionError
        from core.host_config import HostSettings

        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setattr("cli.main.workspace_bridge_gid", lambda h: os.stat(ws).st_gid)
        monkeypatch.setattr("cli.main._workspace_needs_recursive_setup", lambda *a, **k: False)

        def _raise(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.CalledProcessError(1, cmd, stderr="permission denied")

        monkeypatch.setattr("cli.main.subprocess.run", _raise)

        host = HostSettings(docker_unprivileged_user="claude-sandbox")
        with pytest.raises(SandboxExecutionError, match="permission denied"):
            _phase_workspace_shared_group(str(ws), host, "dev")

    def test_missing_bridge_group_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from cli.main import _phase_workspace_shared_group
        from core.host_config import HostSettings, WorkspaceBridgeGroupMissingError

        ws = tmp_path / "ws"
        ws.mkdir()

        def _raise(host: object) -> int:
            raise WorkspaceBridgeGroupMissingError("group missing")

        monkeypatch.setattr("cli.main.workspace_bridge_gid", _raise)
        host = HostSettings(docker_unprivileged_user="claude-sandbox")
        with pytest.raises(WorkspaceBridgeGroupMissingError):
            _phase_workspace_shared_group(str(ws), host, "dev")

    def test_recursive_chmod_failure_collected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """chown succeeds but chmod fails → failure counted, not raised."""
        from cli.main import _workspace_shared_group_recursive

        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "f.txt").write_text("x")
        monkeypatch.setattr("cli.main.os.chown", lambda *a, **k: None)

        def _raise_chmod(path: str, mode: int) -> None:
            raise PermissionError("chmod denied")

        monkeypatch.setattr("cli.main.os.chmod", _raise_chmod)
        count, sample = _workspace_shared_group_recursive(str(ws), 200500)
        assert count >= 1
        assert any("f.txt" in s for s in sample)

    def test_recursive_helper_collects_per_file_failures(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from cli.main import _workspace_shared_group_recursive

        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "ok.txt").write_text("hi")
        (ws / "bad.txt").write_text("hi")

        def _chown_selective(path: str, uid: int, gid: int, follow_symlinks: bool = True) -> None:
            if path.endswith("bad.txt"):
                raise PermissionError("nope")

        monkeypatch.setattr("cli.main.os.chown", _chown_selective)
        # Real chmod is fine on tmp_path.
        count, sample = _workspace_shared_group_recursive(str(ws), os.stat(ws).st_gid)
        assert count == 1
        assert any("bad.txt" in s for s in sample)

    def test_plan_for_dry_run_includes_all_steps(self) -> None:
        from cli.main import _workspace_shared_group_plan

        plan = _workspace_shared_group_plan("/ws", 200500, "dev", "claude-sandbox")
        ops = [a.op for a in plan]
        assert any("chgrp 200500" in op for op in ops)
        assert any("chmod 2770" in op for op in ops)
        assert any("setfacl -m u:claude-sandbox:rwx" in op for op in ops)
        # Default ACL includes both host_user and dev_user
        default_op = next(op for op in ops if "setfacl -d" in op)
        assert "u:claude-sandbox:rwx" in default_op
        assert "u:dev:rwx" in default_op

    def test_drift_helper_treats_unstattable_workspace_as_drift(self, tmp_path: Path) -> None:
        from cli.main import _workspace_needs_recursive_setup

        # Nonexistent path → os.stat raises → treat as drift.
        assert _workspace_needs_recursive_setup(str(tmp_path / "missing"), 200500) is True

    def test_drift_helper_steady_state_returns_false(self, tmp_path: Path) -> None:
        """When setgid AND group ownership match, no recursive setup is needed."""
        import stat

        from cli.main import _workspace_needs_recursive_setup

        ws = tmp_path / "ws"
        ws.mkdir()
        os.chmod(ws, ws.stat().st_mode | stat.S_ISGID)
        bridge_gid = ws.stat().st_gid
        assert _workspace_needs_recursive_setup(str(ws), bridge_gid) is False

    def test_recursive_real_walk_chmods_dirs_and_files(self, tmp_path: Path) -> None:
        """End-to-end: recursive helper sets 2770 on dirs, 0660 on files."""
        import stat

        from cli.main import _workspace_shared_group_recursive

        ws = tmp_path / "ws"
        ws.mkdir()
        sub = ws / "sub"
        sub.mkdir()
        (ws / "a.txt").write_text("hi")
        (sub / "b.txt").write_text("ho")
        # symlinks should be skipped for chmod
        (ws / "link").symlink_to("a.txt")

        count, _ = _workspace_shared_group_recursive(str(ws), ws.stat().st_gid)
        assert count == 0
        assert sub.stat().st_mode & 0o7777 == 0o2770
        assert (ws / "a.txt").stat().st_mode & 0o0777 == 0o0660
        assert stat.S_ISLNK((ws / "link").lstat().st_mode)

    def test_root_chmod_failure_wrapped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from cli.main import _phase_workspace_shared_group
        from core.exceptions import SandboxExecutionError
        from core.host_config import HostSettings

        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setattr("cli.main.workspace_bridge_gid", lambda h: 200500)
        monkeypatch.setattr("cli.main._workspace_needs_recursive_setup", lambda *a, **k: False)
        monkeypatch.setattr(
            "cli.main.os.chown",
            lambda *a, **k: (_ for _ in ()).throw(PermissionError("nope")),
        )
        host = HostSettings(docker_unprivileged_user="claude-sandbox")
        with pytest.raises(SandboxExecutionError, match="chgrp/chmod failed"):
            _phase_workspace_shared_group(str(ws), host, "dev")


@pytest.mark.usefixtures("stub_bridge_resolution")
class TestDryRunWorkspaceSharedGroupFallback:
    """Dry-run preview reports gracefully when the bridge gid lookup fails late."""

    def test_dry_run_reports_workspace_plan_unavailable(self, runner: CliRunner, mock_sandbox_ai_home: Path) -> None:
        """If build_jinja_context succeeds but bridge gid lookup fails on the
        dry-run preview path, the preview reports the issue and exits 0."""
        from cli.main import app
        from core.host_config import NoSubgidRangeError

        inst = "myproject"
        _register_instance(inst)
        _write_ipam("myproject", 0)
        _create_tooling_plane(mock_sandbox_ai_home)

        # build_jinja_context's bridge_gid lookup goes through
        # core.hydration.workspace_bridge_gid (mocked to 200000 by the autouse
        # fixture). The dry-run preview's workspace plan uses the cli.main
        # reference; making it raise here exercises the fallback branch.
        with (
            patch(
                "cli.main.workspace_bridge_gid",
                side_effect=NoSubgidRangeError("no subgid for sandbox"),
            ),
        ):
            result = runner.invoke(app, ["start", inst, "--dry-run"])
            assert "shared-group plan unavailable" in result.output


class TestHelperMkdirChownPlan:
    """Section 7: cache/log helper-mkdir+chown plan + phase."""

    def test_plan_groups_leaves_by_parent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cli.main import _helper_mkdir_chown_plan

        monkeypatch.setattr("cli.main.host_id_for_in_container", lambda n, u: 100999)
        monkeypatch.setattr("cli.main.host_gid_for_in_container", lambda n, u: 200999)
        plan = _helper_mkdir_chown_plan("/inst", "claude-sandbox")
        as_tuples = [(str(a.parent), a.leaves, a.owner_uid, a.owner_gid) for a in plan]
        # admin-reframe: cache/admin/tmux_resurrect dropped, log/admin dropped
        # (admin image has no shell / no logs); cache/core/.claude and log/core
        # remain.
        assert as_tuples == [
            ("/inst/cache/core", (".claude",), 100999, 200999),
            ("/inst/log", ("core",), 100999, 200999),
        ]

    def test_phase_sets_default_acl_then_invokes_helper(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
        monkeypatch.setattr("core.actions.helper_mkdir.helper_mkdir_chown_dirs", _fake_helper)

        _phase_helper_mkdir_chown_cache_log("/inst", "claude-sandbox", MachinectlAuth.SUDO, "dev")

        # admin-reframe 3.G.2: two parents (cache/core, log) — two setfacl
        # + two helper invocations, alternating (cache/admin dropped).
        assert [e[0] for e in events] == ["setfacl", "helper", "setfacl", "helper"]
        # First setfacl applies parent default ACL with u:dev:rwx
        cmd = events[0][1]
        assert cmd[:3] == ["setfacl", "-d", "-m"]
        assert "u:dev:rwx" in cmd[3]
        assert cmd[4] == "/inst/cache/core"

    def test_phase_idempotent_re_invocation(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
        monkeypatch.setattr("core.actions.helper_mkdir.helper_mkdir_chown_dirs", _fake_helper)

        for _ in range(2):
            _phase_helper_mkdir_chown_cache_log("/inst", "u", MachinectlAuth.SUDO, "dev")
        # admin-reframe 3.G.2: two parents x two invocations = 4 each.
        assert calls == {"setfacl": 4, "helper": 4}

    def test_phase_setfacl_failure_wrapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cli.main import _phase_helper_mkdir_chown_cache_log
        from core.exceptions import SandboxExecutionError
        from core.host_config import MachinectlAuth

        monkeypatch.setattr("cli.main.host_id_for_in_container", lambda n, u: 1)
        monkeypatch.setattr("cli.main.host_gid_for_in_container", lambda n, u: 2)

        def _raise(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.CalledProcessError(1, cmd, stderr="permission denied")

        monkeypatch.setattr("cli.main.subprocess.run", _raise)
        monkeypatch.setattr("core.actions.helper_mkdir.helper_mkdir_chown_dirs", lambda *a, **k: None)

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

    def test_ownership_after_acl_before_compose(self, runner: CliRunner) -> None:
        """Integration: phase ordering is ACL → ownership → compose."""
        inst = "myproject"
        _register_instance(inst)
        _write_ipam(inst, 0)

        from cli.main import app

        call_order: list[str] = []

        def track_acl(*a: object, **kw: object) -> None:
            call_order.append("acl_grant")

        def track_ownership(*a: object, **kw: object) -> None:
            call_order.append("credential_ownership")

        def track_compose(*a: object, **kw: object) -> None:
            call_order.append("compose_up")

        with (
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="pass"),
            patch("cli.main._phase_hydrate"),
            patch("cli.main._phase_acl_grant", side_effect=track_acl),
            patch("cli.main._phase_helper_mkdir_chown_cache_log"),
            patch("cli.main._phase_grant_post_hydrate_daemon_read"),
            patch("cli.main._phase_helper_cp_chown_ro_files", side_effect=track_ownership),
            patch("cli.main._phase_workspace_shared_group"),
            patch("cli.main._phase_compose_up", side_effect=track_compose),
            patch("cli.main._build_attach_argv", return_value=["/bin/true"]),
            patch(
                "cli.main.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["/bin/true"], returncode=0),
            ),
            patch("cli.main._release_lock"),
        ):
            result = runner.invoke(app, ["start", inst])
            assert result.exit_code == 0
            assert call_order == ["acl_grant", "credential_ownership", "compose_up"], (
                f"Expected ACL → ownership → compose, got: {call_order}"
            )


class TestStartErrorHandlerACLCleanup:
    """Task 5.7: start error handler — ACL cleanup conditional on acl_granted."""

    def test_compose_failure_triggers_revoke(self, runner: CliRunner) -> None:
        """WHEN Phase 6 fails, THEN _revoke_acls is called (acl_granted=True)."""
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app
        from core.exceptions import SandboxExecutionError

        with (
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="pass"),
            patch("cli.main._phase_hydrate"),
            patch("cli.main._phase_acl_grant"),
            patch("cli.main._phase_helper_cp_chown_ro_files"),
            patch("cli.main._phase_workspace_shared_group"),
            patch("cli.main._phase_helper_mkdir_chown_cache_log"),
            patch("cli.main._phase_grant_post_hydrate_daemon_read"),
            patch("cli.main._phase_compose_up", side_effect=SandboxExecutionError("unhealthy")),
            patch("cli.main._revoke_acls", return_value=[]) as mock_revoke,
            patch("cli.main._release_lock"),
        ):
            result = runner.invoke(app, ["start", inst])
            assert result.exit_code == 1
            mock_revoke.assert_called_once()

    def test_phase_5b_failure_triggers_revoke(self, runner: CliRunner) -> None:
        """WHEN Phase 5b (_phase_credential_ownership) fails, THEN _revoke_acls is called."""
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app
        from core.exceptions import SandboxExecutionError

        with (
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="pass"),
            patch("cli.main._phase_hydrate"),
            patch("cli.main._phase_workspace_shared_group"),
            patch("cli.main._phase_acl_grant"),
            patch("cli.main._phase_helper_cp_chown_ro_files", side_effect=SandboxExecutionError("chown failed")),
            patch("cli.main._revoke_acls", return_value=[]) as mock_revoke,
            patch("cli.main._release_lock"),
        ):
            result = runner.invoke(app, ["start", inst])
            assert result.exit_code == 1
            mock_revoke.assert_called_once()

    def test_helper_mkdir_chown_failure_triggers_revoke(self, runner: CliRunner) -> None:
        """Section 10: cache/log helper-mkdir+chown failure → ACL cleanup runs."""
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app
        from core.exceptions import SandboxExecutionError

        with (
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="pass"),
            patch("cli.main._phase_hydrate"),
            patch("cli.main._phase_workspace_shared_group"),
            patch("cli.main._phase_acl_grant"),
            patch(
                "cli.main._phase_helper_mkdir_chown_cache_log",
                side_effect=SandboxExecutionError("helper-mkdir failed"),
            ),
            patch("cli.main._revoke_acls", return_value=[]) as mock_revoke,
            patch("cli.main._release_lock"),
        ):
            result = runner.invoke(app, ["start", inst])
            assert result.exit_code == 1
            mock_revoke.assert_called_once()

    def test_workspace_shared_group_failure_does_not_trigger_revoke(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        """Section 10 + cluster-1 reorder: workspace shared-group runs BEFORE Phase-5
        named-ACL grants per the "Workspace Shared-Group Phase Ordering" requirement,
        so a workspace failure precedes ``acl_granted=True`` and no ACLs exist
        to revoke. Test inverts the prior post-Phase-5-only assumption.
        """
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app
        from core.exceptions import SandboxExecutionError

        with (
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="pass"),
            patch("cli.main._phase_hydrate"),
            patch(
                "cli.main._phase_workspace_shared_group",
                side_effect=SandboxExecutionError("workspace setup failed"),
            ),
            patch("cli.main._phase_acl_grant"),
            patch("cli.main._phase_helper_mkdir_chown_cache_log"),
            patch("cli.main._phase_grant_post_hydrate_daemon_read"),
            patch("cli.main._phase_helper_cp_chown_ro_files"),
            patch("cli.main._revoke_acls", return_value=[]) as mock_revoke,
            patch("cli.main._release_lock"),
        ):
            result = runner.invoke(app, ["start", inst])
            assert result.exit_code == 1
            mock_revoke.assert_not_called()

    def test_ipam_failure_does_not_trigger_revoke(self, runner: CliRunner) -> None:
        """WHEN IPAM fails (pre-Phase-5), THEN _revoke_acls is NOT called."""
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app
        from core.ipam import IPAMExhaustedError

        with (
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", side_effect=IPAMExhaustedError("full")),
            patch("cli.main._revoke_acls") as mock_revoke,
            patch("cli.main._release_lock"),
        ):
            result = runner.invoke(app, ["start", inst])
            assert result.exit_code == 1
            mock_revoke.assert_not_called()


class TestStopLock:
    """Task 7.3: stop lock — acquired on success, BlockingIOError fails fast."""

    def test_stop_acquires_lock(self, runner: CliRunner) -> None:
        """WHEN stop succeeds, THEN lock is acquired and released."""
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app

        with (
            patch("cli.main._warm_check", return_value=True),
            patch("cli.main._acquire_state_lock", return_value=99) as mock_lock,
            patch("cli.main._compose_down"),
            patch("cli.main._revoke_acls", return_value=[]),
            patch("cli.main._phase_stop_unlink_consumer_files", return_value=[]),
            patch("cli.main._release_lock") as mock_release,
        ):
            result = runner.invoke(app, ["stop", inst])
            assert result.exit_code == 0
            mock_lock.assert_called_once()
            mock_release.assert_called_once()

    def test_stop_lock_contention_exits(self, runner: CliRunner) -> None:
        """WHEN BlockingIOError raised, THEN exit code 1."""
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app

        with (
            patch("cli.main._warm_check", return_value=True),
            patch("cli.main._acquire_state_lock", side_effect=BlockingIOError("locked")),
            patch("cli.main._compose_down") as mock_down,
        ):
            result = runner.invoke(app, ["stop", inst])
            assert result.exit_code == 1
            assert "already in progress" in result.output.lower()
            mock_down.assert_not_called()


class TestDestroyFaultIsolation:
    """Task 8.6: destroy fault isolation — each phase independently handled."""

    def test_compose_down_failure_continues(self, runner: CliRunner) -> None:
        """WHEN compose-down fails, THEN destroy continues to rmtree/IPAM/registry."""
        inst = "myproject"
        _register_instance(inst)
        _write_ipam(inst, 0)

        from cli.main import app
        from core.exceptions import SandboxExecutionError

        with (
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._compose_down", side_effect=SandboxExecutionError("timeout")),
            patch("cli.main._revoke_acls", return_value=[]),
            patch("cli.main._phase_stop_unlink_consumer_files", return_value=[]),
            patch("cli.main._release_lock"),
            patch("shutil.rmtree") as mock_rmtree,
        ):
            result = runner.invoke(app, ["destroy", inst, "--force", "--backup-workspaces=none"])
            assert result.exit_code == 0
            # rmtree still called despite compose failure (instance + workspace).
            assert mock_rmtree.call_count >= 1
            # Warning emitted
            assert "warning" in result.output.lower() or "⚠" in result.output

    def test_rmtree_file_not_found_silenced(self, runner: CliRunner) -> None:
        """WHEN rmtree raises FileNotFoundError, THEN silenced (idempotent)."""
        inst = "myproject"
        _register_instance(inst)
        _write_ipam(inst, 0)

        from cli.main import app

        with (
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._compose_down"),
            patch("cli.main._revoke_acls", return_value=[]),
            patch("cli.main._phase_stop_unlink_consumer_files", return_value=[]),
            patch("cli.main._release_lock"),
            patch("shutil.rmtree", side_effect=FileNotFoundError("gone")),
        ):
            result = runner.invoke(app, ["destroy", inst, "--force", "--backup-workspaces=none"])
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
                    "host_uid": "1000",
                },
                "workspaces": {"main": {"bootstrap_mode": "empty", "path": "/x"}},
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
                    "host_uid": "1000",
                },
                "workspaces": {"main": {"bootstrap_mode": "empty", "path": "/x"}},
                "components": {"mcp_firecrawl": False, "mcp_puppeteer": False},
                "components_db_postgres": {"enabled": False},
            }
        )

        # Mix of blank, malformed, and valid NDJSON — blank between content lines survives strip()
        stdout = 'NOT-JSON\n   \n{"Name":"c1","Service":"svc","State":"running","Health":"","Status":"Up"}'

        from core.dispatch import ProbeOutcome

        with patch(
            "cli.main.dispatch.probe",
            return_value=ProbeOutcome(ok=True, timed_out=False, stdout=stdout, message=""),
        ):
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

    def test_stop_emits_acl_warnings(self, runner: CliRunner) -> None:
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app

        with (
            patch("cli.main._warm_check", return_value=True),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._compose_down"),
            patch("cli.main._revoke_acls", return_value=["ACL revoke warning for test: fail"]),
            patch("cli.main._phase_stop_unlink_consumer_files", return_value=[]),
            patch("cli.main._release_lock"),
        ):
            result = runner.invoke(app, ["stop", inst])
            assert result.exit_code == 0
            assert "ACL revoke warning" in result.output




class TestStopUnlinkConsumerFiles:
    """Recipe-symmetry partner of helper-cp+chown phase: stop unlinks consumer files."""

    def test_unlink_removes_existing_files(self, tmp_path: Path) -> None:
        from cli.main import _phase_stop_unlink_consumer_files

        instance_dir = tmp_path / "inst"
        instance_dir.mkdir()
        (instance_dir / "config" / "coredns").mkdir(parents=True)
        (instance_dir / "config" / "proxy").mkdir(parents=True)
        (instance_dir / "config" / "coredns" / "Corefile").write_text("x")
        (instance_dir / "config" / "proxy" / "squid.conf").write_text("y")
        # `missing.txt` deliberately not created — covers FileNotFoundError branch.

        from core.actions import HelperCpChownAction

        with patch(
            "cli.main._helper_cp_chown_plan",
            return_value=[
                HelperCpChownAction(
                    parent=instance_dir / "config" / "coredns",
                    files=("Corefile",),
                    owner_uid=0,
                    owner_gid=0,
                    mode=0o640,
                ),
                HelperCpChownAction(
                    parent=instance_dir / "config" / "proxy",
                    files=("squid.conf", "missing.txt"),
                    owner_uid=0,
                    owner_gid=0,
                    mode=0o640,
                ),
            ],
        ):
            warnings = _phase_stop_unlink_consumer_files(str(instance_dir), "claude-sandbox")
        assert warnings == []
        assert not (instance_dir / "config" / "coredns" / "Corefile").exists()
        assert not (instance_dir / "config" / "proxy" / "squid.conf").exists()

    def test_stop_emits_unlink_warnings(self, runner: CliRunner) -> None:
        """stop emits warnings returned by _phase_stop_unlink_consumer_files."""
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app

        with (
            patch("cli.main._warm_check", return_value=True),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._compose_down"),
            patch("cli.main._revoke_acls", return_value=[]),
            patch(
                "cli.main._phase_stop_unlink_consumer_files",
                return_value=["unlink /x: stale lock"],
            ),
            patch("cli.main._release_lock"),
        ):
            result = runner.invoke(app, ["stop", inst])
            assert result.exit_code == 0
            assert "unlink /x: stale lock" in result.output

    def test_unlink_aggregates_oserror_warnings(self, tmp_path: Path) -> None:
        from cli.main import _phase_stop_unlink_consumer_files

        instance_dir = tmp_path / "inst"
        instance_dir.mkdir()
        from core.actions import HelperCpChownAction

        with (
            patch(
                "cli.main._helper_cp_chown_plan",
                return_value=[
                    HelperCpChownAction(
                        parent=instance_dir / "config" / "coredns",
                        files=("Corefile",),
                        owner_uid=0,
                        owner_gid=0,
                        mode=0o640,
                    ),
                ],
            ),
            patch("cli.main.os.unlink", side_effect=PermissionError("locked down")),
        ):
            warnings = _phase_stop_unlink_consumer_files(str(instance_dir), "claude-sandbox")
            assert len(warnings) == 1
            assert "Corefile" in warnings[0]
            assert "locked down" in warnings[0]


class TestCluster3TeardownSymmetry:
    """Cluster 3 (orchestrator-volumes-teardown-symmetry) — regression tests
    for the structural fixes: revoke-plan excludes persistent grants, env-file
    ACL is persistent, shared `_phase_stop_teardown` between stop+destroy.
    """

    def test_revoke_plan_excludes_helper_cp_managed_paths(self, tmp_path: Path) -> None:
        """ADDED requirement: Acl Revoke Plan Excludes Persistent Grants.

        The revoke plan MUST NOT contain entries that recurse into helper-cp
        parent directories (the recursive `setfacl -R -x` would EPERM on
        consumer-uid-owned files inside).
        """
        from cli.main import RO_FILE_RECIPES, _acl_revoke_plan

        instance_dir = tmp_path / "inst"
        instance_dir.mkdir()
        plan = _acl_revoke_plan(str(instance_dir), "sandbox")
        helper_cp_parents = {
            os.path.join(str(instance_dir), parent).rstrip("/") for parent, _, _, _ in RO_FILE_RECIPES
        }
        for action in plan:
            if "-R" not in action.command:
                continue
            target = action.command[-1].rstrip("/")
            assert target not in helper_cp_parents, (
                f"revoke plan contains recursive entry on helper-cp parent {target}: {action.description}"
            )

    def test_revoke_plan_excludes_recursive_walks(self, tmp_path: Path) -> None:
        """The revoke plan MUST NOT use `-R` recursive flag at all post-cluster-3.

        The pre-cluster-3 plan recursively setfacl-x'd `docker/` and `config/`,
        which walked into consumer-owned files (helper-cp recipe + EXEC_FILE_RECIPES)
        and emitted EPERM warnings on every entry. Cluster 3's structural fix:
        revoke is dir-level only; helper-cp managed files are unlinked separately
        by `_phase_stop_unlink_consumer_files`.
        """
        from cli.main import _acl_revoke_plan

        plan = _acl_revoke_plan("/inst", "sandbox", ["/home/user/proj"])
        for action in plan:
            assert "-R" not in action.command, (
                f"revoke plan contains recursive walk: {action.description} → {action.command}"
            )

    def test_revoke_plan_excludes_ancestor_traverse(self, tmp_path: Path) -> None:
        """Ancestor traverse ACLs are `granted-once, persistent` and MUST NOT
        appear in the revoke plan (re-revoking would break the next start's
        ability to traverse to the instance dir).
        """
        from cli.main import _acl_revoke_plan

        plan = _acl_revoke_plan("/inst", "sandbox", ["/home/user/proj"])
        for action in plan:
            assert "ancestor" not in action.description

    def test_revoke_plan_excludes_workspace_shared_group_persistent(self, tmp_path: Path) -> None:
        """Workspace shared-group state (chgrp/chmod 2770/setgid + g::rwx
        default) is `granted-once, persistent` and MUST NOT appear in the
        revoke plan. Only the per-user named-ACL portion is revoked.
        """
        from cli.main import _acl_revoke_plan

        ws = tmp_path / "ws"
        plan = _acl_revoke_plan("/inst", "sandbox", [str(ws)])
        joined_descs = " ".join(a.description for a in plan)
        assert "chgrp" not in joined_descs
        assert "setgid" not in joined_descs
        for action in plan:
            if "-d" in action.command and "workspace" in action.description:
                assert "-x" in action.command
                idx = action.command.index("-x")
                target = action.command[idx + 1]
                assert target.startswith("u:"), (
                    f"workspace default revoke not named-entry-only: {action.command}"
                )

    def test_revoke_plan_excludes_env_file_persistent(self, tmp_path: Path) -> None:
        """The .sandbox.env named-ACL is `granted-once, persistent` (cluster 3
        — Environment File Read ACL) and MUST NOT appear in the revoke plan.
        It survives stop and is removed only by destroy's rmtree.
        """
        from cli.main import _acl_revoke_plan

        plan = _acl_revoke_plan("/inst", "sandbox")
        for action in plan:
            assert ".sandbox.env" not in action.description, (
                f"revoke plan touches .sandbox.env: {action.description}"
            )
            assert not any(".sandbox.env" in a for a in action.command), (
                f"revoke plan args touch .sandbox.env: {action.command}"
            )

    def test_phase_stop_teardown_orders_compose_then_unlink_then_revoke(self, tmp_path: Path) -> None:
        """ADDED requirement: Teardown Sequence.

        `_phase_stop_teardown` MUST invoke compose-down → unlink-helper-cp-managed
        → revoke-acls in that order. Verifies the ordering invariant via mock
        call sequence (not just presence).
        """
        from cli.main import _phase_stop_teardown
        from core.host_config import MachinectlAuth

        calls: list[str] = []
        config = MagicMock()

        def _track_compose(*_a: object, **_kw: object) -> None:
            calls.append("compose_down")

        def _track_unlink(*_a: object, **_kw: object) -> list[str]:
            calls.append("unlink")
            return []

        def _track_revoke(*_a: object, **_kw: object) -> list[str]:
            calls.append("revoke")
            return []

        with (
            patch("cli.main._compose_down", side_effect=_track_compose),
            patch("cli.main._phase_stop_unlink_consumer_files", side_effect=_track_unlink),
            patch("cli.main._revoke_acls", side_effect=_track_revoke),
        ):
            _phase_stop_teardown(
                "/inst", "proj", "sandbox", config, ["/ws"], volumes=False, auth=MachinectlAuth.SUDO
            )
        assert calls == ["compose_down", "unlink", "revoke"]

    def test_stop_invokes_phase_stop_teardown(self, runner: CliRunner) -> None:
        """`sandbox stop` delegates to `_phase_stop_teardown` (shared helper)."""
        inst = "myproject"
        _register_instance(inst)
        from cli.main import app

        with (
            patch("cli.main._warm_check", return_value=True),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_stop_teardown", return_value=[]) as mock_teardown,
            patch("cli.main._release_lock"),
        ):
            result = runner.invoke(app, ["stop", inst])
        assert result.exit_code == 0, result.output
        mock_teardown.assert_called_once()
        assert mock_teardown.call_args.kwargs["volumes"] is False

    def test_stop_clean_passes_volumes_true_to_teardown(self, runner: CliRunner) -> None:
        """`sandbox stop --clean` propagates volumes=True to the shared helper."""
        inst = "myproject"
        _register_instance(inst)
        from cli.main import app

        with (
            patch("cli.main._warm_check", return_value=True),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_stop_teardown", return_value=[]) as mock_teardown,
            patch("cli.main._release_lock"),
        ):
            result = runner.invoke(app, ["stop", inst, "--clean"])
        assert result.exit_code == 0, result.output
        assert mock_teardown.call_args.kwargs["volumes"] is True

    def test_destroy_invokes_phase_stop_teardown(self, runner: CliRunner) -> None:
        """`sandbox destroy` D5+D6 delegates to `_phase_stop_teardown`."""
        inst = "myproject"
        _register_instance(inst)
        _write_ipam(inst, 0)
        from cli.main import app

        with (
            patch("cli.main._compose_down"),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._release_lock"),
            patch("cli.main._phase_stop_teardown", return_value=[]) as mock_teardown,
        ):
            result = runner.invoke(app, ["destroy", inst, "--force", "--backup-workspaces=none"])
        assert result.exit_code == 0, result.output
        assert mock_teardown.call_args.kwargs["volumes"] is True

    def test_container_status_does_not_re_grant_env_acl(self, tmp_path: Path) -> None:
        """`_container_status` MUST NOT defensively re-grant the .sandbox.env
        ACL — that ACL is now persistent, so the temp `_ensure_env_file_readable_by_daemon`
        ritual was retired. Verifies absence of the helper symbol.
        """
        import cli.main as main_mod

        assert not hasattr(main_mod, "_ensure_env_file_readable_by_daemon")

    def test_acl_revoke_plan_is_strict_subset_of_grant_plan_paths(self, tmp_path: Path) -> None:
        """Cross-cutting invariant: every `setfacl -x` target in the revoke
        plan SHALL correspond to a `setfacl -m` target in the grant plan
        (asymmetry direction: grant ⊇ revoke). Persistent grants are in grant
        but not revoke; the reverse MUST NOT occur.
        """
        from cli.main import _acl_grant_plan, _acl_revoke_plan

        instance_dir = tmp_path / "inst"
        instance_dir.mkdir()
        ws = tmp_path / "ws"
        ws.mkdir()
        grant = _acl_grant_plan(str(instance_dir), "sandbox", [str(ws)], dev_user="dev")
        revoke = _acl_revoke_plan(str(instance_dir), "sandbox", [str(ws)])
        grant_targets = {a.command[-1] for a in grant}
        for action in revoke:
            assert action.command[-1] in grant_targets, (
                f"revoke target {action.command[-1]!r} ({action.description}) "
                f"is not in grant plan — asymmetry direction violated"
            )


class TestDestroyFaultIsolationWarnings:
    """Cover L1027, L1041-1042, L1049-1050: destroy ACL/IPAM/registry warnings."""

    def test_destroy_emits_acl_warnings(self, runner: CliRunner) -> None:
        """L1027: ACL revoke warnings emitted during destroy."""
        inst = "myproject"
        _register_instance(inst)
        _write_ipam(inst, 0)

        from cli.main import app

        with (
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._compose_down"),
            patch("cli.main._revoke_acls", return_value=["ACL warning: test"]),
            patch("cli.main._phase_stop_unlink_consumer_files", return_value=[]),
            patch("cli.main._release_lock"),
            patch("shutil.rmtree"),
        ):
            result = runner.invoke(app, ["destroy", inst, "--force", "--backup-workspaces=none"])
            assert result.exit_code == 0
            assert "ACL warning" in result.output

    def test_destroy_ipam_failure_emits_warning(self, runner: CliRunner) -> None:
        """L1041-1042: IPAM release failure emits warning, doesn't abort."""
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app

        with (
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._compose_down"),
            patch("cli.main._revoke_acls", return_value=[]),
            patch("cli.main._phase_stop_unlink_consumer_files", return_value=[]),
            patch("cli.main._release_lock"),
            patch("shutil.rmtree"),
            patch("cli.main.IPAMLedger.release", side_effect=RuntimeError("corrupt")),
        ):
            result = runner.invoke(app, ["destroy", inst, "--force", "--backup-workspaces=none"])
            assert result.exit_code == 0
            assert "IPAM release warning" in result.output

    def test_destroy_registry_failure_emits_warning(self, runner: CliRunner) -> None:
        """L1049-1050: Registry cleanup failure emits warning, doesn't abort."""
        inst = "myproject"
        _register_instance(inst)
        _write_ipam(inst, 0)

        from cli.main import app

        with (
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._compose_down"),
            patch("cli.main._revoke_acls", return_value=[]),
            patch("cli.main._phase_stop_unlink_consumer_files", return_value=[]),
            patch("cli.main._release_lock"),
            patch("shutil.rmtree"),
            patch("cli.main.InstanceRegistry.remove", side_effect=KeyError("not found")),
        ):
            result = runner.invoke(app, ["destroy", inst, "--force", "--backup-workspaces=none"])
            assert result.exit_code == 0
            assert "Registry cleanup warning" in result.output


class TestDestroyBackupWorkspacesSpec:
    """Direct unit tests for `_resolve_backup_workspaces_spec`."""

    def test_all_returns_sorted_workspaces(self) -> None:
        from cli.main import _resolve_backup_workspaces_spec

        assert _resolve_backup_workspaces_spec("all", {"b", "a", "c"}) == ["a", "b", "c"]

    def test_none_returns_empty(self) -> None:
        from cli.main import _resolve_backup_workspaces_spec

        assert _resolve_backup_workspaces_spec("none", {"a", "b"}) == []

    def test_csv_returns_listed(self) -> None:
        from cli.main import _resolve_backup_workspaces_spec

        assert _resolve_backup_workspaces_spec("a,c", {"a", "b", "c"}) == ["a", "c"]

    def test_all_plus_named_rejected(self) -> None:
        import typer as _typer
        from cli.main import _resolve_backup_workspaces_spec

        with pytest.raises(_typer.BadParameter, match="cannot combine 'all'"):
            _resolve_backup_workspaces_spec("all,a", {"a"})

    def test_none_plus_named_rejected(self) -> None:
        import typer as _typer
        from cli.main import _resolve_backup_workspaces_spec

        with pytest.raises(_typer.BadParameter, match="cannot combine 'none'"):
            _resolve_backup_workspaces_spec("none,a", {"a"})

    def test_unknown_name_rejected(self) -> None:
        import typer as _typer
        from cli.main import _resolve_backup_workspaces_spec

        with pytest.raises(_typer.BadParameter, match="not found"):
            _resolve_backup_workspaces_spec("missing,also_missing", {"main"})


class TestDestroyBackupFlows:
    """Phase D4 backup loop (success + failure) and TTY no-flag prompt."""

    def test_backup_workspaces_all_invokes_create_backup(self, runner: CliRunner) -> None:
        inst = "myproject"
        _register_instance(inst)
        _write_ipam(inst, 0)

        from cli.main import app

        with (
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._compose_down"),
            patch("cli.main._revoke_acls", return_value=[]),
            patch("cli.main._phase_stop_unlink_consumer_files", return_value=[]),
            patch("cli.main._release_lock"),
            patch("shutil.rmtree"),
            patch("cli.main.create_backup") as mock_backup,
            patch("cli.main.acquire_backup_lock") as mock_acquire,
        ):
            mock_acquire.return_value.__enter__ = lambda self: None
            mock_acquire.return_value.__exit__ = lambda self, *a: None
            result = runner.invoke(app, ["destroy", inst, "--force", "--backup-workspaces=all"])
            assert result.exit_code == 0, result.output
            assert mock_backup.call_count == 1  # one workspace ("main")
            assert mock_backup.call_args.kwargs["acquire_lock"] is False

    def test_backup_failure_aborts_destroy(self, runner: CliRunner) -> None:
        from cli.main import app
        from core.workspace_backups import BackupRsyncError

        inst = "myproject"
        _register_instance(inst)
        _write_ipam(inst, 0)

        with (
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._compose_down") as mock_down,
            patch("cli.main._release_lock"),
            patch("shutil.rmtree") as mock_rmtree,
            patch("cli.main.create_backup", side_effect=BackupRsyncError("disk full")),
            patch("cli.main.acquire_backup_lock") as mock_acquire,
        ):
            mock_acquire.return_value.__enter__ = lambda self: None
            mock_acquire.return_value.__exit__ = lambda self, *a: None
            result = runner.invoke(app, ["destroy", inst, "--force", "--backup-workspaces=all"])
            assert result.exit_code == 1
            assert "destroy aborted" in result.output.lower()
            # D3 compose down ran; D5 (compose down -v) and D7+ did NOT.
            assert mock_down.call_count == 1
            mock_rmtree.assert_not_called()

    def test_non_tty_no_flag_refuses(self, runner: CliRunner) -> None:
        from cli.main import app

        inst = "myproject"
        _register_instance(inst)
        # CliRunner is non-TTY by default; no --backup-workspaces flag.
        result = runner.invoke(app, ["destroy", inst, "--force"])
        assert result.exit_code == 1
        assert "non-interactive mode requires --backup-workspaces" in result.output

    def test_tty_no_flag_prompts_per_workspace(self, runner: CliRunner) -> None:
        from cli.main import app

        inst = "myproject"
        _register_instance(inst)
        _write_ipam(inst, 0)

        with (
            patch("cli.main._stdin_is_tty", return_value=True),
            patch("cli.main.typer.confirm", return_value=True) as mock_confirm,
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._compose_down"),
            patch("cli.main._revoke_acls", return_value=[]),
            patch("cli.main._phase_stop_unlink_consumer_files", return_value=[]),
            patch("cli.main._release_lock"),
            patch("shutil.rmtree"),
            patch("cli.main.create_backup") as mock_backup,
            patch("cli.main.acquire_backup_lock") as mock_acquire,
        ):
            mock_acquire.return_value.__enter__ = lambda self: None
            mock_acquire.return_value.__exit__ = lambda self, *a: None
            result = runner.invoke(app, ["destroy", inst, "--force"])
            assert result.exit_code == 0, result.output
            # confirm called once per workspace (myproject has "main").
            assert mock_confirm.call_count == 1
            assert mock_backup.call_count == 1


class TestDestroyUnlinksConsumerFiles:
    """destroy invokes the same unlink-before-revoke phase as stop (Change L)."""

    def test_destroy_emits_unlink_warnings(self, runner: CliRunner) -> None:
        """destroy emits warnings returned by _phase_stop_unlink_consumer_files."""
        inst = "myproject"
        _register_instance(inst)
        _write_ipam(inst, 0)

        from cli.main import app

        with (
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._compose_down"),
            patch("cli.main._revoke_acls", return_value=[]),
            patch(
                "cli.main._phase_stop_unlink_consumer_files",
                return_value=["unlink /x: stale lock"],
            ),
            patch("cli.main._release_lock"),
            patch("shutil.rmtree"),
        ):
            result = runner.invoke(app, ["destroy", inst, "--force", "--backup-workspaces=none"])
            assert result.exit_code == 0
            assert "unlink /x: stale lock" in result.output


class TestDestroyLockContention:
    """D2 gate-lock and D5 reacquire-lock BlockingIOError paths."""

    def test_gate_lock_contention_exits(self, runner: CliRunner) -> None:
        from cli.main import app

        inst = "myproject"
        _register_instance(inst)
        with patch("cli.main._acquire_state_lock", side_effect=BlockingIOError):
            result = runner.invoke(app, ["destroy", inst, "--force", "--backup-workspaces=none"])
        assert result.exit_code == 1
        assert "already in progress" in result.output

    def test_reacquire_lock_contention_exits(self, runner: CliRunner) -> None:
        from cli.main import app

        inst = "myproject"
        _register_instance(inst)
        # First acquire (gate) succeeds; second (D5 reacquire) raises.
        with (
            patch("cli.main._acquire_state_lock", side_effect=[99, BlockingIOError()]),
            patch("cli.main._compose_down"),
            patch("cli.main._release_lock"),
            patch("cli.main.is_backup_lock_held", return_value=False),
            patch("cli.main.acquire_backup_lock") as mock_acquire,
        ):
            mock_acquire.return_value.__enter__ = lambda self: None
            mock_acquire.return_value.__exit__ = lambda self, *a: None
            result = runner.invoke(app, ["destroy", inst, "--force", "--backup-workspaces=none"])
        assert result.exit_code == 1
        assert "already in progress" in result.output


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

    def test_status_ipam_exhausted_continues(self, runner: CliRunner) -> None:
        """WHEN IPAM is exhausted, THEN status still renders (no crash)."""
        inst = "myproject"
        _register_instance(inst)

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
            patch("cli.main._container_status", return_value=containers),
            patch(
                "cli.main.IPAMLedger.peek_next_slot",
                side_effect=IPAMExhaustedError("full"),
            ),
        ):
            result = runner.invoke(app, ["status", inst])
            assert result.exit_code == 0
            # Status renders despite IPAM exhaustion
            assert "running" in result.output.lower() or "core" in result.output.lower()


# ── Section 7 (host-config-machinectl-auth): auth-mode-aware preview ──────


@pytest.mark.usefixtures("stub_bridge_resolution")
class TestDryRunAuthModePreview:
    """Task 7.7: --dry-run preview reflects machinectl_authentication mode."""

    def test_dry_run_preview_shows_polkit_command_without_sudo(
        self, runner: CliRunner, mock_sandbox_ai_home: Path, project_config_factory: HostConfigFactory
    ) -> None:
        inst = "myproject"
        _register_instance(inst)
        _write_ipam("myproject", 0)
        _create_tooling_plane(mock_sandbox_ai_home)

        from cli.main import app

        polkit_cfg = project_config_factory(user="sandbox", auth="polkit")
        with (
            patch("cli.main.HostConfig.from_toml", return_value=polkit_cfg),
        ):
            result = runner.invoke(app, ["start", inst, "--dry-run"])

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
        inst = "myproject"
        _register_instance(inst)
        _write_ipam("myproject", 0)
        _create_tooling_plane(mock_sandbox_ai_home)

        from cli.main import app

        sudo_cfg = project_config_factory(user="sandbox", auth="sudo")
        with (
            patch("cli.main.HostConfig.from_toml", return_value=sudo_cfg),
        ):
            result = runner.invoke(app, ["start", inst, "--dry-run"])

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

        inst = "myproject"
        _register_instance(inst)
        _write_ipam("myproject", 0)

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
            patch("cli.main.HostConfig.from_toml", return_value=polkit_cfg),
            patch("cli.main._container_status", side_effect=fake_container_status),
        ):
            result = runner.invoke(app, ["status", inst])

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

        inst = "myproject"
        _register_instance(inst)
        _write_ipam("myproject", 0)

        with (
            patch(
                "cli.main.HostConfig.from_toml",
                side_effect=FileNotFoundError("No sandbox-ai.toml found. Run sandbox init."),
            ),
        ):
            args = [command, inst]
            if command == "destroy":
                args.append("--force")
            result = runner.invoke(app, args)

        assert result.exit_code == 1, f"{command} should exit 1 without sandbox-ai.toml"
        assert "sandbox-ai.toml" in result.output


# ── Group 6 coverage: handover cwd, backup-lock check, attach ws selection, status views ──


class TestBuildAttachArgvCwd:
    """`_build_attach_argv` selects cwd via ssh remote-command (admin-reframe D9).

    Replaces the prior `_phase_handover(cwd_workspace=...)` ``-w /workspaces/<ws>``
    docker-exec test — Shape 2 lands the operator in core via ssh, so the
    cwd suffix is part of the ssh remote command, not a docker-exec flag.
    """

    def _argv(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ws: str
    ) -> list[str]:
        from cli.main import _build_attach_argv
        from core.host_config import HostConfig, HostSettings

        ledger_path = tmp_path / "ipam.json"
        ledger_path.write_text('{"myproj": 0}\n')
        monkeypatch.setattr("core.ipam._default_ledger_path", lambda: str(ledger_path))
        home = tmp_path / "home"
        (home / "instances" / "myproj" / "secrets").mkdir(parents=True)
        monkeypatch.setenv("SANDBOX_AI_HOME", str(home))
        cfg = HostConfig(host=HostSettings(docker_unprivileged_user="sandbox"))
        return _build_attach_argv("myproj", ws, cfg)

    def test_argv_remote_command_sets_cwd_to_workspaces_ws(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        argv = self._argv(monkeypatch, tmp_path, "api")
        assert argv[-1] == "cd /workspaces/api && exec bash -l"

    def test_argv_no_docker_exec_w_flag(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The pre-D9 implementation used ``docker exec -w /workspaces/<ws>``.
        # Post-D9 the cwd lives in the ssh remote-command suffix, not in
        # the ProxyCommand's ``docker exec`` invocation.
        argv = self._argv(monkeypatch, tmp_path, "api")
        proxy = next(a for a in argv if a.startswith("ProxyCommand="))
        assert " -w " not in proxy
        assert "/workspaces/api" not in proxy


class TestLifecycleBackupLockRefusal:
    """`start`, `stop`, `attach`, `destroy` refuse fast when ``<inst>.backup.lock`` is held."""

    def test_start_refuses_when_backup_lock_held(self, runner: CliRunner) -> None:
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app

        with (
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main.is_backup_lock_held", return_value=True),
            patch("cli.main._release_lock") as mock_release,
        ):
            result = runner.invoke(app, ["start", inst])
            assert result.exit_code == 1
            assert "backup in progress" in result.output.lower()
            mock_release.assert_called_once()

    def test_stop_refuses_when_backup_lock_held(self, runner: CliRunner) -> None:
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app

        with (
            patch("cli.main._warm_check", return_value=True),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main.is_backup_lock_held", return_value=True),
            patch("cli.main._release_lock") as mock_release,
            patch("cli.main._compose_down") as mock_down,
        ):
            result = runner.invoke(app, ["stop", inst])
            assert result.exit_code == 1
            assert "backup in progress" in result.output.lower()
            mock_release.assert_called_once()
            mock_down.assert_not_called()

    def test_attach_refuses_when_backup_lock_held(self, runner: CliRunner) -> None:
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app

        with (
            patch("cli.main.is_backup_lock_held", return_value=True),
            patch("cli.main._build_attach_argv", return_value=["/bin/true"]),
            patch(
                "cli.main.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["/bin/true"], returncode=0),
            ) as mock_handover,
        ):
            result = runner.invoke(app, ["attach", inst])
            assert result.exit_code == 1
            assert "backup in progress" in result.output.lower()
            mock_handover.assert_not_called()

    def test_destroy_refuses_when_backup_lock_held(self, runner: CliRunner) -> None:
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app

        with (
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main.is_backup_lock_held", return_value=True),
            patch("cli.main._release_lock") as mock_release,
            patch("cli.main._compose_down") as mock_down,
        ):
            result = runner.invoke(app, ["destroy", inst, "--force", "--backup-workspaces=none"])
            assert result.exit_code == 1
            assert "backup in progress" in result.output.lower()
            mock_release.assert_called_once()
            mock_down.assert_not_called()


_TWO_WORKSPACE_TOML = VALID_TOML_CONTENT.replace(
    b"""[workspaces.main]
bootstrap_mode = "copy"
source = "/home/user/myproject"
path = "/home/user/myproject"
""",
    b"""[workspaces.main]
bootstrap_mode = "empty"
path = "/home/user/.sandbox-ai/workspaces/myproject/main"

[workspaces.scratch]
bootstrap_mode = "empty"
path = "/home/user/.sandbox-ai/workspaces/myproject/scratch"
""",
)


class TestAttachWorkspaceSelection:
    """cli-attach: optional ``<ws>``; N=1 default, N>1 list with exit 1, unknown ws rejected."""

    def test_n1_omitted_defaults_to_only_workspace(self, runner: CliRunner) -> None:
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app

        with (
            patch("cli.main.is_backup_lock_held", return_value=False),
            patch("cli.main._warm_check", return_value=True),
            patch(
                "cli.main.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["/bin/true"], returncode=0),
            ),
            patch("cli.main._build_attach_argv", return_value=["/bin/true"]) as mock_build,
        ):
            result = runner.invoke(app, ["attach", inst])
            assert result.exit_code == 0
            # _build_attach_argv(inst, ws, host_config) — ws is positional arg 1.
            args, _kwargs = mock_build.call_args
            assert args[1] == "main"

    def test_n_greater_than_one_omitted_lists_and_exits(self, runner: CliRunner) -> None:
        inst = "myproject"
        instance_dir = _register_instance(inst)
        (instance_dir / "sandbox.toml").write_bytes(_TWO_WORKSPACE_TOML)

        from cli.main import app

        with (
            patch("cli.main.is_backup_lock_held", return_value=False),
            patch("cli.main._build_attach_argv", return_value=["/bin/true"]),
            patch(
                "cli.main.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["/bin/true"], returncode=0),
            ) as mock_handover,
        ):
            result = runner.invoke(app, ["attach", inst])
            assert result.exit_code == 1
            assert "main" in result.output and "scratch" in result.output
            mock_handover.assert_not_called()

    def test_unknown_workspace_rejected(self, runner: CliRunner) -> None:
        inst = "myproject"
        instance_dir = _register_instance(inst)
        (instance_dir / "sandbox.toml").write_bytes(_TWO_WORKSPACE_TOML)

        from cli.main import app

        with (
            patch("cli.main.is_backup_lock_held", return_value=False),
            patch("cli.main._build_attach_argv", return_value=["/bin/true"]),
            patch(
                "cli.main.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["/bin/true"], returncode=0),
            ) as mock_handover,
        ):
            result = runner.invoke(app, ["attach", inst, "nonexistent"])
            assert result.exit_code == 1
            assert "nonexistent" in result.output.lower() or "not found" in result.output.lower()
            mock_handover.assert_not_called()

    def test_explicit_workspace_passes_through(self, runner: CliRunner) -> None:
        inst = "myproject"
        instance_dir = _register_instance(inst)
        (instance_dir / "sandbox.toml").write_bytes(_TWO_WORKSPACE_TOML)

        from cli.main import app

        with (
            patch("cli.main.is_backup_lock_held", return_value=False),
            patch("cli.main._warm_check", return_value=True),
            patch(
                "cli.main.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["/bin/true"], returncode=0),
            ),
            patch("cli.main._build_attach_argv", return_value=["/bin/true"]) as mock_build,
        ):
            result = runner.invoke(app, ["attach", inst, "scratch"])
            assert result.exit_code == 0
            args, _kwargs = mock_build.call_args
            assert args[1] == "scratch"


class TestWorkspaceStateLabel:
    """`_workspace_state_label` returns the cli-status state column value."""

    def test_missing_path_returns_missing(self, tmp_path: Path) -> None:
        from cli.main import _workspace_state_label
        from core.host_config import HostSettings

        host = HostSettings(docker_unprivileged_user="sandbox")
        label = _workspace_state_label(str(tmp_path / "does-not-exist"), host)
        assert "missing" in label

    def test_correct_setgid_and_group_returns_ok(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from cli.main import _workspace_state_label
        from core.host_config import HostSettings

        ws = tmp_path / "ws"
        ws.mkdir()
        ws.chmod(0o2770)
        st = os.stat(ws)
        host = HostSettings(docker_unprivileged_user="sandbox")
        monkeypatch.setattr("cli.main.workspace_bridge_gid", lambda _h: st.st_gid)
        label = _workspace_state_label(str(ws), host)
        assert "ok" in label

    def test_missing_setgid_returns_drift(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from cli.main import _workspace_state_label
        from core.host_config import HostSettings

        ws = tmp_path / "ws"
        ws.mkdir()
        ws.chmod(0o0770)
        st = os.stat(ws)
        host = HostSettings(docker_unprivileged_user="sandbox")
        monkeypatch.setattr("cli.main.workspace_bridge_gid", lambda _h: st.st_gid)
        label = _workspace_state_label(str(ws), host)
        assert "drift" in label

    def test_missing_bridge_group_returns_drift(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from cli.main import _workspace_state_label
        from core.host_config import HostSettings, WorkspaceBridgeGroupMissingError

        ws = tmp_path / "ws"
        ws.mkdir()
        host = HostSettings(docker_unprivileged_user="sandbox")

        def _raise(_h: HostSettings) -> int:
            raise WorkspaceBridgeGroupMissingError("group missing")

        monkeypatch.setattr("cli.main.workspace_bridge_gid", _raise)
        label = _workspace_state_label(str(ws), host)
        assert "drift" in label


class TestWorkspaceDuSize:
    """`_workspace_du_size` returns the first whitespace-delimited token from `du -sh`."""

    def test_success_returns_size(self) -> None:
        from cli.main import _workspace_du_size

        completed = subprocess.CompletedProcess([], 0, "12K\t/some/path\n", "")
        with patch("cli.main.subprocess.run", return_value=completed):
            assert _workspace_du_size("/some/path") == "12K"

    def test_empty_stdout_returns_dash(self) -> None:
        from cli.main import _workspace_du_size

        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch("cli.main.subprocess.run", return_value=completed):
            assert _workspace_du_size("/p") == "—"

    def test_called_process_error_returns_dash(self) -> None:
        from cli.main import _workspace_du_size

        with patch("cli.main.subprocess.run", side_effect=subprocess.CalledProcessError(1, "du")):
            assert _workspace_du_size("/p") == "—"

    def test_timeout_returns_dash(self) -> None:
        from cli.main import _workspace_du_size

        with patch("cli.main.subprocess.run", side_effect=subprocess.TimeoutExpired("du", 30)):
            assert _workspace_du_size("/p") == "—"

    def test_du_not_on_path_returns_dash(self) -> None:
        from cli.main import _workspace_du_size

        with patch("cli.main.subprocess.run", side_effect=FileNotFoundError("du")):
            assert _workspace_du_size("/p") == "—"


class TestStatusSummaryView:
    """`sandbox status` (no <inst>) renders a registry-driven summary table."""

    def test_summary_empty_registry(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "no instances" in result.output.lower()

    def test_summary_with_registered_instances(self, runner: CliRunner) -> None:
        _register_instance("alpha")
        _register_instance("beta")
        _write_ipam("alpha", 0)

        from cli.main import app

        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "alpha" in result.output
        assert "beta" in result.output

    def test_summary_handles_corrupt_sandbox_toml(self, runner: CliRunner) -> None:
        """A registered instance with unparseable TOML shows '?' for workspace count."""
        instance_dir = _register_instance("broken")
        (instance_dir / "sandbox.toml").write_text("not valid toml [[[")

        from cli.main import app

        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "broken" in result.output

    def test_summary_handles_ipam_exhausted(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        """IPAMExhaustedError on peek surfaces '—' for the slot column without crashing."""
        from core.ipam import IPAMExhaustedError

        _register_instance("solo")
        from cli.main import app

        monkeypatch.setattr(
            "cli.main.IPAMLedger.peek_next_slot",
            lambda _self, _name: (_ for _ in ()).throw(IPAMExhaustedError("full")),
        )
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "solo" in result.output

    def test_status_detailed_without_inst_rejected(self, runner: CliRunner) -> None:
        from cli.main import app

        _seed_registry(_user_home())
        result = runner.invoke(app, ["status", "--detailed"])
        assert result.exit_code == 1
        assert "--detailed" in result.output


class TestStatusDetailedWorkspacesTable:
    """`sandbox status <inst>` renders a Workspaces section per cli-status spec."""

    def test_workspaces_section_rendered(self, runner: CliRunner) -> None:
        inst = "myproject"
        instance_dir = _register_instance(inst)
        (instance_dir / "sandbox.toml").write_bytes(_TWO_WORKSPACE_TOML)

        from cli.main import app

        with patch("cli.main._container_status", return_value=[]):
            result = runner.invoke(app, ["status", inst])
            assert result.exit_code == 0
            assert "Workspaces" in result.output
            assert "main" in result.output
            assert "scratch" in result.output

    def test_detailed_flag_adds_size_column(self, runner: CliRunner) -> None:
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app

        with (
            patch("cli.main._container_status", return_value=[]),
            patch("cli.main._workspace_du_size", return_value="42K"),
        ):
            result = runner.invoke(app, ["status", inst, "--detailed"])
            assert result.exit_code == 0
            assert "Size" in result.output
            assert "42K" in result.output

    def test_default_omits_size_column(self, runner: CliRunner) -> None:
        inst = "myproject"
        _register_instance(inst)

        from cli.main import app

        with (
            patch("cli.main._container_status", return_value=[]),
            patch("cli.main._workspace_du_size") as mock_du,
        ):
            result = runner.invoke(app, ["status", inst])
            assert result.exit_code == 0
            mock_du.assert_not_called()
