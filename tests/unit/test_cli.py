"""CLI lifecycle unit tests — start, stop, attach, destroy.

All subprocess interactions are mocked at the core.executor.Executor.run boundary.
Tests validate the full phase sequencing per the orchestrator design spec.
"""

import json
import os
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

RENAMED_TOML_CONTENT = VALID_TOML_CONTENT.replace(
    b'name = "myproject"', b'name = "renamed-project"'
)

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
        """Existing instance: full start sequence (registry hit -> compose -> handover)."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)
        _write_ipam(home, instance_id, 0)

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

    def test_start_no_init_errors(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        """New project: registry miss → error with init guidance."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/newproject"

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
        ):
            result = runner.invoke(app, ["start"])
            assert result.exit_code == 1
            assert "sandbox init" in result.output.lower()

    def test_start_partial_init_errors(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
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

    def test_start_progress_output(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        """Start shows phase progress indicators."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)
        _write_ipam(home, instance_id, 0)

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

    def test_start_handover_indication(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        """Start shows handover indication before PTY exec."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)
        _write_ipam(home, instance_id, 0)

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

    def test_start_exits_on_missing_secrets(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
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

    def test_start_exits_on_doctor_chain1_failure(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
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

    def test_start_uses_console_status_for_compose(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        """start uses console.status() context manager during compose-up."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)
        _write_ipam(home, instance_id, 0)

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
            patch("cli.main._check_secrets", return_value=[]),
            patch("cli.main.run_check_subset", return_value=[]),
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


class TestStartProjectNameImmutabilityWarning:
    """Spec: sandbox-toml-schema — project.name immutability warning."""

    def test_divergent_name_emits_warning(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        """WHEN project.name differs from instance_id name component, THEN warning is emitted."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        inst = _register_instance(home, project_dir, instance_id)
        # Overwrite sandbox.toml with renamed project.name
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
            patch("cli.main._phase_compose_up"),
            patch("cli.main._phase_handover"),
            patch("cli.main._release_lock"),
        ):
            result = runner.invoke(app, ["start"])
            assert result.exit_code == 0
            assert "project.name has changed" in result.output
            assert "COMPOSE_PROJECT_NAME" in result.output

    def test_matching_name_no_warning(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        """WHEN project.name matches instance_id name component, THEN no warning."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        instance_id = "myproject-abc123"
        _register_instance(home, project_dir, instance_id)
        _write_ipam(home, instance_id, 0)

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
            patch("cli.main._phase_compose_up"),
            patch("cli.main._phase_handover"),
            patch("cli.main._release_lock"),
        ):
            result = runner.invoke(app, ["start"])
            assert result.exit_code == 0
            assert "project.name has changed" not in result.output


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


# ── Helper function unit tests (coverage) ────────────────────────────────────


class TestResolveHelpers:
    """Direct tests for _resolve_sandbox_ai_home and _resolve_project_dir."""

    def test_resolve_sandbox_ai_home_returns_parent_of_cli(self) -> None:
        from cli.main import _resolve_sandbox_ai_home

        result = _resolve_sandbox_ai_home()
        assert os.path.isabs(result)
        # Should be the repo root (parent of cli/)
        assert os.path.isdir(os.path.join(result, "cli"))

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
        assert config.project.name == "myproject"


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

    def test_acquire_contention(self, tmp_path: Path) -> None:
        import fcntl as _fcntl

        from cli.main import _acquire_state_lock

        lock_path = tmp_path / "state.lock"
        held_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        _fcntl.flock(held_fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        try:
            with pytest.raises(BlockingIOError):
                _acquire_state_lock(str(tmp_path))
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
        inst_dir = tmp_path

        with patch("cli.main.write_htpasswd") as mock_write:
            password = _phase_credentials(str(inst_dir))
            assert len(password) > 0
            mock_write.assert_called_once()
            line = mock_write.call_args[0][1]
            assert line.startswith("proxyuser:$2b$")


class TestPhaseHydrateDirect:
    """Direct test for _phase_hydrate."""

    def test_phase_hydrate_calls_render(self) -> None:
        from cli.main import _phase_hydrate
        from core.hydration import SandboxConfig

        mock_config = SandboxConfig.model_validate({
            "project": {
                "name": "test",
                "user_project_root": "/home/dev/test",
                "host_unprivileged_user": "sandbox",
                "host_uid": "1000",
            }
        })

        with (
            patch("cli.main.build_jinja_context", return_value={}) as mock_ctx,
            patch("cli.main.render_templates") as mock_render,
        ):
            _phase_hydrate(mock_config, 0, "pass", "/home", "/inst")
            mock_ctx.assert_called_once()
            mock_render.assert_called_once()


class TestPhaseACLDirect:
    """Direct test for _phase_acl_grant."""

    def test_phase_acl_grant_calls_setfacl(self) -> None:
        from cli.main import _phase_acl_grant

        with patch("subprocess.run") as mock_run:
            _phase_acl_grant("/inst", "sandbox")
            assert mock_run.call_count == 2
            calls = mock_run.call_args_list
            assert "u:sandbox:rX" in calls[0][0][0]
            assert "u:sandbox:rX" in calls[1][0][0]


class TestBuildComposeFiles:
    """Direct test for _build_compose_files."""

    def test_base_only(self) -> None:
        from cli.main import _build_compose_files
        from core.hydration import SandboxConfig

        config = SandboxConfig.model_validate({
            "project": {
                "name": "t", "user_project_root": "/x",
                "host_unprivileged_user": "s", "host_uid": "1000",
            },
            "components": {"mcp_firecrawl": False, "mcp_puppeteer": False},
            "components_db_postgres": {"enabled": False},
        })
        files = _build_compose_files("/inst", config)
        assert len(files) == 2  # -f, path

    def test_with_extras(self) -> None:
        from cli.main import _build_compose_files
        from core.hydration import SandboxConfig

        config = SandboxConfig.model_validate({
            "project": {
                "name": "t", "user_project_root": "/x",
                "host_unprivileged_user": "s", "host_uid": "1000",
            },
            "components": {"mcp_firecrawl": True, "mcp_puppeteer": False},
            "components_db_postgres": {"enabled": True},
        })
        files = _build_compose_files("/inst", config)
        assert len(files) == 6  # base + postgres + firecrawl


class TestPhaseComposeUpDirect:
    """Direct test for _phase_compose_up."""

    def test_compose_up_calls_executor(self) -> None:
        from cli.main import _phase_compose_up
        from core.hydration import SandboxConfig

        config = SandboxConfig.model_validate({
            "project": {
                "name": "t", "user_project_root": "/x",
                "host_unprivileged_user": "s", "host_uid": "1000",
            },
        })

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
        from core.hydration import SandboxConfig

        config = SandboxConfig.model_validate({
            "project": {
                "name": "t", "user_project_root": "/x",
                "host_unprivileged_user": "s", "host_uid": "1000",
            },
        })

        with patch("cli.main.Executor") as MockExec:
            _compose_down("/inst", "myproj", "sandbox", config, volumes=False)
            cmd_str = MockExec.return_value.run.call_args[0][0][-1]
            assert "down" in cmd_str
            assert " -v" not in cmd_str

    def test_compose_down_volumes(self) -> None:
        from cli.main import _compose_down
        from core.hydration import SandboxConfig

        config = SandboxConfig.model_validate({
            "project": {
                "name": "t", "user_project_root": "/x",
                "host_unprivileged_user": "s", "host_uid": "1000",
            },
        })

        with patch("cli.main.Executor") as MockExec:
            _compose_down("/inst", "myproj", "sandbox", config, volumes=True)
            cmd_str = MockExec.return_value.run.call_args[0][0][-1]
            assert "down -v" in cmd_str


class TestRevokeACLsDirect:
    """Direct test for _revoke_acls."""

    def test_revoke_acls_calls_setfacl(self) -> None:
        from cli.main import _revoke_acls

        with patch("subprocess.run") as mock_run:
            _revoke_acls("/inst", "sandbox")
            assert mock_run.call_count == 2


class TestInitScaffoldDirect:
    """Task 9.1: init command creates full instance (migrated from _scaffold_instance)."""

    def test_init_creates_full_instance(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        from cli.main import app
        from core.hydration import SandboxConfig

        home = mock_sandbox_ai_home
        project_dir = "/home/dev/newproject"

        mock_config = SandboxConfig.model_validate({
            "project": {
                "name": "newproject", "user_project_root": project_dir,
                "host_unprivileged_user": "sandbox", "host_uid": "1000",
            },
        })

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._detect_git_config", return_value=("Jane", "j@e.com")),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main.create_instance_dirs") as mock_dirs,
            patch("cli.main.write_sandbox_toml") as mock_toml,
            patch("cli.main._load_config", return_value=mock_config),
            patch("cli.main.create_env_file") as mock_env,
            patch("cli.main.apply_default_acls") as mock_acls,
            patch("cli.main.prompt_secrets") as mock_secrets,
            patch("cli.main.write_initialized_sentinel") as mock_sentinel,
        ):
            result = runner.invoke(app, ["init", "--user", "sandbox"])
            assert result.exit_code == 0
            mock_dirs.assert_called_once()
            mock_toml.assert_called_once()
            mock_env.assert_called_once()
            mock_acls.assert_called_once()
            mock_secrets.assert_called_once()
            mock_sentinel.assert_called_once()


# ── Edge case tests for remaining coverage ───────────────────────────────────


class TestStopNoInstance:
    """Cover stop with unregistered instance."""

    def test_stop_no_instance_exits(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(mock_sandbox_ai_home)),
            patch("cli.main._resolve_project_dir", return_value="/nonexistent"),
        ):
            result = runner.invoke(app, ["stop"])
            assert result.exit_code == 1
            assert "no sandbox" in result.output.lower()


class TestAttachNoInstance:
    """Cover attach with unregistered instance."""

    def test_attach_no_instance_exits(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(mock_sandbox_ai_home)),
            patch("cli.main._resolve_project_dir", return_value="/nonexistent"),
        ):
            result = runner.invoke(app, ["attach"])
            assert result.exit_code == 1
            assert "no sandbox" in result.output.lower()


class TestDestroyNoInstance:
    """Cover destroy with unregistered instance."""

    def test_destroy_no_instance_exits(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(mock_sandbox_ai_home)),
            patch("cli.main._resolve_project_dir", return_value="/nonexistent"),
        ):
            result = runner.invoke(app, ["destroy", "--force"])
            assert result.exit_code == 1
            assert "no sandbox" in result.output.lower()


class TestDestroyPrefixGuardInternal:
    """Cover the internal prefix guard path (not mocked)."""

    def test_prefix_guard_rejects_bad_path(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        home = mock_sandbox_ai_home

        from cli.main import app

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

    def test_init_with_firecrawl_includes_secret(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        from cli.main import app
        from core.hydration import SandboxConfig

        home = mock_sandbox_ai_home
        project_dir = "/home/dev/fc-project"

        mock_config = SandboxConfig.model_validate({
            "project": {
                "name": "fc-project", "user_project_root": project_dir,
                "host_unprivileged_user": "sandbox", "host_uid": "1000",
            },
            "components": {"mcp_firecrawl": True, "mcp_puppeteer": False},
            "components_db_postgres": {"enabled": True},
        })

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._detect_git_config", return_value=("", "")),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main.create_instance_dirs"),
            patch("cli.main.write_sandbox_toml"),
            patch("cli.main._load_config", return_value=mock_config),
            patch("cli.main.create_env_file"),
            patch("cli.main.apply_default_acls"),
            patch("cli.main.prompt_secrets") as mock_prompt,
            patch("cli.main.write_initialized_sentinel"),
        ):
            result = runner.invoke(app, ["init", "--user", "sandbox"])
            assert result.exit_code == 0
            # Verify firecrawl secret was included in prompt_secrets call
            call_args = mock_prompt.call_args[0]
            secret_names = [s[0] for s in call_args[1]]
            assert "FIRECRAWL_API_KEY" in secret_names


# ── sandbox doctor ───────────────────────────────────────────────────────────


class TestDoctorAllPass:
    """Task 10.1: sandbox doctor --user — all checks pass."""

    def test_doctor_all_pass_exit_code_0(self, runner: CliRunner) -> None:
        from cli.main import app
        from core.doctor import CheckResult

        all_pass = [
            CheckResult(status="pass", name=f"check-{i}", detail="ok")
            for i in range(12)
        ]
        with (
            patch("cli.main.detect_distro", return_value="debian"),
            patch("cli.main.build_check_registry", return_value=[]),
            patch("cli.main.run_checks", return_value=all_pass),
            patch("cli.main.render_results"),
        ):
            result = runner.invoke(app, ["doctor", "--user", "sandbox"])
            assert result.exit_code == 0


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

    def test_init_creates_instance(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        """init scaffolds a new instance successfully."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/newproject"

        from cli.main import app
        from core.hydration import SandboxConfig

        mock_config = SandboxConfig.model_validate({
            "project": {
                "name": "newproject", "user_project_root": project_dir,
                "host_unprivileged_user": "sandbox", "host_uid": "1000",
            },
        })

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._detect_git_config", return_value=("Jane", "j@e.com")),
            patch("cli.main.run_check_subset", return_value=[]),
            patch("cli.main.create_instance_dirs"),
            patch("cli.main.write_sandbox_toml"),
            patch("cli.main._load_config", return_value=mock_config),
            patch("cli.main.create_env_file"),
            patch("cli.main.apply_default_acls"),
            patch("cli.main.prompt_secrets"),
            patch("cli.main.write_initialized_sentinel"),
        ):
            result = runner.invoke(app, ["init", "--user", "sandbox"])
            assert result.exit_code == 0


class TestInitReInitRejection:
    """Task 3.1: sandbox init — re-init rejected."""

    def test_reinit_rejected(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        """init errors when instance already exists."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
        ):
            result = runner.invoke(app, ["init", "--user", "sandbox"])
            assert result.exit_code == 1
            assert "already initialized" in result.output.lower() or "destroy" in result.output.lower()


class TestInitDryRun:
    """Task 3.1: sandbox init --dry-run previews without writing."""

    def test_init_dry_run_no_state(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        """init --dry-run does not create any files or registry entries."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/newproject"

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main._detect_git_config", return_value=("", "")),
        ):
            result = runner.invoke(app, ["init", "--user", "sandbox", "--dry-run"])
            assert result.exit_code == 0

        # Registry should be unmodified
        reg_data = json.loads((home / ".state" / "instances.json").read_text())
        assert project_dir not in reg_data


class TestInitDoctorPreFlightFailure:
    """Task 3.1: sandbox init — doctor pre-flight failure aborts."""

    def test_init_aborts_on_doctor_failure(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        """init aborts when doctor pre-flight checks fail."""
        from core.doctor import CheckResult

        home = mock_sandbox_ai_home
        project_dir = "/home/dev/newproject"

        from cli.main import app

        failed_results = [
            CheckResult(status="fail", name="setfacl", detail="not found", remediation="install acl")
        ]
        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
            patch("cli.main.run_check_subset", return_value=failed_results),
            patch("cli.main.render_results"),
        ):
            result = runner.invoke(app, ["init", "--user", "sandbox"])
            assert result.exit_code == 1


class TestInitNonTTY:
    """Task 3.1: sandbox init in non-TTY environment."""

    def test_init_non_tty_completes(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        """init completes in non-TTY mode (prompt_secrets skips)."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/newproject"

        from cli.main import app
        from core.hydration import SandboxConfig

        mock_config = SandboxConfig.model_validate({
            "project": {
                "name": "newproject", "user_project_root": project_dir,
                "host_unprivileged_user": "sandbox", "host_uid": "1000",
            },
        })

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
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
            result = runner.invoke(app, ["init", "--user", "sandbox"])
            assert result.exit_code == 0


class TestInitMissingUser:
    """Task 3.1: sandbox init without --user errors."""

    def test_init_missing_user_exits_error(self, runner: CliRunner) -> None:
        from cli.main import app

        result = runner.invoke(app, ["init"])
        assert result.exit_code != 0


# ── sandbox start --dry-run ──────────────────────────────────────────────────


class TestDryRunExistingInstance:
    """Task 12.1: --dry-run with existing instance."""

    def test_dry_run_skips_warm_check(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        """Warm state check is skipped when --dry-run is set."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam(home, "myproject-abc123", 0)

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

    def test_dry_run_existing_instance_exit_0(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        """Existing instance dry-run passes with exit code 0."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam(home, "myproject-abc123", 0)
        _create_tooling_plane(home)

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
        ):
            result = runner.invoke(app, ["start", "--dry-run"])
            assert result.exit_code == 0

    def test_dry_run_shows_ipam_preview(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        """IPAM slot is previewed with 'subject to concurrent changes' note."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam(home, "myproject-abc123", 5)
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

    def test_dry_run_shows_compose_command(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        """Compose command is displayed in dry-run output."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam(home, "myproject-abc123", 0)
        _create_tooling_plane(home)

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
        ):
            result = runner.invoke(app, ["start", "--dry-run"])
            assert "docker compose" in result.output.lower() or "compose" in result.output.lower()

    def test_dry_run_template_error_exits_1(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        """Template error causes exit code 1."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam(home, "myproject-abc123", 0)
        _create_tooling_plane(home)
        # Break the compose template
        (home / ".docker" / "compose.yml").write_text("{{ undefined_var }}")

        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(home)),
            patch("cli.main._resolve_project_dir", return_value=project_dir),
        ):
            result = runner.invoke(app, ["start", "--dry-run"])
            assert result.exit_code == 1

    def test_dry_run_missing_env_keys_reported(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        """Missing .sandbox.env keys are reported in dry-run."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        inst = _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam(home, "myproject-abc123", 0)
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

    def test_dry_run_no_instance_errors_with_guidance(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        """No-instance dry-run errors with init guidance message."""
        home = mock_sandbox_ai_home

        from cli.main import app

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
    (docker_dir / "compose.yml").write_text(
        "# compose for {{ project_name }}\nversion: '3'\n"
    )
    (docker_dir / "core" / "entrypoint.sh").write_text("#!/bin/bash\n")
    (docker_dir / "core" / "Dockerfile.core.wolfi").write_text(
        "FROM {{ core_base_image }}\n"
    )
    (docker_dir / "admin" / "entrypoint.sh").write_text("#!/bin/bash\n")
    (docker_dir / "admin" / "Dockerfile.admin.debian").write_text(
        "FROM {{ admin_base_image }}\n"
    )
    (docker_dir / "extras").mkdir(parents=True, exist_ok=True)
    (docker_dir / "extras" / "db-postgres.yml").write_text("# postgres\n")

    config_dir = home / ".config"
    (config_dir / "dns-sidecar").mkdir(parents=True, exist_ok=True)
    (config_dir / "dns-sidecar" / "Corefile").write_text(
        "# Corefile for {{ project_name }}\n"
    )
    (config_dir / "proxy" / "squid.conf").write_text(
        "# squid for {{ proxy_ip }}\n"
    )
    (config_dir / "proxy" / "ERR_SANDBOX_403").write_text("403 Forbidden\n")
    (config_dir / "admin").mkdir(parents=True, exist_ok=True)
    (config_dir / "admin" / ".zshrc").write_text("# zshrc\n")
    (config_dir / "admin" / ".tmux.conf").write_text("# tmux\n")
    (config_dir / "admin" / "gitmux.conf").write_text("# gitmux\n")
    (config_dir / "admin" / "starship.toml").write_text("# starship\n")
    (config_dir / "core").mkdir(parents=True, exist_ok=True)
    (config_dir / "core" / ".bashrc").write_text("# bashrc\n")
    (config_dir / "core" / ".npmrc").write_text("# npmrc\n")
    (config_dir / "core" / ".gitconfig").write_text("# gitconfig\n")
    (config_dir / "core" / ".claude.json").write_text("{}\n")
    (config_dir / "core" / "CLAUDE.md").write_text("# Claude\n")


class TestDryRunIpamExhausted:
    """Cover IPAM exhaustion path in dry-run pipeline."""

    def test_dry_run_ipam_exhausted_exits_1(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
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

    def test_check_secrets_firecrawl_missing(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        """Firecrawl secret reported when mcp_firecrawl=true."""
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        inst = _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam(home, "myproject-abc123", 0)
        _create_tooling_plane(home)

        # Write a sandbox.toml with mcp_firecrawl=true
        firecrawl_toml = (inst / "sandbox.toml").read_bytes().decode()
        firecrawl_toml = firecrawl_toml.replace(
            "mcp_firecrawl = false", "mcp_firecrawl = true"
        )
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
        from core.hydration import SandboxConfig

        config = SandboxConfig.model_validate({
            "project": {
                "name": "t", "user_project_root": "/x",
                "host_unprivileged_user": "s", "host_uid": "1000",
            },
        })

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
        from core.hydration import SandboxConfig

        config = SandboxConfig.model_validate({
            "project": {
                "name": "t", "user_project_root": "/x",
                "host_unprivileged_user": "s", "host_uid": "1000",
            },
        })

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
        from core.hydration import SandboxConfig

        config = SandboxConfig.model_validate({
            "project": {
                "name": "t", "user_project_root": "/x",
                "host_unprivileged_user": "s", "host_uid": "1000",
            },
        })

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

    def test_status_no_instance_exits_1(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        from cli.main import app

        with (
            patch("cli.main._resolve_sandbox_ai_home", return_value=str(mock_sandbox_ai_home)),
            patch("cli.main._resolve_project_dir", return_value="/nonexistent"),
        ):
            result = runner.invoke(app, ["status"])
            assert result.exit_code == 1
            assert "no sandbox" in result.output.lower()


class TestStatusRunning:
    """Task 7.1: sandbox status — running instance."""

    def test_status_running_shows_state(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam(home, "myproject-abc123", 0)

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

    def test_status_stopped_shows_state(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam(home, "myproject-abc123", 0)

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

    def test_status_degraded_shows_warning(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam(home, "myproject-abc123", 0)

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

    def test_status_shows_ipam_subnets(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam(home, "myproject-abc123", 3)

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

    def test_status_warns_on_missing_secrets(
        self, runner: CliRunner, mock_sandbox_ai_home: Path
    ) -> None:
        home = mock_sandbox_ai_home
        project_dir = "/home/dev/myproject"
        _register_instance(home, project_dir, "myproject-abc123")
        _write_ipam(home, "myproject-abc123", 0)

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
