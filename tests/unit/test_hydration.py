"""Tests for core/hydration.py — Pydantic config model and Jinja2 rendering pipeline."""

import os
import shutil
from pathlib import Path

import pytest

from core.hydration import (
    SandboxConfig,
    build_jinja_context,
    render_templates,
)


# ─── Minimal valid TOML for SandboxConfig ─────────────────────────────────────

VALID_TOML = """\
[project]
name = "testproject"
user_project_root = "/home/dev/testproject"
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
db_postgres = true
mcp_firecrawl = false
mcp_puppeteer = false

[components.db_postgres]
expose_host_ports = [5432]

[components.ingress]
web_ports = [3000, 8080]

[proxy.whitelist]
domains = [
  ".github.com",
  ".npmjs.com",
]
"""


class TestSandboxConfig:
    def test_parse_valid_toml(self, tmp_path: Path) -> None:
        """SandboxConfig parses valid TOML without errors."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = SandboxConfig.from_toml(str(toml_path))
        assert config.project.name == "testproject"
        assert config.project.user_project_root == "/home/dev/testproject"
        assert config.core.pids_limit == 400
        assert config.admin.base_distro_family == "debian"
        assert config.runtimes.python is True
        assert config.runtimes.go is False
        assert config.components.db_postgres is True
        assert config.components.mcp_firecrawl is False
        assert ".github.com" in config.proxy_whitelist.domains

    def test_missing_required_field_raises(self, tmp_path: Path) -> None:
        """Missing required field raises ValidationError."""
        broken = VALID_TOML.replace('name = "testproject"', "")
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(broken)
        with pytest.raises(Exception):  # Pydantic ValidationError
            SandboxConfig.from_toml(str(toml_path))


class TestBuildJinjaContext:
    def test_produces_correct_ips_at_index_zero(self, tmp_path: Path) -> None:
        """Context dict has correct IP values at base_index=0."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = SandboxConfig.from_toml(str(toml_path))

        ctx = build_jinja_context(
            config=config,
            base_index=0,
            proxy_password="secretpass",
            instance_dir="/sandboxes/testproject-abc123",
        )

        assert ctx["isolated_subnet"] == "10.100.0.0/24"
        assert ctx["proxy_subnet"] == "10.100.1.0/24"
        assert ctx["egress_subnet"] == "10.100.2.0/24"
        assert ctx["dns_sidecar_ip"] == "10.100.0.53"
        assert ctx["proxy_ip"] == "10.100.1.254"
        assert ctx["proxy_password"] == "secretpass"
        assert ctx["instance_dir"] == "/sandboxes/testproject-abc123"
        assert ctx["user_project_root"] == "/home/dev/testproject"
        assert ctx["core_base_image"] == "cgr.dev/chainguard/wolfi-base:latest"
        assert ctx["admin_base_image"] == "debian:trixie-slim"
        assert ctx["host_uid"] == "1000"

    def test_runtimes_in_context(self, tmp_path: Path) -> None:
        """Runtimes dict is available in the Jinja2 context."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = SandboxConfig.from_toml(str(toml_path))

        ctx = build_jinja_context(
            config=config,
            base_index=0,
            proxy_password="x",
            instance_dir="/tmp/x",
        )

        assert ctx["runtimes"]["python"] is True
        assert ctx["runtimes"]["go"] is False

    def test_whitelist_domains_in_context(self, tmp_path: Path) -> None:
        """Proxy whitelist domains are available in the Jinja2 context."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = SandboxConfig.from_toml(str(toml_path))

        ctx = build_jinja_context(
            config=config,
            base_index=0,
            proxy_password="x",
            instance_dir="/tmp/x",
        )

        assert ".github.com" in ctx["proxy_whitelist_domains"]


