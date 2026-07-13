# Copyright (c) 2026 Rafa Souza. SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for core/scaffold.py — instance directory scaffolding."""

import os
import stat
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from core.exceptions import SandboxExecutionError
from core.host_config import ensure_per_user_state
from core.scaffold import (
    INSTANCE_SUBDIRS,
    REQUIRED_INSTANCE_SECRETS,
    SecretSeedingError,
    WorkspaceSpec,
    apply_default_acls,
    create_env_file,
    create_instance_dirs,
    ensure_registry_seed,
    parse_secrets_file,
    prompt_secrets,
    resolve_secrets_from_env,
    seed_secrets,
    write_initialized_sentinel,
    write_sandbox_toml,
)

# ─── Expected directory tree (S1 from design spec) ───────────────────────────
#
# Post-Change-D contract (per orchestrator-volumes "Scaffold-vs-Helper Boundary"):
# scaffold creates the cache/log parents but NOT the helper-recipe-owned leaves.
# The leaves listed in ``HELPER_RECIPE_LEAVES`` below are created by
# ``_phase_helper_mkdir_chown_cache_log`` on first start.

EXPECTED_DIRS = [
    "docker/core",
    "docker/admin",
    "docker/extras",
    "config/core",
    "config/coredns",
    "config/dnsdist",
    "config/proxy",
    "log/proxy",
    "log/orchestrator",
    "cache/core",
    "custom/config/core",
]

# Cache/log leaf inventory per orchestrator-volumes "Cache/Log Leaf Inventory"
# requirement. These are owned end-to-end by the helper recipe and MUST NOT
# appear in INSTANCE_SUBDIRS (the "Scaffold-vs-Helper Boundary" requirement).
# Cluster 1 (orchestrator-volumes-scaffold-helper-acl-completeness) extended
# the inventory to include log/core and log/admin so the helper-mkdir+chown
# phase owns log leaves end-to-end, eliminating the userns-EPERM bug class
# from a scaffold-pre-created log leaf.
HELPER_RECIPE_CACHE_LEAVES = frozenset({
    "cache/core/.claude",
    "cache/admin/tmux_resurrect",
    "log/core",
    "log/admin",
})


