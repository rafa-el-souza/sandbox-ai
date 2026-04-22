"""Tests for core/hydration.py — Pydantic config model and Jinja2 rendering pipeline."""


import os
from pathlib import Path

import pytest
from core.hydration import (
    AdminConfig,
    CoreConfig,
    DbPostgresConfig,
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
mcp_firecrawl = false
mcp_puppeteer = false

[components.db_postgres]
enabled = true
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
        assert config.components_db_postgres.enabled is True
        assert config.components.mcp_firecrawl is False
        assert ".github.com" in config.proxy_whitelist.domains

    def test_missing_required_field_raises(self, tmp_path: Path) -> None:
        """Missing required field raises ValidationError."""
        broken = VALID_TOML.replace('name = "testproject"', "")
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(broken)
        with pytest.raises(Exception):  # Pydantic ValidationError
            SandboxConfig.from_toml(str(toml_path))


class TestDbPostgresConfigFields:
    """Task 3.1: DbPostgresConfig gains pg_user and pg_db fields."""

    def test_default_field_values(self) -> None:
        """pg_user defaults to 'sandbox', pg_db defaults to 'sandbox_db'."""
        db = DbPostgresConfig()
        assert db.pg_user == "sandbox"
        assert db.pg_db == "sandbox_db"

    def test_round_trip_through_from_toml(self, tmp_path: Path) -> None:
        """pg_user and pg_db survive from_toml() parsing with defaults."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = SandboxConfig.from_toml(str(toml_path))
        assert config.components_db_postgres.pg_user == "sandbox"
        assert config.components_db_postgres.pg_db == "sandbox_db"

    def test_custom_values_from_toml(self, tmp_path: Path) -> None:
        """Custom pg_user/pg_db values are parsed from TOML."""
        custom = VALID_TOML.replace(
            "[components.db_postgres]\nenabled = true\nexpose_host_ports = [5432]",
            "[components.db_postgres]\nenabled = true\nexpose_host_ports = [5432]\n"
            'pg_user = "custom_user"\npg_db = "custom_db"',
        )
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(custom)
        config = SandboxConfig.from_toml(str(toml_path))
        assert config.components_db_postgres.pg_user == "custom_user"
        assert config.components_db_postgres.pg_db == "custom_db"


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

    def test_extras_context_keys(self, tmp_path: Path) -> None:
        """Task 4.1: build_jinja_context includes pg_user, pg_db, and firecrawl IPs."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = SandboxConfig.from_toml(str(toml_path))

        ctx = build_jinja_context(
            config=config,
            base_index=0,
            proxy_password="testpass",
            instance_dir="/sandboxes/test",
        )

        # pg_user and pg_db from DbPostgresConfig
        assert ctx["pg_user"] == "sandbox"
        assert ctx["pg_db"] == "sandbox_db"
        # Firecrawl IPs from derive_static_ips (already flow through **ips)
        assert "mcp_firecrawl_isolated_ip" in ctx
        assert "mcp_firecrawl_proxy_ip" in ctx
        assert ctx["mcp_firecrawl_isolated_ip"] == "10.100.0.55"
        assert ctx["mcp_firecrawl_proxy_ip"] == "10.100.1.55"

    def test_custom_claude_rules_loaded_when_present(self, tmp_path: Path) -> None:
        """custom_claude_rules contains file content when custom/config/core/CLAUDE.md exists."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = SandboxConfig.from_toml(str(toml_path))

        instance_dir = tmp_path / "instance"
        custom_rules = instance_dir / "custom" / "config" / "core" / "CLAUDE.md"
        custom_rules.parent.mkdir(parents=True)
        custom_rules.write_text("Always use pytest.\n")

        ctx = build_jinja_context(
            config=config,
            base_index=0,
            proxy_password="x",
            instance_dir=str(instance_dir),
        )
        assert ctx["custom_claude_rules"] == "Always use pytest."

    def test_custom_claude_rules_empty_when_absent(self, tmp_path: Path) -> None:
        """custom_claude_rules is '' when custom/config/core/CLAUDE.md does not exist."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = SandboxConfig.from_toml(str(toml_path))

        ctx = build_jinja_context(
            config=config,
            base_index=0,
            proxy_password="x",
            instance_dir=str(tmp_path / "nonexistent"),
        )
        assert ctx["custom_claude_rules"] == ""


class TestCoreConfigResourceLimits:
    """Task 8.1, 8.3: CoreConfig mem_limit and cpus fields with defaults."""

    def test_core_config_defaults(self) -> None:
        """CoreConfig accepts mem_limit and cpus with correct defaults."""
        core = CoreConfig()
        assert core.mem_limit == "8gb"
        assert core.cpus == 4.0

    def test_core_config_custom_values(self) -> None:
        """CoreConfig accepts custom mem_limit and cpus."""
        core = CoreConfig(mem_limit="16gb", cpus=8.0)
        assert core.mem_limit == "16gb"
        assert core.cpus == 8.0

    def test_core_config_backward_compatibility(self, tmp_path: Path) -> None:
        """Omitted mem_limit and cpus fields use defaults."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = SandboxConfig.from_toml(str(toml_path))
        assert config.core.mem_limit == "8gb"
        assert config.core.cpus == 4.0


class TestAdminConfigResourceLimits:
    """Task 8.2, 8.4: AdminConfig mem_limit and cpus fields with defaults."""

    def test_admin_config_defaults(self) -> None:
        """AdminConfig accepts mem_limit and cpus with correct defaults."""
        admin = AdminConfig()
        assert admin.mem_limit == "8gb"
        assert admin.cpus == 4.0

    def test_admin_config_custom_values(self) -> None:
        """AdminConfig accepts custom mem_limit and cpus."""
        admin = AdminConfig(mem_limit="4gb", cpus=2.0)
        assert admin.mem_limit == "4gb"
        assert admin.cpus == 2.0

    def test_admin_config_backward_compatibility(self, tmp_path: Path) -> None:
        """Omitted mem_limit and cpus fields use defaults."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = SandboxConfig.from_toml(str(toml_path))
        assert config.admin.mem_limit == "8gb"
        assert config.admin.cpus == 4.0


class TestBuildJinjaContextResourceLimits:
    """Tasks 8.5-8.8: build_jinja_context resource limit keys."""

    def test_core_resource_keys_present(self, tmp_path: Path) -> None:
        """Context includes core_mem_limit, core_memswap_limit, core_cpus."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = SandboxConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(config=config, base_index=0, proxy_password="x", instance_dir="/tmp/x")
        assert ctx["core_mem_limit"] == "8gb"
        assert ctx["core_memswap_limit"] == "8gb"
        assert ctx["core_cpus"] == "4.0"

    def test_admin_resource_keys_present(self, tmp_path: Path) -> None:
        """Context includes admin_mem_limit, admin_memswap_limit, admin_cpus."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = SandboxConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(config=config, base_index=0, proxy_password="x", instance_dir="/tmp/x")
        assert ctx["admin_mem_limit"] == "8gb"
        assert ctx["admin_memswap_limit"] == "8gb"
        assert ctx["admin_cpus"] == "4.0"

    def test_core_memswap_equals_mem_limit(self, tmp_path: Path) -> None:
        """core_memswap_limit always equals core_mem_limit (zero swap)."""
        custom_toml = VALID_TOML.replace(
            '[core]\nshm_size = "2gb"',
            '[core]\nmem_limit = "16gb"\nshm_size = "2gb"',
        )
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(custom_toml)
        config = SandboxConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(config=config, base_index=0, proxy_password="x", instance_dir="/tmp/x")
        assert ctx["core_mem_limit"] == "16gb"
        assert ctx["core_memswap_limit"] == "16gb"

    def test_admin_memswap_equals_mem_limit(self, tmp_path: Path) -> None:
        """admin_memswap_limit always equals admin_mem_limit (zero swap)."""
        custom_toml = VALID_TOML.replace(
            '[admin]\nshm_size = "2gb"',
            '[admin]\nmem_limit = "4gb"\nshm_size = "2gb"',
        )
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(custom_toml)
        config = SandboxConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(config=config, base_index=0, proxy_password="x", instance_dir="/tmp/x")
        assert ctx["admin_mem_limit"] == "4gb"
        assert ctx["admin_memswap_limit"] == "4gb"


class TestScaffoldTemplateResourceLimits:
    """Task 8.9: Scaffold _SANDBOX_TOML_TEMPLATE includes resource limit defaults."""

    def test_scaffold_template_contains_resource_limits(self, tmp_path: Path) -> None:
        """Scaffold output contains mem_limit and cpus in both [core] and [admin]."""
        from core.scaffold import write_sandbox_toml

        instance = tmp_path / "instance"
        instance.mkdir()
        write_sandbox_toml(
            str(instance), "testproject", "/home/dev/test", "sandbox",
        )
        content = (instance / "sandbox.toml").read_text()

        # Verify [core] section has mem_limit and cpus
        core_section = content.split("[core]")[1].split("[")[0]
        assert 'mem_limit = "8gb"' in core_section
        assert "cpus = 4.0" in core_section

        # Verify [admin] section has mem_limit and cpus
        admin_section = content.split("[admin]")[1].split("[")[0]
        assert 'mem_limit = "8gb"' in admin_section
        assert "cpus = 4.0" in admin_section


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
            "log/admin", "log/core", "cache/core/.claude", "cache/admin/tmux_resurrect",
            "custom/config/admin", "custom/config/core",
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
        (instance / "cache" / "core" / ".claude" / "settings.json").write_text("PRECIOUS")
        (instance / "log" / "admin" / "test.log").write_text("PRECIOUS")

        render_templates(ctx, str(tooling), str(instance), db_postgres=True, mcp_firecrawl=True)

        assert (instance / "sandbox.toml").read_text() == "PRECIOUS"
        assert (instance / ".sandbox.env").read_text() == "PRECIOUS"
        assert (instance / "custom" / "config" / "admin" / "custom.zshrc").read_text() == "PRECIOUS"
        assert (instance / "cache" / "core" / ".claude" / "settings.json").read_text() == "PRECIOUS"
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

    def test_renders_and_copies_configs(self, tooling_and_instance: tuple[Path, Path]) -> None:
        """Config files are rendered (Jinja2) or statically copied to instance.

        .zshrc, .tmux.conf, .bashrc, .npmrc, .gitconfig, and CLAUDE.md are now
        rendered through the Jinja2 pipeline (not statically copied). Static files
        (.claude.json, gitmux.conf, starship.toml) are still copied as-is.
        Assertions verify file existence at the expected output paths.
        """
        tooling, instance = tooling_and_instance
        ctx = _build_test_context(str(instance))

        render_templates(ctx, str(tooling), str(instance), db_postgres=False, mcp_firecrawl=False)

        assert (instance / "config" / "admin" / ".zshrc").exists()
        assert (instance / "config" / "core" / ".bashrc").exists()
        assert (instance / "config" / "core" / "CLAUDE.md").exists()

    def test_no_unresolved_jinja_markers(self, tooling_and_instance: tuple[Path, Path]) -> None:
        """T1: No rendered file under instance dir contains literal {{ markers.

        This test scans all rendered output for surviving Jinja2 template syntax
        after a full render_templates() call. It catches future regressions where
        a file is misclassified as static (and therefore not rendered), leaving
        {{ }} markers in the deployed config.

        Synthetic stub limitation: The fixture tooling plane uses minimal stubs,
        not the real .config/ files. This means the test would NOT have detected
        the original Defect 1 (stubs contained no {{ }} markers). Full
        content-level coverage requires T2/T3 integration tests against the real
        tooling plane (separate change).
        """
        tooling, instance = tooling_and_instance
        ctx = _build_test_context(str(instance))

        render_templates(ctx, str(tooling), str(instance), db_postgres=True, mcp_firecrawl=True)

        violations: list[str] = []
        for root, _dirs, files in os.walk(str(instance)):
            for fname in files:
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath) as f:
                        content = f.read()
                except (UnicodeDecodeError, PermissionError):
                    continue
                if "{{" in content:
                    rel = os.path.relpath(fpath, str(instance))
                    violations.append(rel)

        assert violations == [], (
            f"Unresolved Jinja2 markers found in rendered files: {violations}"
        )

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
        "proxy_url_core": "http://proxyuser:testpass@proxy:3128",
        "core_base_image": "cgr.dev/chainguard/wolfi-base:latest",
        "admin_base_image": "debian:trixie-slim",
        "core_distro_family": "wolfi",
        "admin_distro_family": "debian",
        "host_uid": "1000",
        "core_pids_limit": 400,
        "admin_pids_limit": 400,
        "core_shm_size": "2gb",
        "admin_shm_size": "2gb",
        "core_mem_limit": "8gb",
        "core_memswap_limit": "8gb",
        "core_cpus": "4.0",
        "admin_mem_limit": "8gb",
        "admin_memswap_limit": "8gb",
        "admin_cpus": "4.0",
        "runtime": "runsc",
        "dns_image": "coredns/coredns:1.11.1",
        "proxy_image": "ubuntu/squid:latest",
        "nvm_version": "0.39.7",
        "node_version": "20.12.2",
        "runtimes": {"python": True, "typescript": True, "rust": True, "go": False},
        "proxy_whitelist_domains": [".github.com", ".npmjs.com"],
        "pg_user": "sandbox",
        "pg_db": "sandbox_db",
        "warmup_prompt": "",
        "git_user": "Agent",
        "git_email": "agent@sandbox.local",
        "custom_config_core": "/home/agent/.sandbox/custom",
        "custom_config_admin": "/home/human/.sandbox/custom",
        "tmux_resurrect_dir": "/home/human/.sandbox/tmux_resurrect",
        "db_postgres_enabled": True,
        "mcp_firecrawl_enabled": False,
        "custom_claude_rules": "",
    }