class TestRenderTemplates:
    @pytest.fixture
    def tooling_and_instance(self, tmp_path: Path) -> tuple[Path, Path]:
        """Create a minimal tooling plane and instance dir for render tests."""
        tooling = tmp_path / "tooling"
        instance = tmp_path / "instance"

        # Create a minimal compose.yml template
        docker_dir = tooling / ".docker"
        docker_dir.mkdir(parents=True)
        (docker_dir / "compose.yml").write_text(
            "# rendered: {{ project_name }}\nsubnet: {{ isolated_subnet }}\n"
        )
        # Core Dockerfile template
        core_dir = docker_dir / "core"
        core_dir.mkdir()
        (core_dir / "Dockerfile.core.wolfi").write_text("FROM {{ core_base_image }}\n")
        (core_dir / "entrypoint.sh").write_text("#!/bin/bash\n")

        # Admin Dockerfile template
        admin_dir = docker_dir / "admin"
        admin_dir.mkdir()
        (admin_dir / "Dockerfile.admin.debian").write_text("FROM {{ admin_base_image }}\n")
        (admin_dir / "entrypoint.sh").write_text("#!/bin/sh\n")

        # Extras
        extras_dir = docker_dir / "extras"
        extras_dir.mkdir()
        (extras_dir / "db-postgres.yml").write_text("# postgres: {{ db_postgres_ip }}\n")
        (extras_dir / "mcp-firecrawl.yml").write_text("# firecrawl\n")
        (extras_dir / "Dockerfile.mcp-firecrawl").write_text("FROM node\n")

        # Config templates
        config_dir = tooling / ".config"
        # dns-sidecar
        dns_dir = config_dir / "dns-sidecar"
        dns_dir.mkdir(parents=True)
        (dns_dir / "Corefile").write_text("{{ proxy_whitelist_domains | join(' ') }}\n")

        # proxy
        proxy_dir = config_dir / "proxy"
        proxy_dir.mkdir(parents=True)
        (proxy_dir / "squid.conf").write_text("acl src {{ isolated_subnet }}\n")
        (proxy_dir / "ERR_SANDBOX_403").write_text("DENIED\n")

        # admin static configs
        admin_cfg = config_dir / "admin"
        admin_cfg.mkdir(parents=True)
        for f in [".zshrc", ".tmux.conf", "gitmux.conf", "starship.toml"]:
            (admin_cfg / f).write_text(f"# {f}\n")

        # core static configs
        core_cfg = config_dir / "core"
        core_cfg.mkdir(parents=True)
        for f in [".bashrc", ".npmrc", ".gitconfig", ".claude.json", "CLAUDE.md"]:
            (core_cfg / f).write_text(f"# {f}\n")

        # Create instance dirs
        for d in [
            "docker/core", "docker/admin", "docker/extras",
            "config/admin", "config/core", "config/dns-sidecar", "config/proxy",
            "log/admin", "log/core", "cache/.claude", "custom/config/admin",
        ]:
            (instance / d).mkdir(parents=True, exist_ok=True)

        return tooling, instance

    def test_renders_compose(self, tooling_and_instance: tuple[Path, Path]) -> None:
        """compose.yml is rendered with Jinja2 substitutions."""
        tooling, instance = tooling_and_instance
        ctx = _build_test_context(str(instance))

        render_templates(ctx, str(tooling), str(instance), db_postgres=True, mcp_firecrawl=False)

        rendered = (instance / "docker" / "compose.yml").read_text()
        assert "rendered: testproject" in rendered
        assert "10.100.0.0/24" in rendered

    def test_renders_dockerfile_by_distro(self, tooling_and_instance: tuple[Path, Path]) -> None:
        """Dockerfile selected by base_distro_family, rendered as Dockerfile.core."""
        tooling, instance = tooling_and_instance
        ctx = _build_test_context(str(instance))

        render_templates(ctx, str(tooling), str(instance), db_postgres=False, mcp_firecrawl=False)

        core_df = (instance / "docker" / "core" / "Dockerfile.core").read_text()
        assert "cgr.dev/chainguard/wolfi-base:latest" in core_df

        admin_df = (instance / "docker" / "admin" / "Dockerfile.admin").read_text()
        assert "debian:trixie-slim" in admin_df

    def test_disabled_component_skips_extra(self, tooling_and_instance: tuple[Path, Path]) -> None:
        """Disabled components do not have their extras rendered."""
        tooling, instance = tooling_and_instance
        ctx = _build_test_context(str(instance))

        render_templates(ctx, str(tooling), str(instance), db_postgres=False, mcp_firecrawl=False)

        assert not (instance / "docker" / "extras" / "db-postgres.yml").exists()
        assert not (instance / "docker" / "extras" / "mcp-firecrawl.yml").exists()

    def test_enabled_component_renders_extra(self, tooling_and_instance: tuple[Path, Path]) -> None:
        """Enabled components have their extras rendered."""
        tooling, instance = tooling_and_instance
        ctx = _build_test_context(str(instance))

        render_templates(ctx, str(tooling), str(instance), db_postgres=True, mcp_firecrawl=False)

        pg = (instance / "docker" / "extras" / "db-postgres.yml").read_text()
        assert "10.100.0.54" in pg

    def test_precious_state_never_overwritten(self, tooling_and_instance: tuple[Path, Path]) -> None:
        """Precious state files (sandbox.toml, .sandbox.env, custom/, cache/, log/) are never touched."""
        tooling, instance = tooling_and_instance
        ctx = _build_test_context(str(instance))

        # Write precious state files
        (instance / "sandbox.toml").write_text("PRECIOUS")
        (instance / ".sandbox.env").write_text("PRECIOUS")
        (instance / "custom" / "config" / "admin" / "custom.zshrc").write_text("PRECIOUS")
        (instance / "cache" / ".claude" / "settings.json").write_text("PRECIOUS")
        (instance / "log" / "admin" / "test.log").write_text("PRECIOUS")

        render_templates(ctx, str(tooling), str(instance), db_postgres=True, mcp_firecrawl=True)

        assert (instance / "sandbox.toml").read_text() == "PRECIOUS"
        assert (instance / ".sandbox.env").read_text() == "PRECIOUS"
        assert (instance / "custom" / "config" / "admin" / "custom.zshrc").read_text() == "PRECIOUS"
        assert (instance / "cache" / ".claude" / "settings.json").read_text() == "PRECIOUS"
        assert (instance / "log" / "admin" / "test.log").read_text() == "PRECIOUS"

    def test_renders_corefile(self, tooling_and_instance: tuple[Path, Path]) -> None:
        """Corefile is rendered with whitelist domains."""
        tooling, instance = tooling_and_instance
        ctx = _build_test_context(str(instance))

        render_templates(ctx, str(tooling), str(instance), db_postgres=False, mcp_firecrawl=False)

        corefile = (instance / "config" / "dns-sidecar" / "Corefile").read_text()
        assert ".github.com" in corefile

    def test_generates_allowed_domains(self, tooling_and_instance: tuple[Path, Path]) -> None:
        """allowed_domains.txt is generated from whitelist domains."""
        tooling, instance = tooling_and_instance
        ctx = _build_test_context(str(instance))

        render_templates(ctx, str(tooling), str(instance), db_postgres=False, mcp_firecrawl=False)

        domains = (instance / "config" / "proxy" / "allowed_domains.txt").read_text()
        assert ".github.com" in domains

    def test_copies_static_configs(self, tooling_and_instance: tuple[Path, Path]) -> None:
        """Static config files (admin/, core/) are copied to instance."""
        tooling, instance = tooling_and_instance
        ctx = _build_test_context(str(instance))

        render_templates(ctx, str(tooling), str(instance), db_postgres=False, mcp_firecrawl=False)

        assert (instance / "config" / "admin" / ".zshrc").exists()
        assert (instance / "config" / "core" / ".bashrc").exists()
        assert (instance / "config" / "core" / "CLAUDE.md").exists()