class TestCreateInstanceDirs:
    def test_creates_full_tree(self, tmp_path: Path) -> None:
        """All required subdirectories are created."""
        instance_dir = tmp_path / "sandboxes" / "myproject-abc123"
        create_instance_dirs(str(instance_dir))

        for rel in EXPECTED_DIRS:
            assert (instance_dir / rel).is_dir(), f"Missing dir: {rel}"

    def test_helper_recipe_leaves_absent_post_scaffold(self, tmp_path: Path) -> None:
        """Cache/log helper-recipe-owned leaves are NOT created by scaffold.

        Per orchestrator-volumes' "Scaffold-vs-Helper Boundary" requirement,
        directories subject to a helper-recipe ``subuid-chown`` mechanism
        (the cache/log leaves) are created by ``_phase_helper_mkdir_chown_cache_log``
        on first start, not by ``create_instance_dirs``. A leaf created by
        scaffold (running as host dev uid) would be unmapped in the daemon's
        userns, blocking the helper's chown with EPERM.
        """
        instance_dir = tmp_path / "sandboxes" / "myproject-abc123"
        create_instance_dirs(str(instance_dir))

        for leaf in HELPER_RECIPE_CACHE_LEAVES:
            assert not (instance_dir / leaf).exists(), (
                f"Scaffold MUST NOT pre-create helper-recipe-owned leaf: {leaf}"
            )

    def test_scaffold_helper_boundary(self) -> None:
        """INSTANCE_SUBDIRS does not intersect the helper-recipe leaf inventory.

        Enforces orchestrator-volumes' "Scaffold-vs-Helper Boundary"
        requirement: cache/log leaves owned by a helper-recipe
        ``subuid-chown`` mechanism MUST NOT appear in
        ``core.scaffold.INSTANCE_SUBDIRS``. Inventory currently:
        ``cache/core/.claude`` and ``cache/admin/tmux_resurrect``. Future
        helper-recipe leaves added to ``orchestrator-volumes``'s
        "Cache/Log Leaf Inventory" must extend ``HELPER_RECIPE_CACHE_LEAVES``
        and remain absent from ``INSTANCE_SUBDIRS``.
        """
        intersection = HELPER_RECIPE_CACHE_LEAVES & set(INSTANCE_SUBDIRS)
        assert intersection == set(), (
            f"INSTANCE_SUBDIRS must not contain helper-recipe-owned leaves; "
            f"violating entries: {sorted(intersection)}"
        )

    def test_log_leaves_excluded_from_instance_subdirs(self) -> None:
        """log/core and log/admin must NOT appear in INSTANCE_SUBDIRS.

        Cluster 1 regression test for finding 8.A: scaffold pre-creating
        ``log/core`` and ``log/admin`` as host dev (uid 1000) makes the
        helper-mkdir+chown phase EPERM in the daemon's userns where
        host uid 1000 is unmapped. The structural fix is to exclude
        these leaves from ``INSTANCE_SUBDIRS`` so the helper recipe
        creates them as in-container root (mapped to host claude-sandbox)
        end-to-end. Spec source: orchestrator-volumes' extended
        "Cache/Log Leaf Inventory" + "Scaffold-vs-Helper Boundary".
        """
        assert "log/core" not in INSTANCE_SUBDIRS
        assert "log/admin" not in INSTANCE_SUBDIRS

    def test_admin_config_cache_custom_excluded_from_instance_subdirs(self) -> None:
        """admin's config/cache/custom dirs MUST NOT appear in INSTANCE_SUBDIRS.

        Per ``admin-reframe``: the admin container is operator-facing tooling,
        not a user-configurable runtime, so its config/cache/custom-config
        scaffold dirs are dropped. ``docker/admin`` (the build-context dir)
        is retained — compose still references it.
        """
        assert "config/admin" not in INSTANCE_SUBDIRS
        assert "cache/admin" not in INSTANCE_SUBDIRS
        assert "custom/config/admin" not in INSTANCE_SUBDIRS
        # docker/admin retained — build context still required by compose
        assert "docker/admin" in INSTANCE_SUBDIRS

    def test_idempotent(self, tmp_path: Path) -> None:
        """Calling twice does not raise."""
        instance_dir = tmp_path / "sandboxes" / "myproject-abc123"
        create_instance_dirs(str(instance_dir))
        create_instance_dirs(str(instance_dir))  # Should not raise

    def test_creates_workspace_dirs_with_mode_0700(self, tmp_path: Path) -> None:
        """Each entry in ``workspaces`` produces ``<workspace.path>`` with mode 0700."""
        instance_dir = tmp_path / "sandboxes" / "proj-abc"
        ws_main = tmp_path / "workspaces" / "proj" / "main"
        ws_api = tmp_path / "workspaces" / "proj" / "api"
        specs = [
            WorkspaceSpec(name="main", bootstrap_mode="empty", source=None, path=str(ws_main)),
            WorkspaceSpec(name="api", bootstrap_mode="copy", source="/src", path=str(ws_api)),
        ]
        create_instance_dirs(str(instance_dir), specs)
        assert ws_main.is_dir()
        assert ws_api.is_dir()
        assert stat.S_IMODE(ws_main.stat().st_mode) == 0o700
        assert stat.S_IMODE(ws_api.stat().st_mode) == 0o700


