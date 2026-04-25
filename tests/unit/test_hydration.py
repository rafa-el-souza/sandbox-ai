"""Tests for core/hydration.py — Pydantic config model and Jinja2 rendering pipeline."""

import os
from pathlib import Path

import pytest
from core.hydration import (
    _JINJA_RENDERED_CONFIG,
    IMAGE_DIGESTS,
    AdminConfig,
    CoreConfig,
    DbPostgresConfig,
    ProxyWhitelistConfig,
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
        assert ctx["core_proxy_subnet"] == "10.100.1.0/24"
        assert ctx["dns_subnet"] == "10.100.2.0/24"
        assert ctx["admin_subnet"] == "10.100.3.0/24"
        assert ctx["admin_proxy_subnet"] == "10.100.4.0/24"
        assert ctx["egress_subnet"] == "10.100.5.0/24"
        assert ctx["coredns_dns_ip"] == "10.100.2.53"
        assert ctx["proxy_core_ip"] == "10.100.1.254"
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
        assert "github.com" in ctx["proxy_whitelist_domains_coredns"]

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
        assert "mcp_firecrawl_proxy_ip" in ctx
        assert "firecrawl_dns_ip" in ctx
        assert ctx["mcp_firecrawl_proxy_ip"] == "10.100.1.55"
        assert ctx["firecrawl_dns_ip"] == "10.100.2.55"

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
            str(instance),
            "testproject",
            "/home/dev/test",
            "sandbox",
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
        (docker_dir / "compose.yml").write_text("# rendered: {{ project_name }}\nsubnet: {{ isolated_subnet }}\n")
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
        # coredns
        dns_dir = config_dir / "coredns"
        dns_dir.mkdir(parents=True)
        (dns_dir / "Corefile").write_text("{{ proxy_whitelist_domains_coredns | join(' ') }}\n")
        # dnsdist
        dnsdist_dir = config_dir / "dnsdist"
        dnsdist_dir.mkdir(parents=True)
        (dnsdist_dir / "dnsdist.conf").write_text(
            'setLocal("0.0.0.0:53")\nnewServer({address="{{ coredns_dns_ip }}:53"})\n'
        )

        # proxy
        proxy_dir = config_dir / "proxy"
        proxy_dir.mkdir(parents=True)
        (proxy_dir / "squid.conf").write_text("acl src {{ isolated_subnet }}\n")
        (proxy_dir / "ERR_SANDBOX_403").write_text("DENIED\n")

        # admin static configs
        admin_cfg = config_dir / "admin"
        admin_cfg.mkdir(parents=True)
        for f in [".zshrc", ".tmux.conf", "gitmux.conf", "starship.toml", ".gitconfig"]:
            (admin_cfg / f).write_text(f"# {f}\n")

        # core static configs
        core_cfg = config_dir / "core"
        core_cfg.mkdir(parents=True)
        for f in [".bashrc", ".npmrc", ".gitconfig", ".claude.json", "CLAUDE.md"]:
            (core_cfg / f).write_text(f"# {f}\n")

        # Create instance dirs
        for d in [
            "docker/core",
            "docker/admin",
            "docker/extras",
            "config/admin",
            "config/core",
            "config/coredns",
            "config/dnsdist",
            "config/proxy",
            "log/admin",
            "log/core",
            "cache/core/.claude",
            "cache/admin/tmux_resurrect",
            "custom/config/admin",
            "custom/config/core",
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

        corefile = (instance / "config" / "coredns" / "Corefile").read_text()
        assert "github.com" in corefile

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
                except UnicodeDecodeError, PermissionError:
                    continue
                if "{{" in content:
                    rel = os.path.relpath(fpath, str(instance))
                    violations.append(rel)

        assert violations == [], f"Unresolved Jinja2 markers found in rendered files: {violations}"


def _build_test_context(instance_dir: str) -> dict[str, object]:
    """Build a minimal Jinja2 context for render tests."""
    from core.ipam import derive_static_ips, derive_subnets

    isolated, core_proxy, dns, admin, admin_proxy, egress = derive_subnets(0)
    ips = derive_static_ips(0)

    return {
        "project_name": "testproject",
        "instance_dir": instance_dir,
        "user_project_root": "/home/dev/testproject",
        "isolated_subnet": isolated,
        "core_proxy_subnet": core_proxy,
        "dns_subnet": dns,
        "admin_subnet": admin,
        "admin_proxy_subnet": admin_proxy,
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
        "dns_image": IMAGE_DIGESTS["coredns"],
        "proxy_image": IMAGE_DIGESTS["squid"],
        "dnsdist_image": IMAGE_DIGESTS["dnsdist"],
        "db_postgres_image": IMAGE_DIGESTS["postgres"],
        "nvm_version": "0.39.7",
        "node_version": "20.12.2",
        "runtimes": {"python": True, "typescript": True, "rust": True, "go": False},
        "proxy_whitelist_domains": [".github.com", ".npmjs.com"],
        "proxy_whitelist_domains_coredns": ["github.com", "npmjs.com"],
        "proxy_whitelist_read_only_domains": [".npmjs.com"],
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
            ctx,
            str(tooling),
            db_postgres=False,
            mcp_firecrawl=False,
        )
        assert count > 0
        assert errors == []

    def test_validate_with_firecrawl(self, tmp_path: Path) -> None:
        """mcp_firecrawl=True checks firecrawl template and Dockerfile."""
        from core.hydration import validate_templates

        tooling = _build_minimal_tooling(tmp_path)
        ctx = _build_test_context(str(tmp_path / "inst"))
        count, errors = validate_templates(
            ctx,
            str(tooling),
            db_postgres=False,
            mcp_firecrawl=True,
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
            ctx,
            str(tooling),
            db_postgres=False,
            mcp_firecrawl=False,
        )
        assert any("Template not found" in e for e in errors)

    def test_validate_undefined_variable(self, tmp_path: Path) -> None:
        """Undefined variable in template produces UndefinedError."""
        from core.hydration import validate_templates

        tooling = _build_minimal_tooling(tmp_path)
        (tooling / ".docker" / "compose.yml").write_text("{{ nonexistent_var }}")
        ctx = _build_test_context(str(tmp_path / "inst"))
        count, errors = validate_templates(
            ctx,
            str(tooling),
            db_postgres=False,
            mcp_firecrawl=False,
        )
        assert any("Undefined variable" in e for e in errors)

    def test_validate_syntax_error(self, tmp_path: Path) -> None:
        """Syntax error in template produces TemplateSyntaxError."""
        from core.hydration import validate_templates

        tooling = _build_minimal_tooling(tmp_path)
        (tooling / ".docker" / "compose.yml").write_text("{% if %}broken{% endif %}")
        ctx = _build_test_context(str(tmp_path / "inst"))
        count, errors = validate_templates(
            ctx,
            str(tooling),
            db_postgres=False,
            mcp_firecrawl=False,
        )
        assert any("Syntax error" in e for e in errors)

    def test_validate_missing_static_file(self, tmp_path: Path) -> None:
        """Missing static file produces error."""
        from core.hydration import validate_templates

        tooling = _build_minimal_tooling(tmp_path)
        (tooling / ".config" / "core" / ".claude.json").unlink()
        ctx = _build_test_context(str(tmp_path / "inst"))
        count, errors = validate_templates(
            ctx,
            str(tooling),
            db_postgres=False,
            mcp_firecrawl=False,
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
    dns_dir = config_dir / "coredns"
    dns_dir.mkdir(parents=True)
    (dns_dir / "Corefile").write_text("# {{ project_name }}\n")
    dnsdist_dir = config_dir / "dnsdist"
    dnsdist_dir.mkdir(parents=True)
    (dnsdist_dir / "dnsdist.conf").write_text(
        'setLocal("0.0.0.0:53")\nnewServer({address="{{ coredns_dns_ip }}:53"})\n'
    )
    proxy_dir = config_dir / "proxy"
    proxy_dir.mkdir(parents=True)
    (proxy_dir / "squid.conf").write_text("# {{ proxy_core_ip }}\n")
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
    # Rendered (admin/.gitconfig)
    (admin_cfg / ".gitconfig").write_text("# gitconfig {{ custom_config_admin }}\n")
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
        template_content = (Path(__file__).parent.parent.parent / ".docker" / "extras" / "db-postgres.yml").read_text()

        env = jinja2.Environment(
            loader=jinja2.BaseLoader(),
            undefined=jinja2.StrictUndefined,
        )
        template = env.from_string(template_content)
        rendered = template.render(ctx)

        # No ${VAR:-default} patterns should remain
        dollar_defaults = re.findall(r"\$\{[^}]+:-[^}]+\}", rendered)
        assert dollar_defaults == [], (
            f"Found ${'{'}...:-...{'}'} patterns in rendered db-postgres.yml: {dollar_defaults}"
        )

    def test_env_file_directive_present(self, tmp_path: Path) -> None:
        """Rendered db-postgres.yml contains env_file directive."""
        import jinja2

        ctx = _build_test_context(str(tmp_path / "inst"))
        template_content = (Path(__file__).parent.parent.parent / ".docker" / "extras" / "db-postgres.yml").read_text()

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
            Path(__file__).parent.parent.parent / ".docker" / "extras" / "mcp-firecrawl.yml"
        ).read_text()

        env = jinja2.Environment(
            loader=jinja2.BaseLoader(),
            undefined=jinja2.StrictUndefined,
        )
        template = env.from_string(template_content)
        rendered = template.render(ctx)

        dollar_defaults = re.findall(r"\$\{[^}]+:-[^}]+\}", rendered)
        assert dollar_defaults == [], (
            f"Found ${'{'}...:-...{'}'} patterns in rendered mcp-firecrawl.yml: {dollar_defaults}"
        )

    def test_env_file_directive_present(self, tmp_path: Path) -> None:
        """Rendered mcp-firecrawl.yml contains env_file directive."""
        import jinja2

        ctx = _build_test_context(str(tmp_path / "inst"))
        template_content = (
            Path(__file__).parent.parent.parent / ".docker" / "extras" / "mcp-firecrawl.yml"
        ).read_text()

        env = jinja2.Environment(
            loader=jinja2.BaseLoader(),
            undefined=jinja2.StrictUndefined,
        )
        template = env.from_string(template_content)
        rendered = template.render(ctx)

        assert "env_file:" in rendered
        assert ".sandbox.env" in rendered


class TestReadOnlyDomainsGeneration:
    """Task 7.4: read_only_domains.txt generation from context key."""

    def test_read_only_domains_file_generated(self, tmp_path: Path) -> None:
        """read_only_domains.txt is generated from proxy_whitelist_read_only_domains."""
        tooling = tmp_path / "tooling"
        instance = tmp_path / "instance"

        # Minimal tooling plane
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

        config_dir = tooling / ".config"
        for d in ["coredns", "dnsdist", "proxy", "admin", "core"]:
            (config_dir / d).mkdir(parents=True, exist_ok=True)
        (config_dir / "coredns" / "Corefile").write_text("# {{ project_name }}\n")
        (config_dir / "dnsdist" / "dnsdist.conf").write_text(
            'setLocal("0.0.0.0:53")\n'
            'newServer({address="{{ coredns_dns_ip }}:53"})\n'
        )
        (config_dir / "proxy" / "squid.conf").write_text("# {{ proxy_core_ip }}\n")
        (config_dir / "proxy" / "ERR_SANDBOX_403").write_text("DENIED\n")
        for f in [".zshrc", ".tmux.conf", "gitmux.conf", "starship.toml", ".gitconfig"]:
            (config_dir / "admin" / f).write_text(f"# {f}\n")
        for f in [".bashrc", ".npmrc", ".gitconfig", ".claude.json", "CLAUDE.md"]:
            (config_dir / "core" / f).write_text(f"# {f}\n")

        # Instance dirs
        for d in [
            "docker/core",
            "docker/admin",
            "docker/extras",
            "config/admin",
            "config/core",
            "config/coredns",
            "config/dnsdist",
            "config/proxy",
        ]:
            (instance / d).mkdir(parents=True, exist_ok=True)

        ctx = _build_test_context(str(instance))
        ctx["proxy_whitelist_read_only_domains"] = [".pypi.org", ".npmjs.com"]

        render_templates(ctx, str(tooling), str(instance), db_postgres=False, mcp_firecrawl=False)

        domains_file = instance / "config" / "proxy" / "read_only_domains.txt"
        assert domains_file.exists()
        content = domains_file.read_text()
        assert ".pypi.org" in content
        assert ".npmjs.com" in content

    def test_empty_read_only_domains_produces_empty_file(self, tmp_path: Path) -> None:
        """Empty read_only_domains produces an empty file."""
        tooling = tmp_path / "tooling"
        instance = tmp_path / "instance"

        docker_dir = tooling / ".docker"
        docker_dir.mkdir(parents=True)
        (docker_dir / "compose.yml").write_text("# {{ project_name }}\n")
        (docker_dir / "core").mkdir()
        (docker_dir / "core" / "Dockerfile.core.wolfi").write_text("FROM {{ core_base_image }}\n")
        (docker_dir / "core" / "entrypoint.sh").write_text("#!/bin/bash\n")
        (docker_dir / "admin").mkdir()
        (docker_dir / "admin" / "Dockerfile.admin.debian").write_text("FROM {{ admin_base_image }}\n")
        (docker_dir / "admin" / "entrypoint.sh").write_text("#!/bin/sh\n")

        config_dir = tooling / ".config"
        for d in ["coredns", "dnsdist", "proxy", "admin", "core"]:
            (config_dir / d).mkdir(parents=True, exist_ok=True)
        (config_dir / "coredns" / "Corefile").write_text("# {{ project_name }}\n")
        (config_dir / "dnsdist" / "dnsdist.conf").write_text(
            'setLocal("0.0.0.0:53")\n'
            'newServer({address="{{ coredns_dns_ip }}:53"})\n'
        )
        (config_dir / "proxy" / "squid.conf").write_text("# {{ proxy_core_ip }}\n")
        (config_dir / "proxy" / "ERR_SANDBOX_403").write_text("DENIED\n")
        for f in [".zshrc", ".tmux.conf", "gitmux.conf", "starship.toml", ".gitconfig"]:
            (config_dir / "admin" / f).write_text(f"# {f}\n")
        for f in [".bashrc", ".npmrc", ".gitconfig", ".claude.json", "CLAUDE.md"]:
            (config_dir / "core" / f).write_text(f"# {f}\n")

        for d in [
            "docker/core",
            "docker/admin",
            "docker/extras",
            "config/admin",
            "config/core",
            "config/coredns",
            "config/dnsdist",
            "config/proxy",
        ]:
            (instance / d).mkdir(parents=True, exist_ok=True)

        ctx = _build_test_context(str(instance))
        ctx["proxy_whitelist_read_only_domains"] = []

        render_templates(ctx, str(tooling), str(instance), db_postgres=False, mcp_firecrawl=False)

        domains_file = instance / "config" / "proxy" / "read_only_domains.txt"
        assert domains_file.exists()
        assert domains_file.read_text() == ""


class TestProxyWhitelistReadOnlyDomains:
    """Task 7.5: ProxyWhitelistConfig.read_only_domains field validation and defaults."""

    def test_default_is_empty_list(self) -> None:
        """read_only_domains defaults to empty list."""
        config = ProxyWhitelistConfig()
        assert config.read_only_domains == []

    def test_accepts_domain_list(self) -> None:
        """read_only_domains accepts a list of strings."""
        config = ProxyWhitelistConfig(read_only_domains=[".pypi.org", ".npmjs.com"])
        assert config.read_only_domains == [".pypi.org", ".npmjs.com"]

    def test_backward_compatible_with_missing_field(self, tmp_path: Path) -> None:
        """Existing sandbox.toml without read_only_domains still parses correctly."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = SandboxConfig.from_toml(str(toml_path))
        assert config.proxy_whitelist.read_only_domains == []


class TestImageDigestContextValues:
    """Task 7.6: image-related context values use digest format."""

    def test_context_dns_image_is_digest(self, tmp_path: Path) -> None:
        """dns_image uses @sha256: digest format."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = SandboxConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(config=config, base_index=0, proxy_password="x", instance_dir="/tmp/x")
        assert "@sha256:" in ctx["dns_image"]

    def test_context_proxy_image_is_digest(self, tmp_path: Path) -> None:
        """proxy_image uses @sha256: digest format."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = SandboxConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(config=config, base_index=0, proxy_password="x", instance_dir="/tmp/x")
        assert "@sha256:" in ctx["proxy_image"]

    def test_context_db_postgres_image_present(self, tmp_path: Path) -> None:
        """db_postgres_image is present in context."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = SandboxConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(config=config, base_index=0, proxy_password="x", instance_dir="/tmp/x")
        assert "db_postgres_image" in ctx
        assert "@sha256:" in ctx["db_postgres_image"]


class TestValidTomlBackwardCompatibility:
    """Task 7.7: VALID_TOML fixtures remain backward-compatible with mutable tags."""

    def test_mutable_tags_accepted(self, tmp_path: Path) -> None:
        """VALID_TOML with mutable tags parses without errors."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = SandboxConfig.from_toml(str(toml_path))
        # User-supplied mutable tags are accepted — defaults would be digests
        assert config.core.base_image == "cgr.dev/chainguard/wolfi-base:latest"
        assert config.admin.base_image == "debian:trixie-slim"


class TestImageDigestsDnsdist:
    """Wave 3: IMAGE_DIGESTS includes dnsdist entry."""

    def test_dnsdist_key_present(self) -> None:
        """IMAGE_DIGESTS contains 'dnsdist' key."""
        assert "dnsdist" in IMAGE_DIGESTS

    def test_dnsdist_value_format(self) -> None:
        """dnsdist value uses digest format with powerdns/dnsdist-19."""
        val = IMAGE_DIGESTS["dnsdist"]
        assert val.startswith("powerdns/dnsdist-19@sha256:")
        assert len(val.split("@sha256:")[1]) == 64

    def test_image_digests_has_6_entries(self) -> None:
        """IMAGE_DIGESTS has exactly 6 entries (wolfi_base, debian_trixie, squid, coredns, dnsdist, postgres)."""
        assert len(IMAGE_DIGESTS) == 6
        assert set(IMAGE_DIGESTS.keys()) == {"wolfi_base", "debian_trixie", "squid", "coredns", "dnsdist", "postgres"}


class TestSixSubnetContextKeys:
    """Wave 3: build_jinja_context returns 6 subnet + new IP keys."""

    def test_six_subnet_keys_present(self, tmp_path: Path) -> None:
        """Context contains all 6 subnet CIDR keys."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = SandboxConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(config=config, base_index=0, proxy_password="x", instance_dir="/tmp/x")
        subnet_keys = [
            "isolated_subnet",
            "core_proxy_subnet",
            "dns_subnet",
            "admin_subnet",
            "admin_proxy_subnet",
            "egress_subnet",
        ]
        for key in subnet_keys:
            assert key in ctx, f"Missing context key: {key}"

    def test_proxy_dual_ip_keys(self, tmp_path: Path) -> None:
        """Context includes proxy_core_ip and proxy_admin_ip."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = SandboxConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(config=config, base_index=0, proxy_password="x", instance_dir="/tmp/x")
        assert "proxy_core_ip" in ctx
        assert "proxy_admin_ip" in ctx

    def test_dnsdist_ip_keys(self, tmp_path: Path) -> None:
        """Context includes all 3 dnsdist IP keys."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = SandboxConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(config=config, base_index=0, proxy_password="x", instance_dir="/tmp/x")
        for key in ["dnsdist_isolated_ip", "dnsdist_dns_ip", "dnsdist_admin_ip"]:
            assert key in ctx, f"Missing context key: {key}"

    def test_coredns_ip_keys(self, tmp_path: Path) -> None:
        """Context includes all 3 coredns IP keys."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = SandboxConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(config=config, base_index=0, proxy_password="x", instance_dir="/tmp/x")
        for key in ["coredns_dns_ip", "coredns_admin_ip", "coredns_egress_ip"]:
            assert key in ctx, f"Missing context key: {key}"

    def test_db_postgres_admin_ip_key(self, tmp_path: Path) -> None:
        """Context includes db_postgres_admin_ip."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = SandboxConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(config=config, base_index=0, proxy_password="x", instance_dir="/tmp/x")
        assert "db_postgres_admin_ip" in ctx

    def test_firecrawl_dns_ip_key(self, tmp_path: Path) -> None:
        """Context includes firecrawl_dns_ip."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = SandboxConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(config=config, base_index=0, proxy_password="x", instance_dir="/tmp/x")
        assert "firecrawl_dns_ip" in ctx

    def test_dnsdist_image_key(self, tmp_path: Path) -> None:
        """Context includes dnsdist_image from IMAGE_DIGESTS."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = SandboxConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(config=config, base_index=0, proxy_password="x", instance_dir="/tmp/x")
        assert "dnsdist_image" in ctx
        assert ctx["dnsdist_image"] == IMAGE_DIGESTS["dnsdist"]

    def test_legacy_keys_absent(self, tmp_path: Path) -> None:
        """Legacy 3-subnet keys not in context."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = SandboxConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(config=config, base_index=0, proxy_password="x", instance_dir="/tmp/x")
        assert "dns_sidecar_ip" not in ctx
        assert "proxy_ip" not in ctx
        assert "proxy_subnet" not in ctx


class TestConfigRenderingRegistry:
    """Wave 3: _JINJA_RENDERED_CONFIG renamed and extended."""

    def test_coredns_corefile_in_registry(self) -> None:
        """Registry contains coredns/Corefile."""
        sources = [src for src, _dst in _JINJA_RENDERED_CONFIG]
        assert "coredns/Corefile" in sources

    def test_dnsdist_conf_in_registry(self) -> None:
        """Registry contains dnsdist/dnsdist.conf."""
        sources = [src for src, _dst in _JINJA_RENDERED_CONFIG]
        assert "dnsdist/dnsdist.conf" in sources

    def test_legacy_dns_sidecar_absent(self) -> None:
        """Registry does NOT contain dns-sidecar/Corefile."""
        sources = [src for src, _dst in _JINJA_RENDERED_CONFIG]
        assert "dns-sidecar/Corefile" not in sources


class TestDnsdistTemplateContent:
    """Wave 3: dnsdist.conf template contains required DNS exfiltration defense directives."""

    def test_dnsdist_wire_length_rule(self) -> None:
        """Template contains QNameWireLengthRule filter."""
        content = (Path(__file__).parent.parent.parent / ".config" / "dnsdist" / "dnsdist.conf").read_text()
        assert "QNameWireLengthRule(0, 65)" in content
        assert "DropAction()" in content

    def test_dnsdist_label_count_rule(self) -> None:
        """Template contains QNameLabelsCountRule filter."""
        content = (Path(__file__).parent.parent.parent / ".config" / "dnsdist" / "dnsdist.conf").read_text()
        assert "QNameLabelsCountRule(0, 7)" in content

    def test_dnsdist_set_local(self) -> None:
        """Template binds to 0.0.0.0:53."""
        content = (Path(__file__).parent.parent.parent / ".config" / "dnsdist" / "dnsdist.conf").read_text()
        assert 'setLocal("0.0.0.0:53")' in content

    def test_dnsdist_control_socket(self) -> None:
        """Template has localhost-only control socket."""
        content = (Path(__file__).parent.parent.parent / ".config" / "dnsdist" / "dnsdist.conf").read_text()
        assert 'controlSocket("127.0.0.1:5199")' in content

    def test_dnsdist_backend_coredns(self) -> None:
        """Template forwards to coredns via Jinja2 variable."""
        content = (Path(__file__).parent.parent.parent / ".config" / "dnsdist" / "dnsdist.conf").read_text()
        assert "{{ coredns_dns_ip }}" in content
        assert "newServer" in content


class TestSquidFirecrawlAcl:
    """Wave 3: proxy/squid.conf contains firecrawl ACL and safe_methods."""

    def test_firecrawl_src_acl(self) -> None:
        """squid.conf contains firecrawl_src source ACL."""
        content = (Path(__file__).parent.parent.parent / ".config" / "proxy" / "squid.conf").read_text()
        assert "acl firecrawl_src src" in content
        assert "{{ mcp_firecrawl_proxy_ip }}" in content

    def test_safe_methods_acl(self) -> None:
        """squid.conf contains safe_methods ACL."""
        content = (Path(__file__).parent.parent.parent / ".config" / "proxy" / "squid.conf").read_text()
        assert "acl safe_methods method GET HEAD OPTIONS" in content

    def test_firecrawl_allow_rule(self) -> None:
        """squid.conf contains firecrawl allow rule with safe_methods."""
        content = (Path(__file__).parent.parent.parent / ".config" / "proxy" / "squid.conf").read_text()
        assert "http_access allow firecrawl_src authenticated_users safe_methods whitelist" in content

    def test_firecrawl_after_agent_admin(self) -> None:
        """Firecrawl allow rule appears after agent/admin allows and before deny all."""
        content = (Path(__file__).parent.parent.parent / ".config" / "proxy" / "squid.conf").read_text()
        agent_pos = content.index("http_access allow agent_src")
        admin_pos = content.index("http_access allow admin_src")
        firecrawl_pos = content.index("http_access allow firecrawl_src")
        deny_pos = content.index("http_access deny all")
        assert agent_pos < admin_pos < firecrawl_pos < deny_pos


def _render_compose(tmp_path: Path) -> str:
    """Render compose.yml template through Jinja2 with StrictUndefined."""
    import jinja2

    ctx = _build_test_context(str(tmp_path / "inst"))
    template_content = (
        Path(__file__).parent.parent.parent / ".docker" / "compose.yml"
    ).read_text()
    env = jinja2.Environment(
        loader=jinja2.BaseLoader(),
        undefined=jinja2.StrictUndefined,
    )
    return env.from_string(template_content).render(ctx)


def _render_extras(tmp_path: Path, filename: str) -> str:
    """Render an extras template through Jinja2 with StrictUndefined."""
    import jinja2

    ctx = _build_test_context(str(tmp_path / "inst"))
    template_content = (
        Path(__file__).parent.parent.parent
        / ".docker" / "extras" / filename
    ).read_text()
    env = jinja2.Environment(
        loader=jinja2.BaseLoader(),
        undefined=jinja2.StrictUndefined,
    )
    return env.from_string(template_content).render(ctx)


class TestComposeSecurityBaseline:
    """5.T: x-security-baseline anchor and service inheritance."""

    def test_baseline_anchor_defined(self, tmp_path: Path) -> None:
        """compose.yml source contains x-security-baseline anchor."""
        raw = (
            Path(__file__).parent.parent.parent
            / ".docker" / "compose.yml"
        ).read_text()
        assert "x-security-baseline:" in raw
        assert "&security-baseline" in raw

    def test_baseline_contains_scalar_properties(
        self, tmp_path: Path,
    ) -> None:
        """Baseline has security_opt, cap_drop, ipc, init, read_only."""
        raw = (
            Path(__file__).parent.parent.parent
            / ".docker" / "compose.yml"
        ).read_text()
        # Find the baseline block (between x-security-baseline and networks:)
        start = raw.index("x-security-baseline:")
        end = raw.index("\nnetworks:")
        block = raw[start:end]
        assert "no-new-privileges:true" in block
        assert "cap_drop:" in block
        assert "ALL" in block
        assert "ipc: private" in block
        assert "init: true" in block
        assert "read_only: true" in block

    def test_baseline_excludes_list_properties(
        self, tmp_path: Path,
    ) -> None:
        """Baseline does NOT contain cap_add, sysctls, or tmpfs."""
        raw = (
            Path(__file__).parent.parent.parent
            / ".docker" / "compose.yml"
        ).read_text()
        start = raw.index("x-security-baseline:")
        end = raw.index("\nnetworks:")
        block = raw[start:end]
        assert "cap_add:" not in block
        assert "sysctls:" not in block
        assert "tmpfs:" not in block

    def test_core_overrides_read_only(self, tmp_path: Path) -> None:
        """Core service overrides baseline read_only to false."""
        rendered = _render_compose(tmp_path)
        # Extract core service block
        core_start = rendered.index("\n  core:")
        admin_start = rendered.index("\n  admin:")
        core_block = rendered[core_start:admin_start]
        assert "read_only: false" in core_block

    def test_admin_overrides_read_only(self, tmp_path: Path) -> None:
        """Admin service overrides baseline read_only to false."""
        rendered = _render_compose(tmp_path)
        admin_start = rendered.index("\n  admin:")
        admin_block = rendered[admin_start:]
        assert "read_only: false" in admin_block


class TestComposeNetworkDefinitions:
    """5.T: 6-network topology with correct properties."""

    def test_new_networks_defined(self, tmp_path: Path) -> None:
        """compose.yml defines core_proxy_net, dns_net, admin_net,
        admin_proxy_net."""
        rendered = _render_compose(tmp_path)
        for net in [
            "core_proxy_net:", "dns_net:",
            "admin_net:", "admin_proxy_net:",
        ]:
            assert net in rendered, f"Missing network: {net}"

    def test_new_networks_internal(self, tmp_path: Path) -> None:
        """All 4 new networks have internal: true."""
        raw = (
            Path(__file__).parent.parent.parent
            / ".docker" / "compose.yml"
        ).read_text()
        for net_name in [
            "core_proxy_net:", "dns_net:",
            "admin_net:", "admin_proxy_net:",
        ]:
            idx = raw.index(net_name)
            # Check within the next 200 chars
            block = raw[idx:idx + 200]
            assert "internal: true" in block, (
                f"{net_name} missing internal: true"
            )

    def test_proxy_net_removed(self, tmp_path: Path) -> None:
        """Legacy proxy_net network is no longer defined."""
        raw = (
            Path(__file__).parent.parent.parent
            / ".docker" / "compose.yml"
        ).read_text()
        # Extract networks block only
        net_start = raw.index("networks:")
        svc_start = raw.index("services:")
        net_block = raw[net_start:svc_start]
        # proxy_net should not appear as a top-level network
        # (core_proxy_net and admin_proxy_net contain "proxy_net"
        # as substring, so check for exact key)
        lines = net_block.split("\n")
        top_keys = [
            ln.strip().rstrip(":") for ln in lines
            if ln and not ln.startswith(" ") and ln.strip().endswith(":")
            and ln.strip() != "networks:"
        ]
        assert "proxy_net" not in top_keys


class TestComposeServiceNetworkMembership:
    """5.T: Zero-shared-network invariant, per-service membership."""

    def test_core_on_isolated_and_core_proxy_only(
        self, tmp_path: Path,
    ) -> None:
        """Core is on isolated_net and core_proxy_net only."""
        rendered = _render_compose(tmp_path)
        core_start = rendered.index("\n  core:")
        admin_start = rendered.index("\n  admin:")
        core_block = rendered[core_start:admin_start]
        assert "isolated_net:" in core_block
        assert "core_proxy_net:" in core_block
        assert "admin_net:" not in core_block
        assert "admin_proxy_net:" not in core_block

    def test_admin_on_admin_and_admin_proxy_only(
        self, tmp_path: Path,
    ) -> None:
        """Admin is on admin_net and admin_proxy_net only."""
        rendered = _render_compose(tmp_path)
        admin_start = rendered.index("\n  admin:")
        admin_block = rendered[admin_start:]
        assert "admin_net:" in admin_block
        assert "admin_proxy_net:" in admin_block
        assert "isolated_net:" not in admin_block
        assert "core_proxy_net:" not in admin_block

    def test_zero_shared_networks(self, tmp_path: Path) -> None:
        """Core and admin network sets have empty intersection."""
        rendered = _render_compose(tmp_path)
        core_start = rendered.index("\n  core:")
        admin_start = rendered.index("\n  admin:")
        core_block = rendered[core_start:admin_start]
        admin_block = rendered[admin_start:]
        all_nets = [
            "isolated_net", "core_proxy_net", "dns_net",
            "admin_net", "admin_proxy_net", "egress_net",
        ]
        core_nets = {n for n in all_nets if f"{n}:" in core_block}
        admin_nets = {n for n in all_nets if f"{n}:" in admin_block}
        assert core_nets & admin_nets == set(), (
            f"Shared networks: {core_nets & admin_nets}"
        )

    def test_coredns_exists_dns_sidecar_absent(
        self, tmp_path: Path,
    ) -> None:
        """Service named coredns exists; dns-sidecar does not."""
        rendered = _render_compose(tmp_path)
        assert "coredns:" in rendered
        assert "dns-sidecar:" not in rendered


class TestComposeDnsdistService:
    """5.T: dnsdist service definition and hardening."""

    def test_dnsdist_cap_add(self, tmp_path: Path) -> None:
        """dnsdist has cap_add NET_BIND_SERVICE."""
        rendered = _render_compose(tmp_path)
        dnsdist_start = rendered.index("\n  dnsdist:")
        proxy_start = rendered.index("\n  proxy:")
        dnsdist_block = rendered[dnsdist_start:proxy_start]
        assert "NET_BIND_SERVICE" in dnsdist_block

    def test_dnsdist_user(self, tmp_path: Path) -> None:
        """dnsdist runs as pdns:pdns."""
        rendered = _render_compose(tmp_path)
        dnsdist_start = rendered.index("\n  dnsdist:")
        proxy_start = rendered.index("\n  proxy:")
        dnsdist_block = rendered[dnsdist_start:proxy_start]
        assert 'user: "pdns:pdns"' in dnsdist_block

    def test_dnsdist_resource_limits(self, tmp_path: Path) -> None:
        """dnsdist has pids_limit 100 and mem_limit 512m."""
        rendered = _render_compose(tmp_path)
        dnsdist_start = rendered.index("\n  dnsdist:")
        proxy_start = rendered.index("\n  proxy:")
        dnsdist_block = rendered[dnsdist_start:proxy_start]
        assert "pids_limit: 100" in dnsdist_block
        assert "512m" in dnsdist_block

    def test_dnsdist_depends_on_coredns(self, tmp_path: Path) -> None:
        """dnsdist depends_on coredns: condition: service_healthy."""
        rendered = _render_compose(tmp_path)
        dnsdist_start = rendered.index("\n  dnsdist:")
        proxy_start = rendered.index("\n  proxy:")
        dnsdist_block = rendered[dnsdist_start:proxy_start]
        assert "coredns:" in dnsdist_block
        assert "service_healthy" in dnsdist_block

    def test_dnsdist_ip_forward_disabled(self, tmp_path: Path) -> None:
        """dnsdist sysctls contain ip_forward=0."""
        rendered = _render_compose(tmp_path)
        dnsdist_start = rendered.index("\n  dnsdist:")
        proxy_start = rendered.index("\n  proxy:")
        dnsdist_block = rendered[dnsdist_start:proxy_start]
        assert "net.ipv4.ip_forward=0" in dnsdist_block


class TestComposeDnsRouting:
    """5.T: Per-container DNS routing through dnsdist."""

    def test_core_dns_points_to_dnsdist(self, tmp_path: Path) -> None:
        """Core dns directive uses dnsdist_isolated_ip."""
        from core.ipam import derive_static_ips

        ips = derive_static_ips(0)
        rendered = _render_compose(tmp_path)
        core_start = rendered.index("\n  core:")
        admin_start = rendered.index("\n  admin:")
        core_block = rendered[core_start:admin_start]
        assert ips["dnsdist_isolated_ip"] in core_block

    def test_admin_dns_points_to_dnsdist(self, tmp_path: Path) -> None:
        """Admin dns directive uses dnsdist_admin_ip."""
        from core.ipam import derive_static_ips

        ips = derive_static_ips(0)
        rendered = _render_compose(tmp_path)
        admin_start = rendered.index("\n  admin:")
        admin_block = rendered[admin_start:]
        assert ips["dnsdist_admin_ip"] in admin_block


class TestComposeExtraHosts:
    """5.T: Per-container extra_hosts resolution."""

    def test_core_extra_hosts_proxy(self, tmp_path: Path) -> None:
        """Core extra_hosts has proxy with proxy_core_ip."""
        from core.ipam import derive_static_ips

        ips = derive_static_ips(0)
        rendered = _render_compose(tmp_path)
        core_start = rendered.index("\n  core:")
        admin_start = rendered.index("\n  admin:")
        core_block = rendered[core_start:admin_start]
        assert f"proxy:{ips['proxy_core_ip']}" in core_block

    def test_admin_extra_hosts_proxy(self, tmp_path: Path) -> None:
        """Admin extra_hosts has proxy with proxy_admin_ip."""
        from core.ipam import derive_static_ips

        ips = derive_static_ips(0)
        rendered = _render_compose(tmp_path)
        admin_start = rendered.index("\n  admin:")
        admin_block = rendered[admin_start:]
        assert f"proxy:{ips['proxy_admin_ip']}" in admin_block


class TestComposeIpForward:
    """5.T: All services have ip_forward=0."""

    def test_core_ip_forward(self, tmp_path: Path) -> None:
        """Core sysctls contain ip_forward=0."""
        rendered = _render_compose(tmp_path)
        core_start = rendered.index("\n  core:")
        admin_start = rendered.index("\n  admin:")
        core_block = rendered[core_start:admin_start]
        assert "net.ipv4.ip_forward=0" in core_block

    def test_admin_ip_forward(self, tmp_path: Path) -> None:
        """Admin sysctls contain ip_forward=0."""
        rendered = _render_compose(tmp_path)
        admin_start = rendered.index("\n  admin:")
        admin_block = rendered[admin_start:]
        assert "net.ipv4.ip_forward=0" in admin_block


class TestComposeNoProxy:
    """5.T: Per-container NO_PROXY scoping."""

    def test_core_no_proxy_scoped(self, tmp_path: Path) -> None:
        """Core NO_PROXY includes isolated/core_proxy,
        excludes admin subnets."""
        from core.ipam import derive_subnets

        subnets = derive_subnets(0)
        rendered = _render_compose(tmp_path)
        core_start = rendered.index("\n  core:")
        admin_start = rendered.index("\n  admin:")
        core_block = rendered[core_start:admin_start]
        # isolated_subnet is subnets[0], core_proxy is subnets[1]
        assert subnets[0] in core_block  # isolated_subnet
        assert subnets[1] in core_block  # core_proxy_subnet
        assert subnets[3] not in core_block  # admin_subnet

    def test_admin_no_proxy_scoped(self, tmp_path: Path) -> None:
        """Admin NO_PROXY includes admin/admin_proxy,
        excludes core subnets."""
        from core.ipam import derive_subnets

        subnets = derive_subnets(0)
        rendered = _render_compose(tmp_path)
        admin_start = rendered.index("\n  admin:")
        admin_block = rendered[admin_start:]
        # admin_subnet is subnets[3], admin_proxy is subnets[4]
        assert subnets[3] in admin_block  # admin_subnet
        assert subnets[4] in admin_block  # admin_proxy_subnet
        assert subnets[0] not in admin_block  # isolated_subnet


class TestComposeDependsOn:
    """5.T: Service dependency chain."""

    def test_core_depends_on_dnsdist(self, tmp_path: Path) -> None:
        """Core depends_on includes dnsdist."""
        rendered = _render_compose(tmp_path)
        core_start = rendered.index("\n  core:")
        admin_start = rendered.index("\n  admin:")
        core_block = rendered[core_start:admin_start]
        assert "dnsdist:" in core_block
        assert "service_healthy" in core_block

    def test_admin_depends_on_dnsdist(self, tmp_path: Path) -> None:
        """Admin depends_on includes dnsdist."""
        rendered = _render_compose(tmp_path)
        admin_start = rendered.index("\n  admin:")
        admin_block = rendered[admin_start:]
        assert "dnsdist:" in admin_block
        assert "service_healthy" in admin_block


class TestDbPostgresTemplate:
    """5.T: db-postgres.yml topology and hardening."""

    def test_db_postgres_admin_net(self, tmp_path: Path) -> None:
        """db-postgres has admin_net membership."""
        rendered = _render_extras(tmp_path, "db-postgres.yml")
        assert "admin_net:" in rendered

    def test_db_postgres_ip_forward(self, tmp_path: Path) -> None:
        """db-postgres sysctls contain ip_forward=0."""
        rendered = _render_extras(tmp_path, "db-postgres.yml")
        assert "net.ipv4.ip_forward=0" in rendered

    def test_db_postgres_admin_ip(self, tmp_path: Path) -> None:
        """db-postgres has db_postgres_admin_ip on admin_net."""
        from core.ipam import derive_static_ips

        ips = derive_static_ips(0)
        rendered = _render_extras(tmp_path, "db-postgres.yml")
        assert ips["db_postgres_admin_ip"] in rendered


class TestMcpFirecrawlTemplate:
    """5.T: mcp-firecrawl.yml topology."""

    def test_firecrawl_dns_points_to_dnsdist(
        self, tmp_path: Path,
    ) -> None:
        """Firecrawl dns uses dnsdist_dns_ip."""
        from core.ipam import derive_static_ips

        ips = derive_static_ips(0)
        rendered = _render_extras(tmp_path, "mcp-firecrawl.yml")
        assert ips["dnsdist_dns_ip"] in rendered

    def test_firecrawl_no_dns_sidecar_refs(
        self, tmp_path: Path,
    ) -> None:
        """Firecrawl has no dns-sidecar references."""
        rendered = _render_extras(tmp_path, "mcp-firecrawl.yml")
        assert "dns-sidecar" not in rendered

    def test_firecrawl_depends_on_dnsdist(
        self, tmp_path: Path,
    ) -> None:
        """Firecrawl depends_on dnsdist."""
        rendered = _render_extras(tmp_path, "mcp-firecrawl.yml")
        assert "dnsdist:" in rendered
        assert "service_healthy" in rendered