def _build_test_context(instance_dir: str) -> dict[str, object]:
    """Build a minimal Jinja2 context for render tests."""
    from core.ipam import derive_static_ips, derive_subnets

    isolated, proxy, egress = derive_subnets(0)
    ips = derive_static_ips(0)

    return {
        "project_name": "testproject",
        "instance_dir": instance_dir,
        "user_project_root": "/home/dev/testproject",
        "isolated_subnet": isolated,
        "proxy_subnet": proxy,
        "egress_subnet": egress,
        **ips,
        "proxy_password": "testpass",
        "core_base_image": "cgr.dev/chainguard/wolfi-base:latest",
        "admin_base_image": "debian:trixie-slim",
        "core_distro_family": "wolfi",
        "admin_distro_family": "debian",
        "host_uid": "1000",
        "core_pids_limit": 400,
        "admin_pids_limit": 400,
        "core_shm_size": "2gb",
        "admin_shm_size": "2gb",
        "runtime": "runsc",
        "dns_image": "coredns/coredns:1.11.1",
        "proxy_image": "ubuntu/squid:latest",
        "nvm_version": "0.39.7",
        "node_version": "20.12.2",
        "runtimes": {"python": True, "typescript": True, "rust": True, "go": False},
        "proxy_whitelist_domains": [".github.com", ".npmjs.com"],
    }