class TestWriteSandboxToml:
    def _ws(self, path: str = "/home/user/myproject") -> list[WorkspaceSpec]:
        return [WorkspaceSpec(name="main", bootstrap_mode="copy", source=path, path=path)]

    def test_generates_valid_toml(self, tmp_path: Path) -> None:
        """sandbox.toml is written with auto-derived defaults and a [workspaces.main] block."""
        instance_dir = tmp_path / "sandboxes" / "myproject-abc123"
        instance_dir.mkdir(parents=True)

        write_sandbox_toml(
            instance_dir=str(instance_dir),
            instance_name="myproject",
            workspaces=self._ws(),
        )

        toml_path = instance_dir / "sandbox.toml"
        assert toml_path.exists()
        content = toml_path.read_text()

        assert 'name = "myproject"' in content
        assert "[workspaces.main]" in content
        assert 'bootstrap_mode = "copy"' in content
        assert 'source = "/home/user/myproject"' in content
        assert 'path = "/home/user/myproject"' in content
        assert "host_unprivileged_user" not in content
        assert "host_uid" in content

    def test_host_uid_auto_detected(self, tmp_path: Path) -> None:
        """host_uid matches the current process UID."""
        instance_dir = tmp_path / "sandboxes" / "myproject-abc123"
        instance_dir.mkdir(parents=True)

        write_sandbox_toml(
            instance_dir=str(instance_dir),
            instance_name="myproject",
            workspaces=self._ws(),
        )

        content = (instance_dir / "sandbox.toml").read_text()
        assert f'host_uid = "{os.getuid()}"' in content

    def test_no_admin_section(self, tmp_path: Path) -> None:
        """Rendered sandbox.toml has no [admin] section and no admin_base_image placeholder.

        Per ``sandbox-toml-schema``'s Schema Generation requirement, the admin
        container's runtime knobs are derived from ``[core]`` rather than
        operator-configurable, so the scaffold MUST NOT emit an ``[admin]``
        section. ``admin_base_image`` is similarly removed from the template's
        format placeholders.
        """
        instance_dir = tmp_path / "sandboxes" / "myproject-abc123"
        instance_dir.mkdir(parents=True)

        write_sandbox_toml(
            instance_dir=str(instance_dir),
            instance_name="myproject",
            workspaces=self._ws(),
        )

        content = (instance_dir / "sandbox.toml").read_text()
        assert "[admin]" not in content
        assert "admin_base_image" not in content


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
        """setfacl -d -m called on log/, cache/, and each registered workspace path."""
        mock_run.return_value = MagicMock(returncode=0)

        apply_default_acls(
            instance_dir="/sandboxes/myproject-abc123",
            workspace_paths=["/home/user/myproject"],
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
                ["setfacl", "-d", "-m", "u:dev:rwx", "/home/user/myproject"],
                check=True,
            ),
        ]
        mock_run.assert_has_calls(expected_calls, any_order=False)

    @patch("subprocess.run")
    def test_fans_out_per_workspace(self, mock_run: MagicMock) -> None:
        """Each workspace path receives a setfacl -d call."""
        mock_run.return_value = MagicMock(returncode=0)
        apply_default_acls(
            instance_dir="/sandboxes/x",
            workspace_paths=["/ws/a", "/ws/b"],
            dev_user="dev",
        )
        called = [c.args[0] for c in mock_run.call_args_list]
        assert ["setfacl", "-d", "-m", "u:dev:rwx", "/ws/a"] in called
        assert ["setfacl", "-d", "-m", "u:dev:rwx", "/ws/b"] in called


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

    def _ws(self) -> list[WorkspaceSpec]:
        return [WorkspaceSpec(name="main", bootstrap_mode="copy", source="/dev/test", path="/dev/test")]

    def test_git_user_interpolated(self, tmp_path: Path) -> None:
        """git_user value is written into sandbox.toml."""
        instance_dir = tmp_path / "sandboxes" / "test"
        instance_dir.mkdir(parents=True)

        write_sandbox_toml(
            instance_dir=str(instance_dir),
            instance_name="test",
            workspaces=self._ws(),
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
            workspaces=self._ws(),
        )

        content = (instance_dir / "sandbox.toml").read_text()
        assert 'git_user = ""' in content
        assert 'git_email = ""' in content


# ─── Task 2.2: detect_git_config ────────────────────────────────────────────