class TestValidateTemplates:
    """Coverage tests for validate_templates dry-run function."""

    def test_validate_all_valid(self, tmp_path: Path) -> None:
        """All templates valid returns (count, [])."""
        from core.hydration import validate_templates

        tooling = _build_minimal_tooling(tmp_path)
        ctx = _build_test_context(str(tmp_path / "inst"))
        count, errors = validate_templates(
            ctx, str(tooling), db_postgres=False, mcp_firecrawl=False,
        )
        assert count > 0
        assert errors == []

    def test_validate_with_firecrawl(self, tmp_path: Path) -> None:
        """mcp_firecrawl=True checks firecrawl template and Dockerfile."""
        from core.hydration import validate_templates

        tooling = _build_minimal_tooling(tmp_path)
        ctx = _build_test_context(str(tmp_path / "inst"))
        count, errors = validate_templates(
            ctx, str(tooling), db_postgres=False, mcp_firecrawl=True,
        )
        assert count > 0
        assert errors == []

    def test_validate_missing_template(self, tmp_path: Path) -> None:
        """Missing template file produces TemplateNotFound error."""
        from core.hydration import validate_templates

        tooling = _build_minimal_tooling(tmp_path)
        (tooling / ".docker" / "compose.yml").unlink()
        ctx = _build_test_context(str(tmp_path / "inst"))
        count, errors = validate_templates(
            ctx, str(tooling), db_postgres=False, mcp_firecrawl=False,
        )
        assert any("Template not found" in e for e in errors)

    def test_validate_undefined_variable(self, tmp_path: Path) -> None:
        """Undefined variable in template produces UndefinedError."""
        from core.hydration import validate_templates

        tooling = _build_minimal_tooling(tmp_path)
        (tooling / ".docker" / "compose.yml").write_text("{{ nonexistent_var }}")
        ctx = _build_test_context(str(tmp_path / "inst"))
        count, errors = validate_templates(
            ctx, str(tooling), db_postgres=False, mcp_firecrawl=False,
        )
        assert any("Undefined variable" in e for e in errors)

    def test_validate_syntax_error(self, tmp_path: Path) -> None:
        """Syntax error in template produces TemplateSyntaxError."""
        from core.hydration import validate_templates

        tooling = _build_minimal_tooling(tmp_path)
        (tooling / ".docker" / "compose.yml").write_text("{% if %}broken{% endif %}")
        ctx = _build_test_context(str(tmp_path / "inst"))
        count, errors = validate_templates(
            ctx, str(tooling), db_postgres=False, mcp_firecrawl=False,
        )
        assert any("Syntax error" in e for e in errors)

    def test_validate_missing_static_file(self, tmp_path: Path) -> None:
        """Missing static file produces error."""
        from core.hydration import validate_templates

        tooling = _build_minimal_tooling(tmp_path)
        (tooling / ".config" / "core" / ".claude.json").unlink()
        ctx = _build_test_context(str(tmp_path / "inst"))
        count, errors = validate_templates(
            ctx, str(tooling), db_postgres=False, mcp_firecrawl=False,
        )
        assert any("Static file missing" in e for e in errors)
        assert any(".claude.json" in e for e in errors)


