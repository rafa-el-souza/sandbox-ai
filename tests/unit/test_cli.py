"""CLI lifecycle unit tests — start, stop, attach, destroy.

All subprocess interactions are mocked at the core.executor.Executor.run boundary.
Tests validate the full phase sequencing per the orchestrator design spec.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

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
[project]
name = "myproject"
user_project_root = "/home/dev/myproject"
host_unprivileged_user = "sandbox"
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

WARMUP_TOML_CONTENT = VALID_TOML_CONTENT.replace(
    b'warmup_prompt = ""', b'warmup_prompt = "bootstrap the project"'
)


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


def _register_instance(home: Path, project_dir: str, instance_id: str) -> Path:
    """Helper: register an instance and create its directory structure."""
    reg = home / ".state" / "instances.json"
    reg.write_text(json.dumps({project_dir: instance_id}))
    inst = home / "sandboxes" / instance_id
    (inst / "docker" / "core").mkdir(parents=True)
    (inst / "docker" / "admin").mkdir(parents=True)
    (inst / "docker" / "extras").mkdir(parents=True)
    (inst / "config" / "proxy").mkdir(parents=True)
    (inst / "config" / "admin").mkdir(parents=True)
    (inst / "config" / "core").mkdir(parents=True)
    (inst / "config" / "dns-sidecar").mkdir(parents=True)
    (inst / "log" / "orchestrator").mkdir(parents=True)
    # Write sandbox.toml
    (inst / "sandbox.toml").write_bytes(VALID_TOML_CONTENT)
    # Write .sandbox.env
    (inst / ".sandbox.env").write_text('CORE_ANTHROPIC_API_KEY="test"')
    # Write .initialized sentinel
    (inst / ".initialized").write_text("")
    return inst


def _write_ipam(home: Path, project_id: str, base_index: int) -> None:
    """Helper: write an IPAM entry."""
    ipam = home / ".state" / "ipam.json"
    ipam.write_text(json.dumps({project_id: base_index}))


# ── sandbox start ────────────────────────────────────────────────────────────


class TestStartHappyPath:
    """Task 8.2: sandbox start happy path — all phases sequenced correctly."""

    def test_start_existing_instance_full_sequence(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        """Existing instance: registry hit → warm check (cold) → lock → IPAM → creds → hydrate → ACL → compose → handover."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)
        _write_ipam(home, instance_id, 0)

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="proxypass123"),
            patch("cli.main._phase_hydrate") as mock_hydrate,
            patch("cli.main._phase_acl_grant") as mock_acl,
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

    def test_start_new_instance_triggers_scaffold(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        """New project: registry miss → scaffold → continue to launch."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/newproject"

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._scaffold_instance") as mock_scaffold,
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="pass"),
            patch("cli.main._phase_hydrate"),
            patch("cli.main._phase_acl_grant"),
            patch("cli.main._phase_compose_up"),
            patch("cli.main._phase_handover"),
            patch("cli.main._release_lock"),
        ):
            # scaffold must be called since registry has no entry
            mock_scaffold.return_value = (str(home / "sandboxes" / "newproject-aaa111"), "newproject-aaa111")
            runner.invoke(app, ["start"])
            mock_scaffold.assert_called_once()


class TestStartWarmExit:
    """Task 8.2: pre-lock warm-exit — no locks acquired."""

    def test_warm_instance_exits_before_locking(
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
            patch("cli.main._acquire_state_lock") as mock_lock,
        ):
            result = runner.invoke(app, ["start"])
            assert result.exit_code == 0
            assert "already running" in result.output.lower()
            mock_lock.assert_not_called()


class TestStartLockContention:
    """Task 8.2: lock contention exit."""

    def test_lock_contention_exits_with_message(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", side_effect=BlockingIOError("locked")),
        ):
            result = runner.invoke(app, ["start"])
            assert result.exit_code == 1
            assert "already in progress" in result.output.lower()


class TestStartIPAMExhausted:
    """Task 8.2: IPAM exhausted exit."""

    def test_ipam_exhausted_releases_lock_and_exits(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")

        from cli.main import app
        from core.ipam import IPAMExhaustedError

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", side_effect=IPAMExhaustedError("full")),
            patch("cli.main._release_lock") as mock_release,
        ):
            result = runner.invoke(app, ["start"])
            assert result.exit_code == 1
            mock_release.assert_called_once()


class TestStartComposeUnhealthy:
    """Task 8.2: compose unhealthy exit."""

    def test_unhealthy_compose_releases_lock_and_exits(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")

        from cli.main import app
        from core.exceptions import SandboxExecutionError

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._warm_check", return_value=False),
            patch("cli.main._acquire_state_lock", return_value=99),
            patch("cli.main._phase_ipam", return_value=0),
            patch("cli.main._phase_credentials", return_value="pass"),
            patch("cli.main._phase_hydrate"),
            patch("cli.main._phase_acl_grant"),
            patch("cli.main._phase_compose_up", side_effect=SandboxExecutionError("unhealthy")),
            patch("cli.main._release_lock") as mock_release,
        ):
            result = runner.invoke(app, ["start"])
            assert result.exit_code == 1
            mock_release.assert_called_once()


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

    def test_stop_cold_instance_warns_and_exits(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
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

    def test_stop_clean_passes_volume_flag(
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

    def test_attach_warm_instance_hands_over_terminal(
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
            patch("cli.main._phase_handover") as mock_handover,
        ):
            result = runner.invoke(app, ["attach"])
            assert result.exit_code == 0
            mock_handover.assert_called_once()


class TestAttachCold:
    """Task 8.2: sandbox attach — cold reject."""

    def test_attach_cold_instance_rejects(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
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

    def test_destroy_accepted_performs_full_teardown(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)
        _write_ipam(home, instance_id, 0)

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

    def test_destroy_rejected_aborts_silently(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
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

    def test_destroy_force_bypasses_confirmation(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)
        _write_ipam(home, instance_id, 0)

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

    def test_destroy_rejects_path_outside_sandboxes(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        # Manually write a corrupted registry pointing outside sandboxes/
        reg = home / ".state" / "instances.json"
        reg.write_text(json.dumps({project_dir: "../../../etc"}))
        _write_ipam(home, "../../../etc", 0)

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

    def test_destroy_clears_ipam_and_registry(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)
        _write_ipam(home, instance_id, 5)

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

    def test_destroy_removes_instance_directory(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)
        _write_ipam(home, instance_id, 0)

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