class TestDetectGitConfig:
    """Task 2.2: auto-detect git user.name and user.email from global config."""

    def test_git_config_detected(self) -> None:
        """Returns (name, email) when git config is available."""
        from unittest.mock import patch

        from core.scaffold import detect_git_config

        def mock_run(cmd: list[str], **kwargs: object) -> object:
            import subprocess

            if "user.name" in cmd:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="Jane Doe\n", stderr="")
            if "user.email" in cmd:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="jane@example.com\n", stderr="")
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")

        with patch("subprocess.run", side_effect=mock_run):
            name, email = detect_git_config()
            assert name == "Jane Doe"
            assert email == "jane@example.com"

    def test_git_not_installed(self) -> None:
        """Returns ('', '') when git is not on PATH."""
        from unittest.mock import patch

        from core.scaffold import detect_git_config

        with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
            name, email = detect_git_config()
            assert name == ""
            assert email == ""

    def test_git_config_unset(self) -> None:
        """Returns ('', '') when git config values are unset."""
        import subprocess
        from unittest.mock import patch

        from core.scaffold import detect_git_config

        mock_result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            name, email = detect_git_config()
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

    def test_non_tty_writes_placeholder_values(self, tmp_path: Path) -> None:
        """Non-TTY mode rewrites empty CORE_* slots with YOUR_<KEY>_HERE placeholders.

        This bypasses the `sandbox start` 'Missing required secrets' fail-fast for
        automation flows that don't actually use those services at runtime, while
        keeping the placeholder obviously not a real credential.
        """
        from unittest.mock import MagicMock, patch

        env_path = tmp_path / ".sandbox.env"
        env_path.write_text('CORE_ANTHROPIC_API_KEY=""\nCORE_GITHUB_TOKEN=""\n')

        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = False

        with patch("sys.stdin", mock_stdin):
            prompt_secrets(
                str(env_path),
                [
                    ("CORE_ANTHROPIC_API_KEY", "Anthropic API key"),
                    ("CORE_GITHUB_TOKEN", "GitHub PAT"),
                ],
                MagicMock(),
            )

        content = env_path.read_text()
        assert 'CORE_ANTHROPIC_API_KEY="YOUR_CORE_ANTHROPIC_API_KEY_HERE"' in content
        assert 'CORE_GITHUB_TOKEN="YOUR_CORE_GITHUB_TOKEN_HERE"' in content
        # The placeholder must not be an empty string anymore
        assert 'CORE_ANTHROPIC_API_KEY=""' not in content

    def test_non_tty_prints_guidance(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Non-TTY mode prints guidance message with env path."""
        from unittest.mock import MagicMock, patch

        env_path = tmp_path / ".sandbox.env"
        env_path.write_text('CORE_ANTHROPIC_API_KEY=""\n')

        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = False

        with patch("sys.stdin", mock_stdin):
            prompt_secrets(str(env_path), [("CORE_ANTHROPIC_API_KEY", "key")], MagicMock())

        captured = capsys.readouterr()
        assert str(env_path) in captured.out
        assert "stub values" in captured.out

    def test_non_tty_preserves_pre_populated_slot(self, tmp_path: Path) -> None:
        """A slot pre-edited with a real value is preserved; only empty slots are stubbed."""
        env_path = tmp_path / ".sandbox.env"
        env_path.write_text(
            'CORE_ANTHROPIC_API_KEY="real-key-here"\nCORE_GITHUB_TOKEN=""\n'
        )

        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = False

        with patch("sys.stdin", mock_stdin):
            prompt_secrets(
                str(env_path),
                [
                    ("CORE_ANTHROPIC_API_KEY", "Anthropic API key"),
                    ("CORE_GITHUB_TOKEN", "GitHub PAT"),
                ],
                MagicMock(),
            )

        content = env_path.read_text()
        # Pre-populated real value survives unchanged
        assert 'CORE_ANTHROPIC_API_KEY="real-key-here"' in content
        # Empty slot is stubbed
        assert 'CORE_GITHUB_TOKEN="YOUR_CORE_GITHUB_TOKEN_HERE"' in content

    def test_non_tty_idempotent_on_second_invocation(self, tmp_path: Path) -> None:
        """Two consecutive non-TTY runs leave the file in the same final state.

        After the first run, slots hold YOUR_<NAME>_HERE placeholders (non-empty),
        so the second run's no-op replace plus non-empty verification both succeed.
        """
        env_path = tmp_path / ".sandbox.env"
        env_path.write_text('CORE_ANTHROPIC_API_KEY=""\nCORE_GITHUB_TOKEN=""\n')

        required = [
            ("CORE_ANTHROPIC_API_KEY", "Anthropic API key"),
            ("CORE_GITHUB_TOKEN", "GitHub PAT"),
        ]
        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = False

        with patch("sys.stdin", mock_stdin):
            prompt_secrets(str(env_path), required, MagicMock())
            after_first = env_path.read_text()
            prompt_secrets(str(env_path), required, MagicMock())
            after_second = env_path.read_text()

        assert after_first == after_second
        assert 'CORE_ANTHROPIC_API_KEY="YOUR_CORE_ANTHROPIC_API_KEY_HERE"' in after_second
        assert 'CORE_GITHUB_TOKEN="YOUR_CORE_GITHUB_TOKEN_HERE"' in after_second
        # No double-wrapping (e.g. YOUR_YOUR_..._HERE_HERE)
        assert "YOUR_YOUR_" not in after_second

    def test_non_tty_missing_slot_raises(self, tmp_path: Path) -> None:
        """A required secret with no slot in the file raises SandboxExecutionError.

        Pins down the loud-failure contract: the function must not silently claim
        success when the canonical `<NAME>=""` slot is absent.
        """
        env_path = tmp_path / ".sandbox.env"
        # Only one of the two required slots exists in the file
        env_path.write_text('CORE_ANTHROPIC_API_KEY=""\n')

        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = False

        with patch("sys.stdin", mock_stdin), pytest.raises(SandboxExecutionError) as exc:
            prompt_secrets(
                str(env_path),
                [
                    ("CORE_ANTHROPIC_API_KEY", "Anthropic API key"),
                    ("CORE_FOO_BAR", "Missing slot"),
                ],
                MagicMock(),
            )

        assert "CORE_FOO_BAR" in str(exc.value)
        assert str(env_path) in str(exc.value)

    def test_non_tty_bare_equals_slot_raises(self, tmp_path: Path) -> None:
        """A bare `NAME=` slot (no quotes, no value) is NOT stubbed and raises.

        Pins down the *current* contract: the auto-stub `replace` only matches
        the canonical `<NAME>=""` form. A bare-equals line `CORE_FOO=` is left
        untouched by the replace, then caught by the verification regex
        (which treats the empty third alternative as "still empty"), so the
        function raises `SandboxExecutionError` rather than silently leaving
        an empty slot. If a future change widens the replace pattern to also
        match bare-equals, this test will need to be flipped.
        """
        env_path = tmp_path / ".sandbox.env"
        env_path.write_text("CORE_FOO=\n")

        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = False

        with patch("sys.stdin", mock_stdin), pytest.raises(SandboxExecutionError) as exc:
            prompt_secrets(
                str(env_path),
                [("CORE_FOO", "test")],
                MagicMock(),
            )

        assert "CORE_FOO" in str(exc.value)
        # File contents must NOT have been stubbed to the canonical form.
        assert 'CORE_FOO="YOUR_CORE_FOO_HERE"' not in env_path.read_text()


# ─── non-interactive secret seeding (D1) ─────────────────────────────────────


class TestResolveSecretsFromEnv:
    """`resolve_secrets_from_env` populate-or-refuse over the required set."""

    def test_populates_all(self) -> None:
        env = {"CORE_ANTHROPIC_API_KEY": "ak", "CORE_GITHUB_TOKEN": "gh", "OTHER": "x"}
        assert resolve_secrets_from_env(env) == {
            "CORE_ANTHROPIC_API_KEY": "ak",
            "CORE_GITHUB_TOKEN": "gh",
        }

    def test_refuses_naming_missing(self) -> None:
        env = {"CORE_ANTHROPIC_API_KEY": "ak"}
        with pytest.raises(SecretSeedingError) as exc:
            resolve_secrets_from_env(env)
        assert "CORE_GITHUB_TOKEN" in str(exc.value)

    def test_refuses_on_empty_value(self) -> None:
        env = {"CORE_ANTHROPIC_API_KEY": "ak", "CORE_GITHUB_TOKEN": ""}
        with pytest.raises(SecretSeedingError) as exc:
            resolve_secrets_from_env(env)
        assert "CORE_GITHUB_TOKEN" in str(exc.value)


class TestParseSecretsFile:
    """`parse_secrets_file` format + validation."""

    def test_populates_with_comment_and_blank(self, tmp_path: Path) -> None:
        f = tmp_path / "s.env"
        f.write_text(
            "# a comment\n\n  # indented comment\n"
            "CORE_ANTHROPIC_API_KEY=ak\nCORE_GITHUB_TOKEN=gh\n"
        )
        assert parse_secrets_file(str(f)) == {
            "CORE_ANTHROPIC_API_KEY": "ak",
            "CORE_GITHUB_TOKEN": "gh",
        }

    def test_missing_key_refuses(self, tmp_path: Path) -> None:
        f = tmp_path / "s.env"
        f.write_text("CORE_ANTHROPIC_API_KEY=ak\n")
        with pytest.raises(SecretSeedingError) as exc:
            parse_secrets_file(str(f))
        assert "CORE_GITHUB_TOKEN" in str(exc.value)

    def test_empty_value_refuses(self, tmp_path: Path) -> None:
        f = tmp_path / "s.env"
        f.write_text("CORE_ANTHROPIC_API_KEY=ak\nCORE_GITHUB_TOKEN=\n")
        with pytest.raises(SecretSeedingError) as exc:
            parse_secrets_file(str(f))
        assert "CORE_GITHUB_TOKEN" in str(exc.value)

    def test_unrecognized_key_refuses(self, tmp_path: Path) -> None:
        f = tmp_path / "s.env"
        f.write_text("CORE_ANTHROPIC_API_KEY=ak\nCORE_GITHUB_TOKEN=gh\nFIRECRAWL_API_KEY=fc\n")
        with pytest.raises(SecretSeedingError) as exc:
            parse_secrets_file(str(f))
        assert "FIRECRAWL_API_KEY" in str(exc.value)

    def test_malformed_line_refuses(self, tmp_path: Path) -> None:
        f = tmp_path / "s.env"
        f.write_text("CORE_ANTHROPIC_API_KEY=ak\nnot_a_pair\n")
        with pytest.raises(SecretSeedingError) as exc:
            parse_secrets_file(str(f))
        assert "KEY=VALUE" in str(exc.value)

    def test_unreadable_file_refuses(self, tmp_path: Path) -> None:
        missing = tmp_path / "absent.env"
        with pytest.raises(SecretSeedingError) as exc:
            parse_secrets_file(str(missing))
        assert str(missing) in str(exc.value)


class TestSeedSecrets:
    """`seed_secrets` replaces each canonical `<NAME>=""` slot."""

    def test_replaces_slots(self, tmp_path: Path) -> None:
        env_path = tmp_path / ".sandbox.env"
        env_path.write_text('CORE_ANTHROPIC_API_KEY=""\nCORE_GITHUB_TOKEN=""\nPG_PASSWORD="x"\n')
        seed_secrets(
            str(env_path),
            {"CORE_ANTHROPIC_API_KEY": "ak", "CORE_GITHUB_TOKEN": "gh"},
        )
        text = env_path.read_text()
        assert 'CORE_ANTHROPIC_API_KEY="ak"' in text
        assert 'CORE_GITHUB_TOKEN="gh"' in text
        assert 'PG_PASSWORD="x"' in text


def test_required_instance_secrets_constant() -> None:
    assert REQUIRED_INSTANCE_SECRETS == ("CORE_ANTHROPIC_API_KEY", "CORE_GITHUB_TOKEN")


# ─── ensure_registry_seed ────────────────────────────────────────────────────


class TestEnsureRegistrySeed:
    """Registry seed creation: empty JSON, idempotent, no overwrite."""

    def test_creates_empty_registry(self, tmp_path: Path) -> None:
        home = tmp_path / ".sandbox-ai"
        ensure_per_user_state(home)
        ensure_registry_seed(home)
        registry = home / "state" / "instances.json"
        assert registry.exists()
        assert registry.read_text() == "{}"

    def test_does_not_overwrite_existing(self, tmp_path: Path) -> None:
        home = tmp_path / ".sandbox-ai"
        ensure_per_user_state(home)
        registry = home / "state" / "instances.json"
        registry.write_text('{"/x": "x-aaa"}')
        ensure_registry_seed(home)
        assert registry.read_text() == '{"/x": "x-aaa"}'