def _build_minimal_tooling(tmp_path: Path) -> Path:
    """Build minimal tooling plane for validate_templates tests.

    Config file set matches the updated _JINJA_RENDERED_CONFIG and
    _STATIC_CONFIG_* lists so TestValidateTemplates exercises the correct
    template and static file sets.
    """
    tooling = tmp_path / "tooling"
    docker_dir = tooling / ".docker"
    docker_dir.mkdir(parents=True)
    (docker_dir / "compose.yml").write_text("# {{ project_name }}\n")
    core_dir = docker_dir / "core"
    core_dir.mkdir()
    (core_dir / "Dockerfile.core.wolfi").write_text("FROM {{ core_base_image }}\n")
    (core_dir / "entrypoint.sh").write_text("#!/bin/bash\n")
    admin_dir = docker_dir / "admin"
    admin_dir.mkdir()
    (admin_dir / "Dockerfile.admin.debian").write_text("FROM {{ admin_base_image }}\n")
    (admin_dir / "entrypoint.sh").write_text("#!/bin/sh\n")
    extras_dir = docker_dir / "extras"
    extras_dir.mkdir()
    (extras_dir / "mcp-firecrawl.yml").write_text("# firecrawl\n")
    (extras_dir / "Dockerfile.mcp-firecrawl").write_text("FROM node\n")

    config_dir = tooling / ".config"
    dns_dir = config_dir / "dns-sidecar"
    dns_dir.mkdir(parents=True)
    (dns_dir / "Corefile").write_text("# {{ project_name }}\n")
    proxy_dir = config_dir / "proxy"
    proxy_dir.mkdir(parents=True)
    (proxy_dir / "squid.conf").write_text("# {{ proxy_ip }}\n")
    (proxy_dir / "ERR_SANDBOX_403").write_text("DENIED\n")
    # Admin: rendered templates + static files
    admin_cfg = config_dir / "admin"
    admin_cfg.mkdir(parents=True)
    # Rendered (.zshrc, .tmux.conf)
    (admin_cfg / ".zshrc").write_text("# zshrc {{ custom_config_admin }}\n")
    (admin_cfg / ".tmux.conf").write_text("# tmux {{ tmux_resurrect_dir }}\n")
    # Static (gitmux.conf, starship.toml)
    for f in ["gitmux.conf", "starship.toml"]:
        (admin_cfg / f).write_text(f"# {f}\n")
    # Core: rendered templates + static files
    core_cfg = config_dir / "core"
    core_cfg.mkdir(parents=True)
    # Rendered (.bashrc, .npmrc, .gitconfig, CLAUDE.md)
    (core_cfg / ".bashrc").write_text("# bashrc {{ custom_config_core }}\n")
    (core_cfg / ".npmrc").write_text("proxy={{ proxy_url_core }}\n")
    (core_cfg / ".gitconfig").write_text("# git {{ git_user }} {{ git_email }}\n")
    (core_cfg / "CLAUDE.md").write_text("# CLAUDE {{ db_postgres_enabled }}\n")
    # Static (.claude.json)
    (core_cfg / ".claude.json").write_text('{"version": 1}\n')

    return tooling


class TestDbPostgresTemplateRendering:
    """Task 6.1: db-postgres.yml renders through Jinja2 with no ${...:-...} patterns."""

    def test_no_dollar_default_patterns(self, tmp_path: Path) -> None:
        """Rendered db-postgres.yml contains zero ${VAR:-default} patterns."""
        import re

        import jinja2

        ctx = _build_test_context(str(tmp_path / "inst"))
        template_content = (
            Path(__file__).parent.parent.parent
            / ".docker" / "extras" / "db-postgres.yml"
        ).read_text()

        env = jinja2.Environment(
            loader=jinja2.BaseLoader(),
            undefined=jinja2.StrictUndefined,
        )
        template = env.from_string(template_content)
        rendered = template.render(ctx)

        # No ${VAR:-default} patterns should remain
        dollar_defaults = re.findall(r"\$\{[^}]+:-[^}]+\}", rendered)
        assert dollar_defaults == [], (
            f"Found ${'{'}...:-...{'}'} patterns in rendered db-postgres.yml: "
            f"{dollar_defaults}"
        )

    def test_env_file_directive_present(self, tmp_path: Path) -> None:
        """Rendered db-postgres.yml contains env_file directive."""
        import jinja2

        ctx = _build_test_context(str(tmp_path / "inst"))
        template_content = (
            Path(__file__).parent.parent.parent
            / ".docker" / "extras" / "db-postgres.yml"
        ).read_text()

        env = jinja2.Environment(
            loader=jinja2.BaseLoader(),
            undefined=jinja2.StrictUndefined,
        )
        template = env.from_string(template_content)
        rendered = template.render(ctx)

        assert "env_file:" in rendered
        assert ".sandbox.env" in rendered


class TestMcpFirecrawlTemplateRendering:
    """Task 7.1: mcp-firecrawl.yml renders through Jinja2 with no ${...:-...}."""

    def test_no_dollar_default_patterns(self, tmp_path: Path) -> None:
        """Rendered mcp-firecrawl.yml has zero ${VAR:-default} patterns.

        Only bare ${VAR} for env_file secrets (FIRECRAWL_API_KEY,
        PG_USER, PG_PASSWORD, PG_DB) are allowed.
        """
        import re

        import jinja2

        ctx = _build_test_context(str(tmp_path / "inst"))
        template_content = (
            Path(__file__).parent.parent.parent
            / ".docker" / "extras" / "mcp-firecrawl.yml"
        ).read_text()

        env = jinja2.Environment(
            loader=jinja2.BaseLoader(),
            undefined=jinja2.StrictUndefined,
        )
        template = env.from_string(template_content)
        rendered = template.render(ctx)

        dollar_defaults = re.findall(r"\$\{[^}]+:-[^}]+\}", rendered)
        assert dollar_defaults == [], (
            f"Found ${'{'}...:-...{'}'} patterns in rendered "
            f"mcp-firecrawl.yml: {dollar_defaults}"
        )

    def test_env_file_directive_present(self, tmp_path: Path) -> None:
        """Rendered mcp-firecrawl.yml contains env_file directive."""
        import jinja2

        ctx = _build_test_context(str(tmp_path / "inst"))
        template_content = (
            Path(__file__).parent.parent.parent
            / ".docker" / "extras" / "mcp-firecrawl.yml"
        ).read_text()

        env = jinja2.Environment(
            loader=jinja2.BaseLoader(),
            undefined=jinja2.StrictUndefined,
        )
        template = env.from_string(template_content)
        rendered = template.render(ctx)

        assert "env_file:" in rendered
        assert ".sandbox.env" in rendered
