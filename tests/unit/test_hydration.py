"""Tests for core/hydration.py — Pydantic config model and Jinja2 rendering pipeline."""

import os
import re
from pathlib import Path

import pytest
from core.hydration import (
    _JINJA_RENDERED_CONFIG,
    IMAGE_REGISTRY,
    AdminConfig,
    CoreConfig,
    DbPostgresConfig,
    ImagePin,
    InstanceConfig,
    ProxyWhitelistConfig,
    build_jinja_context,
    render_templates,
)

# ─── Minimal valid TOML for InstanceConfig ─────────────────────────────────────

VALID_TOML = """\
[instance]
name = "testproject"
user_project_root = "/home/dev/testproject"
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


class TestInstanceConfig:
    def test_parse_valid_toml(self, tmp_path: Path) -> None:
        """InstanceConfig parses valid TOML without errors."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = InstanceConfig.from_toml(str(toml_path))
        assert config.instance.name == "testproject"
        assert config.instance.user_project_root == "/home/dev/testproject"
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
            InstanceConfig.from_toml(str(toml_path))


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
        config = InstanceConfig.from_toml(str(toml_path))
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
        config = InstanceConfig.from_toml(str(toml_path))
        assert config.components_db_postgres.pg_user == "custom_user"
        assert config.components_db_postgres.pg_db == "custom_db"


class TestBuildJinjaContext:
    def test_produces_correct_ips_at_index_zero(self, tmp_path: Path) -> None:
        """Context dict has correct IP values at base_index=0."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = InstanceConfig.from_toml(str(toml_path))

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
        config = InstanceConfig.from_toml(str(toml_path))

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
        config = InstanceConfig.from_toml(str(toml_path))

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
        config = InstanceConfig.from_toml(str(toml_path))

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
        config = InstanceConfig.from_toml(str(toml_path))

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
        config = InstanceConfig.from_toml(str(toml_path))

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
        config = InstanceConfig.from_toml(str(toml_path))
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
        config = InstanceConfig.from_toml(str(toml_path))
        assert config.admin.mem_limit == "8gb"
        assert config.admin.cpus == 4.0


class TestBuildJinjaContextResourceLimits:
    """Tasks 8.5-8.8: build_jinja_context resource limit keys."""

    def test_core_resource_keys_present(self, tmp_path: Path) -> None:
        """Context includes core_mem_limit, core_memswap_limit, core_cpus."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = InstanceConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(config=config, base_index=0, proxy_password="x", instance_dir="/tmp/x")
        assert ctx["core_mem_limit"] == "8gb"
        assert ctx["core_memswap_limit"] == "8gb"
        assert ctx["core_cpus"] == "4.0"

    def test_admin_resource_keys_present(self, tmp_path: Path) -> None:
        """Context includes admin_mem_limit, admin_memswap_limit, admin_cpus."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = InstanceConfig.from_toml(str(toml_path))
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
        config = InstanceConfig.from_toml(str(toml_path))
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
        config = InstanceConfig.from_toml(str(toml_path))
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
    def tooling_and_instance(self, tmp_path: Path, monkeypatch: object) -> tuple[Path, Path]:
        """Create a minimal templates root + instance dir; redirect loader to it."""
        tooling = tmp_path / "tooling"
        instance = tmp_path / "instance"

        # Create a minimal compose.yml template
        docker_dir = tooling / "docker"
        docker_dir.mkdir(parents=True)
        (docker_dir / "compose.yml").write_text("# rendered: {{ instance_name }}\nsubnet: {{ isolated_subnet }}\n")
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

        # CoreDNS Dockerfile (static copy, no Jinja)
        coredns_dir = docker_dir / "coredns"
        coredns_dir.mkdir()
        (coredns_dir / "Dockerfile.coredns").write_text("ARG CORE_BASE\nFROM ${CORE_BASE}\n")

        # Extras
        extras_dir = docker_dir / "extras"
        extras_dir.mkdir()
        (extras_dir / "db-postgres.yml").write_text("# postgres: {{ db_postgres_ip }}\n")
        (extras_dir / "mcp-firecrawl.yml").write_text("# firecrawl\n")
        (extras_dir / "Dockerfile.mcp-firecrawl").write_text("FROM node\n")

        # Config templates
        config_dir = tooling / "config"
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
        for f in [".bashrc", ".npmrc", ".gitconfig", "CLAUDE.md", "sshd_config"]:
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

        _patch_templates_root(monkeypatch, tooling)
        return tooling, instance

    def test_renders_compose(self, tooling_and_instance: tuple[Path, Path]) -> None:
        """compose.yml is rendered with Jinja2 substitutions."""
        tooling, instance = tooling_and_instance
        ctx = _build_test_context(str(instance))

        render_templates(ctx, str(instance), db_postgres=True, mcp_firecrawl=False)

        rendered = (instance / "docker" / "compose.yml").read_text()
        assert "rendered: testproject" in rendered
        assert "10.100.0.0/24" in rendered

    def test_renders_dockerfile_by_distro(self, tooling_and_instance: tuple[Path, Path]) -> None:
        """Dockerfile selected by base_distro_family, rendered as Dockerfile.core."""
        tooling, instance = tooling_and_instance
        ctx = _build_test_context(str(instance))

        render_templates(ctx, str(instance), db_postgres=False, mcp_firecrawl=False)

        core_df = (instance / "docker" / "core" / "Dockerfile.core").read_text()
        assert "cgr.dev/chainguard/wolfi-base:latest" in core_df

        admin_df = (instance / "docker" / "admin" / "Dockerfile.admin").read_text()
        assert "debian:trixie-slim" in admin_df

    def test_disabled_component_skips_extra(self, tooling_and_instance: tuple[Path, Path]) -> None:
        """Disabled components do not have their extras rendered."""
        tooling, instance = tooling_and_instance
        ctx = _build_test_context(str(instance))

        render_templates(ctx, str(instance), db_postgres=False, mcp_firecrawl=False)

        assert not (instance / "docker" / "extras" / "db-postgres.yml").exists()
        assert not (instance / "docker" / "extras" / "mcp-firecrawl.yml").exists()

    def test_enabled_component_renders_extra(self, tooling_and_instance: tuple[Path, Path]) -> None:
        """Enabled components have their extras rendered."""
        tooling, instance = tooling_and_instance
        ctx = _build_test_context(str(instance))

        render_templates(ctx, str(instance), db_postgres=True, mcp_firecrawl=False)

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

        render_templates(ctx, str(instance), db_postgres=True, mcp_firecrawl=True)

        assert (instance / "sandbox.toml").read_text() == "PRECIOUS"
        assert (instance / ".sandbox.env").read_text() == "PRECIOUS"
        assert (instance / "custom" / "config" / "admin" / "custom.zshrc").read_text() == "PRECIOUS"
        assert (instance / "cache" / "core" / ".claude" / "settings.json").read_text() == "PRECIOUS"
        assert (instance / "log" / "admin" / "test.log").read_text() == "PRECIOUS"

    def test_renders_corefile(self, tooling_and_instance: tuple[Path, Path]) -> None:
        """Corefile is rendered with whitelist domains."""
        tooling, instance = tooling_and_instance
        ctx = _build_test_context(str(instance))

        render_templates(ctx, str(instance), db_postgres=False, mcp_firecrawl=False)

        corefile = (instance / "config" / "coredns" / "Corefile").read_text()
        assert "github.com" in corefile

    def test_generates_allowed_domains(self, tooling_and_instance: tuple[Path, Path]) -> None:
        """allowed_domains.txt is generated from whitelist domains."""
        tooling, instance = tooling_and_instance
        ctx = _build_test_context(str(instance))

        render_templates(ctx, str(instance), db_postgres=False, mcp_firecrawl=False)

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

        render_templates(ctx, str(instance), db_postgres=False, mcp_firecrawl=False)

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
        not the real templates/config/ files. This means the test would NOT have detected
        the original Defect 1 (stubs contained no {{ }} markers). Full
        content-level coverage requires T2/T3 integration tests against the real
        tooling plane (separate change).
        """
        tooling, instance = tooling_and_instance
        ctx = _build_test_context(str(instance))

        render_templates(ctx, str(instance), db_postgres=True, mcp_firecrawl=True)

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


class TestWriteRestricted:
    """write_restricted bypasses umask and applies the requested mode."""

    def test_mode_640_under_umask_022(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from core.hydration import write_restricted

        old_umask = os.umask(0o022)
        try:
            target = tmp_path / "f.txt"
            write_restricted(str(target), "hello", 0o640)
            assert (target.stat().st_mode & 0o777) == 0o640
        finally:
            os.umask(old_umask)

    def test_mode_600_under_permissive_umask(self, tmp_path: Path) -> None:
        from core.hydration import write_restricted

        old_umask = os.umask(0o000)
        try:
            target = tmp_path / "secret"
            write_restricted(str(target), b"sekrit", 0o600)
            assert (target.stat().st_mode & 0o777) == 0o600
        finally:
            os.umask(old_umask)

    def test_overwrites_existing_file_at_mode(self, tmp_path: Path) -> None:
        from core.hydration import write_restricted

        target = tmp_path / "f"
        target.write_text("old")
        os.chmod(target, 0o644)
        write_restricted(str(target), "new", 0o640)
        assert target.read_text() == "new"
        assert (target.stat().st_mode & 0o777) == 0o640

    def test_accepts_str_or_bytes(self, tmp_path: Path) -> None:
        from core.hydration import write_restricted

        a = tmp_path / "a"
        b = tmp_path / "b"
        write_restricted(str(a), "abc", 0o640)
        write_restricted(str(b), b"abc", 0o640)
        assert a.read_bytes() == b.read_bytes() == b"abc"


class TestBridgeGidContextKey:
    """build_jinja_context populates in_container_workspace_bridge_gid when host is provided."""

    def test_key_present_when_host_provided(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from core.host_config import HostSettings

        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = InstanceConfig.from_toml(str(toml_path))

        monkeypatch.setattr("core.hydration.workspace_bridge_gid", lambda h: 201665)
        monkeypatch.setattr("core.hydration.in_container_gid_for_host_gid", lambda gid, u: 1665)

        host = HostSettings(docker_unprivileged_user="claude-sandbox")
        ctx = build_jinja_context(config, 0, "p", str(tmp_path), host=host)
        assert ctx["in_container_workspace_bridge_gid"] == 1665

    def test_key_absent_when_host_none(self, tmp_path: Path) -> None:
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = InstanceConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(config, 0, "p", str(tmp_path))
        assert "in_container_workspace_bridge_gid" not in ctx

    def test_missing_group_aborts_hydration(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from core.host_config import HostSettings, WorkspaceBridgeGroupMissingError

        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = InstanceConfig.from_toml(str(toml_path))

        def _raise(host: object) -> int:
            raise WorkspaceBridgeGroupMissingError("group 'sb-ws' does not exist")

        monkeypatch.setattr("core.hydration.workspace_bridge_gid", _raise)
        host = HostSettings(docker_unprivileged_user="claude-sandbox")
        with pytest.raises(WorkspaceBridgeGroupMissingError):
            build_jinja_context(config, 0, "p", str(tmp_path), host=host)


class TestComposeGroupAdd:
    """compose.yml renders group_add on core and admin services."""

    def test_core_and_admin_have_group_add(self, tmp_path: Path) -> None:
        rendered = _render_compose(tmp_path)
        # Crude split into service-level chunks; sufficient since we only check membership.
        assert "group_add:" in rendered
        # appearing twice — once on core, once on admin
        assert rendered.count("group_add:") == 2


class TestRenderTemplatesRestrictiveModes:
    """Hydration writes files at restrictive modes regardless of umask (Decision 6)."""

    def test_rendered_files_mode_640_under_022_umask(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        tooling = _build_minimal_tooling(tmp_path)
        instance = tmp_path / "instance"
        instance.mkdir()
        for d in [
            "docker/core",
            "docker/admin",
            "docker/coredns",
            "docker/extras",
            "config/coredns",
            "config/dnsdist",
            "config/proxy",
            "config/core",
            "config/admin",
            "secrets",
            "log/core",
            "log/admin",
            "cache/core/.claude",
            "cache/admin/tmux_resurrect",
            "custom/config/admin",
            "custom/config/core",
        ]:
            (instance / d).mkdir(parents=True, exist_ok=True)

        _patch_templates_root(monkeypatch, tooling)
        ctx = _build_test_context(str(instance))

        old_umask = os.umask(0o022)
        try:
            render_templates(ctx, str(instance), db_postgres=False, mcp_firecrawl=False)
        finally:
            os.umask(old_umask)

        # Spot-check several rendered files: all should be 0o640
        for rel in [
            "docker/compose.yml",
            "config/coredns/Corefile",
            "config/dnsdist/dnsdist.conf",
            "config/proxy/squid.conf",
            "config/core/.bashrc",
            "config/admin/.zshrc",
            "config/proxy/allowed_domains.txt",
            "config/proxy/read_only_domains.txt",
            "config/core/.claude.json",
        ]:
            path = instance / rel
            assert path.exists(), f"missing {rel}"
            mode = path.stat().st_mode & 0o777
            assert mode == 0o640, f"{rel} has mode {oct(mode)}, expected 0o640"


def _build_test_context(instance_dir: str) -> dict[str, object]:
    """Build a minimal Jinja2 context for render tests."""
    from core.ipam import derive_static_ips, derive_subnets

    isolated, core_proxy, dns, admin, admin_proxy, egress, ipc = derive_subnets(0)
    ips = derive_static_ips(0)

    return {
        "instance_name": "testproject",
        "instance_dir": instance_dir,
        "user_project_root": "/home/dev/testproject",
        "isolated_subnet": isolated,
        "core_proxy_subnet": core_proxy,
        "dns_subnet": dns,
        "admin_subnet": admin,
        "admin_proxy_subnet": admin_proxy,
        "egress_subnet": egress,
        "ipc_subnet": ipc,
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
        "dns_image": IMAGE_REGISTRY["coredns"].pinned,
        "proxy_image": IMAGE_REGISTRY["squid"].pinned,
        "dnsdist_image": IMAGE_REGISTRY["dnsdist"].pinned,
        "busybox_image": IMAGE_REGISTRY["busybox_musl"].pinned,
        "db_postgres_image": IMAGE_REGISTRY["postgres"].pinned,
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
        "in_container_workspace_bridge_gid": 1000,
    }


class TestValidateTemplates:
    """Coverage tests for validate_templates dry-run function."""

    def test_validate_all_valid(self, tmp_path: Path) -> None:
        """All templates valid returns (count, [])."""
        from core.hydration import validate_templates

        ctx = _build_test_context(str(tmp_path / "inst"))
        count, errors = validate_templates(
            ctx,
            db_postgres=False,
            mcp_firecrawl=False,
        )
        assert count > 0
        assert errors == []

    def test_validate_with_firecrawl(self, tmp_path: Path) -> None:
        """mcp_firecrawl=True checks firecrawl template and Dockerfile."""
        from core.hydration import validate_templates

        ctx = _build_test_context(str(tmp_path / "inst"))
        count, errors = validate_templates(
            ctx,
            db_postgres=False,
            mcp_firecrawl=True,
        )
        assert count > 0
        assert errors == []

    def test_validate_all_components_enabled(
        self,
        tmp_path: Path,
    ) -> None:
        """7.T: All real templates valid with all components enabled.

        validate_templates() renders compose.yml, db-postgres.yml,
        mcp-firecrawl.yml, Corefile, dnsdist.conf, squid.conf, and
        every other Jinja2 template with StrictUndefined against the
        packaged templates module — catching any variable that lacks
        a context key.
        """
        from core.hydration import validate_templates

        ctx = _build_test_context(str(tmp_path / "inst"))
        count, errors = validate_templates(
            ctx,
            db_postgres=True,
            mcp_firecrawl=True,
        )
        assert count > 0
        assert errors == [], f"UndefinedError with all components: {errors}"

    def test_validate_missing_template(self, tmp_path: Path, monkeypatch: object) -> None:
        """Missing template file produces TemplateNotFound error."""
        from core.hydration import validate_templates

        tooling = _build_minimal_tooling(tmp_path)
        (tooling / "docker" / "compose.yml").unlink()
        _patch_templates_root(monkeypatch, tooling)
        ctx = _build_test_context(str(tmp_path / "inst"))
        count, errors = validate_templates(
            ctx,
            db_postgres=False,
            mcp_firecrawl=False,
        )
        assert any("Template not found" in e for e in errors)

    def test_validate_undefined_variable(self, tmp_path: Path, monkeypatch: object) -> None:
        """Undefined variable in template produces UndefinedError."""
        from core.hydration import validate_templates

        tooling = _build_minimal_tooling(tmp_path)
        (tooling / "docker" / "compose.yml").write_text("{{ nonexistent_var }}")
        _patch_templates_root(monkeypatch, tooling)
        ctx = _build_test_context(str(tmp_path / "inst"))
        count, errors = validate_templates(
            ctx,
            db_postgres=False,
            mcp_firecrawl=False,
        )
        assert any("Undefined variable" in e for e in errors)

    def test_validate_syntax_error(self, tmp_path: Path, monkeypatch: object) -> None:
        """Syntax error in template produces TemplateSyntaxError."""
        from core.hydration import validate_templates

        tooling = _build_minimal_tooling(tmp_path)
        (tooling / "docker" / "compose.yml").write_text("{% if %}broken{% endif %}")
        _patch_templates_root(monkeypatch, tooling)
        ctx = _build_test_context(str(tmp_path / "inst"))
        count, errors = validate_templates(
            ctx,
            db_postgres=False,
            mcp_firecrawl=False,
        )
        assert any("Syntax error" in e for e in errors)

    def test_validate_missing_static_file(self, tmp_path: Path, monkeypatch: object) -> None:
        """Missing static file produces error."""
        from core.hydration import validate_templates

        tooling = _build_minimal_tooling(tmp_path)
        (tooling / "config" / "proxy" / "ERR_SANDBOX_403").unlink()
        _patch_templates_root(monkeypatch, tooling)
        ctx = _build_test_context(str(tmp_path / "inst"))
        count, errors = validate_templates(
            ctx,
            db_postgres=False,
            mcp_firecrawl=False,
        )
        assert any("Static file missing" in e for e in errors)
        assert any("ERR_SANDBOX_403" in e for e in errors)


def _build_minimal_tooling(tmp_path: Path) -> Path:
    """Build minimal templates root for validate_templates tests.

    Layout matches the packaged ``templates`` module (no leading dots);
    callers swap the PackageLoader and resource resolver via
    ``_patch_templates_root`` so validate_templates reads from this tree.
    """
    tooling = tmp_path / "tooling"
    docker_dir = tooling / "docker"
    docker_dir.mkdir(parents=True)
    (docker_dir / "compose.yml").write_text("# {{ instance_name }}\n")
    core_dir = docker_dir / "core"
    core_dir.mkdir()
    (core_dir / "Dockerfile.core.wolfi").write_text("FROM {{ core_base_image }}\n")
    (core_dir / "entrypoint.sh").write_text("#!/bin/bash\n")
    admin_dir = docker_dir / "admin"
    admin_dir.mkdir()
    (admin_dir / "Dockerfile.admin.debian").write_text("FROM {{ admin_base_image }}\n")
    (admin_dir / "entrypoint.sh").write_text("#!/bin/sh\n")
    coredns_dir = docker_dir / "coredns"
    coredns_dir.mkdir()
    (coredns_dir / "Dockerfile.coredns").write_text("ARG CORE_BASE\nFROM ${CORE_BASE}\n")
    extras_dir = docker_dir / "extras"
    extras_dir.mkdir()
    (extras_dir / "mcp-firecrawl.yml").write_text("# firecrawl\n")
    (extras_dir / "Dockerfile.mcp-firecrawl").write_text("FROM node\n")
    (extras_dir / "db-postgres.yml").write_text("# postgres: {{ db_postgres_admin_ip }}\n")

    config_dir = tooling / "config"
    dns_dir = config_dir / "coredns"
    dns_dir.mkdir(parents=True)
    (dns_dir / "Corefile").write_text("# {{ instance_name }}\n")
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
    (core_cfg / "sshd_config").write_text(
        "ListenAddress {{ core_ipc_ip }}\n"
        "Port 9999\n"
        "PermitRootLogin no\n"
        "AllowUsers agent\n"
        "PasswordAuthentication no\n"
        "KbdInteractiveAuthentication no\n"
        "PubkeyAuthentication yes\n"
        "HostKey /run/secrets/ipc_host_key\n"
        "AuthorizedKeysFile /run/secrets/authorized_keys\n"
        "X11Forwarding no\n"
        "AllowAgentForwarding no\n"
        "AllowTcpForwarding no\n"
        "PermitTunnel no\n"
        "AcceptEnv SANDBOX_WARMUP_PROMPT\n"
        "MaxSessions 10\n"
        "ClientAliveInterval 300\n"
        "ClientAliveCountMax 2\n"
    )

    return tooling


def _patch_templates_root(monkeypatch: object, tooling: Path) -> None:
    """Redirect hydration's PackageLoader and resource resolver to ``tooling``.

    Used by tests that mutate template files (delete/corrupt) to exercise
    the validate_templates error branches without touching the packaged
    templates module.
    """
    import core.hydration as hydration
    import jinja2

    monkeypatch.setattr(hydration, "_resource_files", lambda _name: tooling)  # type: ignore[attr-defined]

    def _fake_package_loader(*_args: object, **_kwargs: object) -> jinja2.FileSystemLoader:
        return jinja2.FileSystemLoader(str(tooling))

    monkeypatch.setattr(jinja2, "PackageLoader", _fake_package_loader)  # type: ignore[attr-defined]


class TestDbPostgresTemplateRendering:
    """Task 6.1: db-postgres.yml renders through Jinja2 with no ${...:-...} patterns."""

    def test_no_dollar_default_patterns(self, tmp_path: Path) -> None:
        """Rendered db-postgres.yml contains zero ${VAR:-default} patterns."""
        import re

        import jinja2

        ctx = _build_test_context(str(tmp_path / "inst"))
        template_content = (
            Path(__file__).parent.parent.parent / "src" / "templates" / "docker" / "extras" / "db-postgres.yml"
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
            f"Found ${'{'}...:-...{'}'} patterns in rendered db-postgres.yml: {dollar_defaults}"
        )

    def test_env_file_directive_present(self, tmp_path: Path) -> None:
        """Rendered db-postgres.yml contains env_file directive."""
        import jinja2

        ctx = _build_test_context(str(tmp_path / "inst"))
        template_content = (
            Path(__file__).parent.parent.parent / "src" / "templates" / "docker" / "extras" / "db-postgres.yml"
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
            Path(__file__).parent.parent.parent / "src" / "templates" / "docker" / "extras" / "mcp-firecrawl.yml"
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
            Path(__file__).parent.parent.parent / "src" / "templates" / "docker" / "extras" / "mcp-firecrawl.yml"
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
        (docker_dir / "compose.yml").write_text("# {{ instance_name }}\n")
        core_dir = docker_dir / "core"
        core_dir.mkdir()
        (core_dir / "Dockerfile.core.wolfi").write_text("FROM {{ core_base_image }}\n")
        (core_dir / "entrypoint.sh").write_text("#!/bin/bash\n")
        admin_dir = docker_dir / "admin"
        admin_dir.mkdir()
        (admin_dir / "Dockerfile.admin.debian").write_text("FROM {{ admin_base_image }}\n")
        (admin_dir / "entrypoint.sh").write_text("#!/bin/sh\n")
        coredns_dock_dir = docker_dir / "coredns"
        coredns_dock_dir.mkdir()
        (coredns_dock_dir / "Dockerfile.coredns").write_text("ARG CORE_BASE\nFROM ${CORE_BASE}\n")

        config_dir = tooling / ".config"
        for d in ["coredns", "dnsdist", "proxy", "admin", "core"]:
            (config_dir / d).mkdir(parents=True, exist_ok=True)
        (config_dir / "coredns" / "Corefile").write_text("# {{ instance_name }}\n")
        (config_dir / "dnsdist" / "dnsdist.conf").write_text(
            'setLocal("0.0.0.0:53")\nnewServer({address="{{ coredns_dns_ip }}:53"})\n'
        )
        (config_dir / "proxy" / "squid.conf").write_text("# {{ proxy_core_ip }}\n")
        (config_dir / "proxy" / "ERR_SANDBOX_403").write_text("DENIED\n")
        for f in [".zshrc", ".tmux.conf", "gitmux.conf", "starship.toml", ".gitconfig"]:
            (config_dir / "admin" / f).write_text(f"# {f}\n")
        for f in [".bashrc", ".npmrc", ".gitconfig", "CLAUDE.md", "sshd_config"]:
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

        render_templates(ctx, str(instance), db_postgres=False, mcp_firecrawl=False)

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
        (docker_dir / "compose.yml").write_text("# {{ instance_name }}\n")
        (docker_dir / "core").mkdir()
        (docker_dir / "core" / "Dockerfile.core.wolfi").write_text("FROM {{ core_base_image }}\n")
        (docker_dir / "core" / "entrypoint.sh").write_text("#!/bin/bash\n")
        (docker_dir / "admin").mkdir()
        (docker_dir / "admin" / "Dockerfile.admin.debian").write_text("FROM {{ admin_base_image }}\n")
        (docker_dir / "admin" / "entrypoint.sh").write_text("#!/bin/sh\n")
        (docker_dir / "coredns").mkdir()
        (docker_dir / "coredns" / "Dockerfile.coredns").write_text("ARG CORE_BASE\nFROM ${CORE_BASE}\n")

        config_dir = tooling / ".config"
        for d in ["coredns", "dnsdist", "proxy", "admin", "core"]:
            (config_dir / d).mkdir(parents=True, exist_ok=True)
        (config_dir / "coredns" / "Corefile").write_text("# {{ instance_name }}\n")
        (config_dir / "dnsdist" / "dnsdist.conf").write_text(
            'setLocal("0.0.0.0:53")\nnewServer({address="{{ coredns_dns_ip }}:53"})\n'
        )
        (config_dir / "proxy" / "squid.conf").write_text("# {{ proxy_core_ip }}\n")
        (config_dir / "proxy" / "ERR_SANDBOX_403").write_text("DENIED\n")
        for f in [".zshrc", ".tmux.conf", "gitmux.conf", "starship.toml", ".gitconfig"]:
            (config_dir / "admin" / f).write_text(f"# {f}\n")
        for f in [".bashrc", ".npmrc", ".gitconfig", "CLAUDE.md", "sshd_config"]:
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

        render_templates(ctx, str(instance), db_postgres=False, mcp_firecrawl=False)

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
        config = InstanceConfig.from_toml(str(toml_path))
        assert config.proxy_whitelist.read_only_domains == []


class TestImageDigestContextValues:
    """Task 7.6: image-related context values use digest format."""

    def test_context_dns_image_is_digest(self, tmp_path: Path) -> None:
        """dns_image uses @sha256: digest format."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = InstanceConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(config=config, base_index=0, proxy_password="x", instance_dir="/tmp/x")
        assert "@sha256:" in ctx["dns_image"]

    def test_context_proxy_image_is_digest(self, tmp_path: Path) -> None:
        """proxy_image uses @sha256: digest format."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = InstanceConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(config=config, base_index=0, proxy_password="x", instance_dir="/tmp/x")
        assert "@sha256:" in ctx["proxy_image"]

    def test_context_db_postgres_image_present(self, tmp_path: Path) -> None:
        """db_postgres_image is present in context."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = InstanceConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(config=config, base_index=0, proxy_password="x", instance_dir="/tmp/x")
        assert "db_postgres_image" in ctx
        assert "@sha256:" in ctx["db_postgres_image"]


class TestValidTomlBackwardCompatibility:
    """Task 7.7: VALID_TOML fixtures remain backward-compatible with mutable tags."""

    def test_mutable_tags_accepted(self, tmp_path: Path) -> None:
        """VALID_TOML with mutable tags parses without errors."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = InstanceConfig.from_toml(str(toml_path))
        # User-supplied mutable tags are accepted — defaults would be digests
        assert config.core.base_image == "cgr.dev/chainguard/wolfi-base:latest"
        assert config.admin.base_image == "debian:trixie-slim"


class TestImageDigestsDnsdist:
    """Wave 3: IMAGE_REGISTRY includes dnsdist entry."""

    def test_dnsdist_key_present(self) -> None:
        """IMAGE_REGISTRY contains 'dnsdist' key."""
        assert "dnsdist" in IMAGE_REGISTRY

    def test_dnsdist_value_format(self) -> None:
        """dnsdist value uses digest format with powerdns/dnsdist-19."""
        pin = IMAGE_REGISTRY["dnsdist"]
        assert pin.ref == "powerdns/dnsdist-19"
        assert pin.digest.startswith("sha256:")
        assert len(pin.digest.split("sha256:")[1]) == 64

    def test_image_registry_has_7_entries(self) -> None:
        """IMAGE_REGISTRY has exactly 7 entries."""
        assert len(IMAGE_REGISTRY) == 7
        assert set(IMAGE_REGISTRY.keys()) == {
            "wolfi_base",
            "debian_trixie",
            "squid",
            "coredns",
            "dnsdist",
            "postgres",
            "busybox_musl",
        }


class TestSixSubnetContextKeys:
    """Wave 3: build_jinja_context returns 6 subnet + new IP keys."""

    def test_six_subnet_keys_present(self, tmp_path: Path) -> None:
        """Context contains all 6 subnet CIDR keys."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = InstanceConfig.from_toml(str(toml_path))
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
        config = InstanceConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(config=config, base_index=0, proxy_password="x", instance_dir="/tmp/x")
        assert "proxy_core_ip" in ctx
        assert "proxy_admin_ip" in ctx

    def test_dnsdist_ip_keys(self, tmp_path: Path) -> None:
        """Context includes all 3 dnsdist IP keys."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = InstanceConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(config=config, base_index=0, proxy_password="x", instance_dir="/tmp/x")
        for key in ["dnsdist_isolated_ip", "dnsdist_dns_ip", "dnsdist_admin_ip"]:
            assert key in ctx, f"Missing context key: {key}"

    def test_coredns_ip_keys(self, tmp_path: Path) -> None:
        """Context includes all 3 coredns IP keys."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = InstanceConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(config=config, base_index=0, proxy_password="x", instance_dir="/tmp/x")
        for key in ["coredns_dns_ip", "coredns_admin_ip", "coredns_egress_ip"]:
            assert key in ctx, f"Missing context key: {key}"

    def test_db_postgres_admin_ip_key(self, tmp_path: Path) -> None:
        """Context includes db_postgres_admin_ip."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = InstanceConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(config=config, base_index=0, proxy_password="x", instance_dir="/tmp/x")
        assert "db_postgres_admin_ip" in ctx

    def test_firecrawl_dns_ip_key(self, tmp_path: Path) -> None:
        """Context includes firecrawl_dns_ip."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = InstanceConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(config=config, base_index=0, proxy_password="x", instance_dir="/tmp/x")
        assert "firecrawl_dns_ip" in ctx

    def test_dnsdist_image_key(self, tmp_path: Path) -> None:
        """Context includes dnsdist_image from IMAGE_REGISTRY."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = InstanceConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(config=config, base_index=0, proxy_password="x", instance_dir="/tmp/x")
        assert "dnsdist_image" in ctx
        assert ctx["dnsdist_image"] == IMAGE_REGISTRY["dnsdist"].pinned

    def test_legacy_keys_absent(self, tmp_path: Path) -> None:
        """Legacy 3-subnet keys not in context."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = InstanceConfig.from_toml(str(toml_path))
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
        content = (
            Path(__file__).parent.parent.parent / "src" / "templates" / "config" / "dnsdist" / "dnsdist.conf"
        ).read_text()
        assert "QNameWireLengthRule(0, 65)" in content
        assert "DropAction()" in content

    def test_dnsdist_label_count_rule(self) -> None:
        """Template contains QNameLabelsCountRule filter."""
        content = (
            Path(__file__).parent.parent.parent / "src" / "templates" / "config" / "dnsdist" / "dnsdist.conf"
        ).read_text()
        assert "QNameLabelsCountRule(0, 7)" in content

    def test_dnsdist_set_local(self) -> None:
        """Template binds to 0.0.0.0:53."""
        content = (
            Path(__file__).parent.parent.parent / "src" / "templates" / "config" / "dnsdist" / "dnsdist.conf"
        ).read_text()
        assert 'setLocal("0.0.0.0:53")' in content

    def test_dnsdist_control_socket(self) -> None:
        """Template has localhost-only control socket."""
        content = (
            Path(__file__).parent.parent.parent / "src" / "templates" / "config" / "dnsdist" / "dnsdist.conf"
        ).read_text()
        assert 'controlSocket("127.0.0.1:5199")' in content

    def test_dnsdist_backend_coredns(self) -> None:
        """Template forwards to coredns via Jinja2 variable."""
        content = (
            Path(__file__).parent.parent.parent / "src" / "templates" / "config" / "dnsdist" / "dnsdist.conf"
        ).read_text()
        assert "{{ coredns_dns_ip }}" in content
        assert "newServer" in content


class TestSquidFirecrawlAcl:
    """Wave 3: proxy/squid.conf contains firecrawl ACL and safe_methods."""

    def test_firecrawl_src_acl(self) -> None:
        """squid.conf contains firecrawl_src source ACL."""
        content = (
            Path(__file__).parent.parent.parent / "src" / "templates" / "config" / "proxy" / "squid.conf"
        ).read_text()
        assert "acl firecrawl_src src" in content
        assert "{{ mcp_firecrawl_proxy_ip }}" in content

    def test_safe_methods_acl(self) -> None:
        """squid.conf contains safe_methods ACL."""
        content = (
            Path(__file__).parent.parent.parent / "src" / "templates" / "config" / "proxy" / "squid.conf"
        ).read_text()
        assert "acl safe_methods method GET HEAD OPTIONS" in content

    def test_firecrawl_allow_rule(self) -> None:
        """squid.conf contains firecrawl allow rule with safe_methods."""
        content = (
            Path(__file__).parent.parent.parent / "src" / "templates" / "config" / "proxy" / "squid.conf"
        ).read_text()
        assert "http_access allow firecrawl_src authenticated_users safe_methods whitelist" in content

    def test_firecrawl_after_agent_admin(self) -> None:
        """Firecrawl allow rule appears after agent/admin allows and before deny all."""
        content = (
            Path(__file__).parent.parent.parent / "src" / "templates" / "config" / "proxy" / "squid.conf"
        ).read_text()
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
        Path(__file__).parent.parent.parent / "src" / "templates" / "docker" / "compose.yml"
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
        Path(__file__).parent.parent.parent / "src" / "templates" / "docker" / "extras" / filename
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
        raw = (Path(__file__).parent.parent.parent / "src" / "templates" / "docker" / "compose.yml").read_text()
        assert "x-security-baseline:" in raw
        assert "&security-baseline" in raw

    def test_baseline_contains_scalar_properties(
        self,
        tmp_path: Path,
    ) -> None:
        """Baseline has security_opt, cap_drop, ipc, init, read_only."""
        raw = (Path(__file__).parent.parent.parent / "src" / "templates" / "docker" / "compose.yml").read_text()
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
        self,
        tmp_path: Path,
    ) -> None:
        """Baseline does NOT contain cap_add, sysctls, or tmpfs."""
        raw = (Path(__file__).parent.parent.parent / "src" / "templates" / "docker" / "compose.yml").read_text()
        start = raw.index("x-security-baseline:")
        end = raw.index("\nnetworks:")
        block = raw[start:end]
        assert "cap_add:" not in block
        assert "sysctls:" not in block
        assert "tmpfs:" not in block

    def test_core_inherits_read_only(self, tmp_path: Path) -> None:
        """Core service inherits baseline read_only: true (no override)."""
        rendered = _render_compose(tmp_path)
        # Extract core service block
        core_start = rendered.index("\n  core:")
        admin_start = rendered.index("\n  admin:")
        core_block = rendered[core_start:admin_start]
        assert "read_only: false" not in core_block

    def test_admin_inherits_read_only(self, tmp_path: Path) -> None:
        """Admin service inherits baseline read_only: true (no override)."""
        rendered = _render_compose(tmp_path)
        admin_start = rendered.index("\n  admin:")
        admin_block = rendered[admin_start:]
        assert "read_only: false" not in admin_block


class TestComposeNetworkDefinitions:
    """5.T: 6-network topology with correct properties."""

    def test_new_networks_defined(self, tmp_path: Path) -> None:
        """compose.yml defines core_proxy_net, dns_net, admin_net,
        admin_proxy_net."""
        rendered = _render_compose(tmp_path)
        for net in [
            "core_proxy_net:",
            "dns_net:",
            "admin_net:",
            "admin_proxy_net:",
        ]:
            assert net in rendered, f"Missing network: {net}"

    def test_new_networks_internal(self, tmp_path: Path) -> None:
        """All 4 new networks have internal: true."""
        raw = (Path(__file__).parent.parent.parent / "src" / "templates" / "docker" / "compose.yml").read_text()
        for net_name in [
            "core_proxy_net:",
            "dns_net:",
            "admin_net:",
            "admin_proxy_net:",
        ]:
            idx = raw.index(net_name)
            # Check within the next 200 chars
            block = raw[idx : idx + 200]
            assert "internal: true" in block, f"{net_name} missing internal: true"

    def test_proxy_net_removed(self, tmp_path: Path) -> None:
        """Legacy proxy_net network is no longer defined."""
        raw = (Path(__file__).parent.parent.parent / "src" / "templates" / "docker" / "compose.yml").read_text()
        # Extract networks block only
        net_start = raw.index("networks:")
        svc_start = raw.index("services:")
        net_block = raw[net_start:svc_start]
        # proxy_net should not appear as a top-level network
        # (core_proxy_net and admin_proxy_net contain "proxy_net"
        # as substring, so check for exact key)
        lines = net_block.split("\n")
        top_keys = [
            ln.strip().rstrip(":")
            for ln in lines
            if ln and not ln.startswith(" ") and ln.strip().endswith(":") and ln.strip() != "networks:"
        ]
        assert "proxy_net" not in top_keys


class TestComposeServiceNetworkMembership:
    """5.T: Zero-shared-network invariant, per-service membership."""

    def test_core_on_isolated_core_proxy_and_ipc(
        self,
        tmp_path: Path,
    ) -> None:
        """Core is on isolated_net, core_proxy_net, and ipc_net."""
        rendered = _render_compose(tmp_path)
        core_start = rendered.index("\n  core:")
        admin_start = rendered.index("\n  admin:")
        core_block = rendered[core_start:admin_start]
        assert "isolated_net:" in core_block
        assert "core_proxy_net:" in core_block
        assert "ipc_net:" in core_block
        assert "admin_net:" not in core_block
        assert "admin_proxy_net:" not in core_block

    def test_admin_on_admin_admin_proxy_and_ipc(
        self,
        tmp_path: Path,
    ) -> None:
        """Admin is on admin_net, admin_proxy_net, and ipc_net."""
        rendered = _render_compose(tmp_path)
        admin_start = rendered.index("\n  admin:")
        admin_block = rendered[admin_start:]
        assert "admin_net:" in admin_block
        assert "admin_proxy_net:" in admin_block
        assert "ipc_net:" in admin_block
        assert "isolated_net:" not in admin_block
        assert "core_proxy_net:" not in admin_block

    def test_ipc_net_is_only_shared_network(self, tmp_path: Path) -> None:
        """Core and admin network sets intersect only on ipc_net."""
        rendered = _render_compose(tmp_path)
        core_start = rendered.index("\n  core:")
        admin_start = rendered.index("\n  admin:")
        core_block = rendered[core_start:admin_start]
        admin_block = rendered[admin_start:]
        all_nets = [
            "isolated_net",
            "core_proxy_net",
            "dns_net",
            "admin_net",
            "admin_proxy_net",
            "egress_net",
            "ipc_net",
        ]
        core_nets = {n for n in all_nets if f"{n}:" in core_block}
        admin_nets = {n for n in all_nets if f"{n}:" in admin_block}
        assert core_nets & admin_nets == {"ipc_net"}, f"Shared networks: {core_nets & admin_nets}"

    def test_coredns_exists_dns_sidecar_absent(
        self,
        tmp_path: Path,
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
        self,
        tmp_path: Path,
    ) -> None:
        """Firecrawl dns uses dnsdist_dns_ip."""
        from core.ipam import derive_static_ips

        ips = derive_static_ips(0)
        rendered = _render_extras(tmp_path, "mcp-firecrawl.yml")
        assert ips["dnsdist_dns_ip"] in rendered

    def test_firecrawl_no_dns_sidecar_refs(
        self,
        tmp_path: Path,
    ) -> None:
        """Firecrawl has no dns-sidecar references."""
        rendered = _render_extras(tmp_path, "mcp-firecrawl.yml")
        assert "dns-sidecar" not in rendered

    def test_firecrawl_depends_on_dnsdist(
        self,
        tmp_path: Path,
    ) -> None:
        """Firecrawl depends_on dnsdist."""
        rendered = _render_extras(tmp_path, "mcp-firecrawl.yml")
        assert "dnsdist:" in rendered
        assert "service_healthy" in rendered


class TestHydrationIpcContext:
    """3.T RED: IPC context keys, sshd_config registry, programmatic .claude.json."""

    def test_context_includes_ipc_subnet(self, tmp_path: Path) -> None:
        """build_jinja_context returns ipc_subnet key."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = InstanceConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(
            config=config,
            base_index=0,
            proxy_password="x",
            instance_dir=str(tmp_path),
        )
        assert "ipc_subnet" in ctx
        assert ctx["ipc_subnet"] == "10.100.6.0/24"

    def test_context_includes_ipc_ips(self, tmp_path: Path) -> None:
        """build_jinja_context contains core_ipc_ip and admin_ipc_ip."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = InstanceConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(
            config=config,
            base_index=0,
            proxy_password="x",
            instance_dir=str(tmp_path),
        )
        assert "core_ipc_ip" in ctx
        assert "admin_ipc_ip" in ctx

    def test_context_includes_firecrawl_isolated_ip(self, tmp_path: Path) -> None:
        """build_jinja_context contains firecrawl_isolated_ip."""
        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = InstanceConfig.from_toml(str(toml_path))
        ctx = build_jinja_context(
            config=config,
            base_index=0,
            proxy_password="x",
            instance_dir=str(tmp_path),
        )
        assert "firecrawl_isolated_ip" in ctx

    def test_sshd_config_in_jinja_rendered_config(self) -> None:
        """_JINJA_RENDERED_CONFIG includes sshd_config entry."""
        from core.hydration import _JINJA_RENDERED_CONFIG

        sources = [src for src, _ in _JINJA_RENDERED_CONFIG]
        assert "core/sshd_config" in sources

    def test_claude_json_not_in_static_config_core(self) -> None:
        """.claude.json must not be in _STATIC_CONFIG_CORE."""
        from core.hydration import _STATIC_CONFIG_CORE

        assert ".claude.json" not in _STATIC_CONFIG_CORE

    def test_static_config_core_is_empty(self) -> None:
        """_STATIC_CONFIG_CORE must be empty list."""
        from core.hydration import _STATIC_CONFIG_CORE

        assert _STATIC_CONFIG_CORE == []

    def test_render_templates_generates_claude_json_with_firecrawl(
        self,
        tmp_path: Path,
    ) -> None:
        """With mcp_firecrawl=True, .claude.json has mcpServers.firecrawl."""
        import json

        instance = tmp_path / "instance"
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
        render_templates(ctx, str(instance), db_postgres=False, mcp_firecrawl=True)

        claude_json = instance / "config" / "core" / ".claude.json"
        assert claude_json.exists()
        data = json.loads(claude_json.read_text())
        assert "mcpServers" in data
        assert "firecrawl" in data["mcpServers"]
        firecrawl = data["mcpServers"]["firecrawl"]
        assert firecrawl["type"] == "http"
        assert ctx["firecrawl_isolated_ip"] in firecrawl["url"]

    def test_render_templates_generates_empty_claude_json_without_firecrawl(
        self,
        tmp_path: Path,
    ) -> None:
        """With mcp_firecrawl=False, .claude.json is '{}'."""
        import json

        instance = tmp_path / "instance"
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
        render_templates(ctx, str(instance), db_postgres=False, mcp_firecrawl=False)

        claude_json = instance / "config" / "core" / ".claude.json"
        assert claude_json.exists()
        data = json.loads(claude_json.read_text())
        assert data == {}


class TestSshdConfigTemplate:
    """4.T RED: sshd_config Jinja2 template rendering and directives."""

    def test_sshd_config_renders_with_ipc_ip(self, tmp_path: Path) -> None:
        """validate_templates passes with sshd_config in registry."""
        from core.hydration import validate_templates

        ctx = _build_test_context(str(tmp_path / "inst"))
        count, errors = validate_templates(
            ctx,
            db_postgres=False,
            mcp_firecrawl=False,
        )
        assert errors == [], f"Unexpected errors: {errors}"
        assert count > 0

    def test_sshd_config_listen_address(self, tmp_path: Path) -> None:
        """sshd_config contains all required directives per spec."""
        import jinja2

        tooling = _build_minimal_tooling(tmp_path)
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(tooling)),
            undefined=jinja2.StrictUndefined,
        )
        ctx = _build_test_context(str(tmp_path / "inst"))
        tpl = env.get_template("config/core/sshd_config")
        rendered = tpl.render(ctx)

        assert "ListenAddress 10.100.6.3" in rendered
        assert "Port 9999" in rendered
        assert "PasswordAuthentication no" in rendered
        assert "AllowUsers agent" in rendered
        assert "PermitRootLogin no" in rendered
        assert "HostKey /run/secrets/ipc_host_key" in rendered
        assert "AuthorizedKeysFile /run/secrets/authorized_keys" in rendered
        assert "AcceptEnv SANDBOX_WARMUP_PROMPT" in rendered
        assert "0.0.0.0" not in rendered


class TestComposeIpcNetAndW4Hardening:
    """5.T RED: Compose template — ipc_net, volume removal, security baseline,
    SSH credential mounts, tmpfs mounts, and per-service cap_add."""

    # --- (a) core on ipc_net ---
    def test_compose_core_on_ipc_net(self, tmp_path: Path) -> None:
        """Core service networks include ipc_net."""
        rendered = _render_compose(tmp_path)
        core_start = rendered.index("\n  core:")
        admin_start = rendered.index("\n  admin:")
        core_block = rendered[core_start:admin_start]
        assert "ipc_net:" in core_block

    # --- (b) admin on ipc_net ---
    def test_compose_admin_on_ipc_net(self, tmp_path: Path) -> None:
        """Admin service networks include ipc_net."""
        rendered = _render_compose(tmp_path)
        admin_start = rendered.index("\n  admin:")
        admin_block = rendered[admin_start:]
        assert "ipc_net:" in admin_block

    # --- (c) ipc_net network definition ---
    def test_compose_ipc_net_definition(self, tmp_path: Path) -> None:
        """ipc_net network block contains internal: true and enable_ipv6: false."""
        raw = (Path(__file__).parent.parent.parent / "src" / "templates" / "docker" / "compose.yml").read_text()
        assert "ipc_net:" in raw
        idx = raw.index("ipc_net:")
        block = raw[idx : idx + 300]
        assert "internal: true" in block
        assert "enable_ipv6: false" in block

    # --- (d) no admin-ipc_vol ---
    def test_compose_no_admin_ipc_vol(self, tmp_path: Path) -> None:
        """Rendered compose does NOT contain admin-ipc_vol."""
        rendered = _render_compose(tmp_path)
        assert "admin-ipc_vol" not in rendered

    # --- (e) no mcp-ipc_vol ---
    def test_compose_no_mcp_ipc_vol(self, tmp_path: Path) -> None:
        """Rendered compose does NOT contain mcp-ipc_vol."""
        rendered = _render_compose(tmp_path)
        assert "mcp-ipc_vol" not in rendered

    # --- (f) no /sock mount ---
    def test_compose_no_sock_mount(self, tmp_path: Path) -> None:
        """Rendered compose does NOT contain /sock."""
        rendered = _render_compose(tmp_path)
        assert "/sock" not in rendered

    # --- (g) core no read_only: false ---
    def test_compose_core_no_read_only_false(self, tmp_path: Path) -> None:
        """Core service does NOT contain read_only: false."""
        rendered = _render_compose(tmp_path)
        core_start = rendered.index("\n  core:")
        admin_start = rendered.index("\n  admin:")
        core_block = rendered[core_start:admin_start]
        assert "read_only: false" not in core_block

    # --- (h) admin no read_only: false ---
    def test_compose_admin_no_read_only_false(self, tmp_path: Path) -> None:
        """Admin service does NOT contain read_only: false."""
        rendered = _render_compose(tmp_path)
        admin_start = rendered.index("\n  admin:")
        admin_block = rendered[admin_start:]
        assert "read_only: false" not in admin_block

    # --- (i) core cap_add CHOWN ---
    def test_compose_core_cap_add_setuid(self, tmp_path: Path) -> None:
        """Core service contains cap_add with CHOWN (non-root sshd-session PTY allocation)."""
        rendered = _render_compose(tmp_path)
        core_start = rendered.index("\n  core:")
        admin_start = rendered.index("\n  admin:")
        core_block = rendered[core_start:admin_start]
        assert "cap_add:" in core_block
        assert "CHOWN" in core_block

    # --- (j) core ipc_host_key bind-mount ---
    def test_compose_core_ipc_host_key_secret(self, tmp_path: Path) -> None:
        """Core service volumes contain ipc_host_key as bind-mount (not Docker secrets)."""
        rendered = _render_compose(tmp_path)
        core_start = rendered.index("\n  core:")
        admin_start = rendered.index("\n  admin:")
        core_block = rendered[core_start:admin_start]
        assert "ipc_host_key:/run/secrets/ipc_host_key:ro" in core_block

    # --- (k) admin ipc_ssh_key bind-mount ---
    def test_compose_admin_ipc_ssh_key_secret(self, tmp_path: Path) -> None:
        """Admin service volumes contain ipc_ssh_key as bind-mount (not Docker secrets)."""
        rendered = _render_compose(tmp_path)
        admin_start = rendered.index("\n  admin:")
        admin_block = rendered[admin_start:]
        assert "ipc_ssh_key:/run/secrets/ipc_ssh_key:ro" in admin_block

    # --- (l) core tmpfs /run ---
    def test_compose_core_tmpfs_run(self, tmp_path: Path) -> None:
        """Core tmpfs includes /run."""
        rendered = _render_compose(tmp_path)
        core_start = rendered.index("\n  core:")
        admin_start = rendered.index("\n  admin:")
        core_block = rendered[core_start:admin_start]
        # Match /run tmpfs entry — must not match /var/run subpath
        assert "/run:" in core_block or "/run\n" in core_block

    # --- (m) core tmpfs ~/.config ---
    def test_compose_core_tmpfs_config(self, tmp_path: Path) -> None:
        """Core tmpfs includes /home/agent/.config."""
        rendered = _render_compose(tmp_path)
        core_start = rendered.index("\n  core:")
        admin_start = rendered.index("\n  admin:")
        core_block = rendered[core_start:admin_start]
        assert "/home/agent/.config" in core_block

    # --- (n) admin tmpfs ~/.cache ---
    def test_compose_admin_tmpfs_cache(self, tmp_path: Path) -> None:
        """Admin tmpfs includes /home/human/.cache."""
        rendered = _render_compose(tmp_path)
        admin_start = rendered.index("\n  admin:")
        admin_block = rendered[admin_start:]
        assert "/home/human/.cache" in admin_block

    # --- (o) admin tmpfs ~/.config ---
    def test_compose_admin_tmpfs_config(self, tmp_path: Path) -> None:
        """Admin tmpfs includes /home/human/.config."""
        rendered = _render_compose(tmp_path)
        admin_start = rendered.index("\n  admin:")
        admin_block = rendered[admin_start:]
        assert "/home/human/.config" in admin_block

    # --- (p) admin tmpfs ~/.zsh_sessions ---
    def test_compose_admin_tmpfs_zsh_sessions(self, tmp_path: Path) -> None:
        """Admin tmpfs includes /home/human/.zsh_sessions."""
        rendered = _render_compose(tmp_path)
        admin_start = rendered.index("\n  admin:")
        admin_block = rendered[admin_start:]
        assert "/home/human/.zsh_sessions" in admin_block

    # --- (q) core no command override ---
    def test_compose_core_no_command_override(self, tmp_path: Path) -> None:
        """Core service does NOT contain a command: directive."""
        rendered = _render_compose(tmp_path)
        core_start = rendered.index("\n  core:")
        admin_start = rendered.index("\n  admin:")
        core_block = rendered[core_start:admin_start]
        assert "command:" not in core_block

    # --- (r) core NO_PROXY includes ipc_subnet ---
    def test_compose_core_no_proxy_includes_ipc(self, tmp_path: Path) -> None:
        """Core service NO_PROXY includes ipc_subnet."""
        from core.ipam import derive_subnets

        subnets = derive_subnets(0)
        ipc_subnet = subnets[6]
        rendered = _render_compose(tmp_path)
        core_start = rendered.index("\n  core:")
        admin_start = rendered.index("\n  admin:")
        core_block = rendered[core_start:admin_start]
        assert ipc_subnet in core_block

    # --- (s) admin NO_PROXY includes ipc_subnet ---
    def test_compose_admin_no_proxy_includes_ipc(self, tmp_path: Path) -> None:
        """Admin service NO_PROXY includes ipc_subnet."""
        from core.ipam import derive_subnets

        subnets = derive_subnets(0)
        ipc_subnet = subnets[6]
        rendered = _render_compose(tmp_path)
        admin_start = rendered.index("\n  admin:")
        admin_block = rendered[admin_start:]
        assert ipc_subnet in admin_block

    # --- (t) core authorized_keys bind mount ---
    def test_compose_core_authorized_keys_bind(self, tmp_path: Path) -> None:
        """Core volumes contain authorized_keys:/run/secrets/authorized_keys:ro."""
        rendered = _render_compose(tmp_path)
        core_start = rendered.index("\n  core:")
        admin_start = rendered.index("\n  admin:")
        core_block = rendered[core_start:admin_start]
        assert "authorized_keys:/run/secrets/authorized_keys:ro" in core_block

    # --- (u) admin known_hosts bind mount ---
    def test_compose_admin_known_hosts_bind(self, tmp_path: Path) -> None:
        """Admin volumes contain ipc_known_hosts:/run/secrets/ipc_known_hosts:ro."""
        rendered = _render_compose(tmp_path)
        admin_start = rendered.index("\n  admin:")
        admin_block = rendered[admin_start:]
        assert "ipc_known_hosts:/run/secrets/ipc_known_hosts:ro" in admin_block

    # --- (v) admin starship bind mount ---
    def test_compose_admin_starship_bind(self, tmp_path: Path) -> None:
        """Admin volumes contain starship.toml:/home/human/.config/starship.toml:ro."""
        rendered = _render_compose(tmp_path)
        admin_start = rendered.index("\n  admin:")
        admin_block = rendered[admin_start:]
        assert "starship.toml:/home/human/.config/starship.toml:ro" in admin_block

    # --- (w) infra services no read_only: false ---
    def test_compose_infra_no_read_only_false(self, tmp_path: Path) -> None:
        """coredns, proxy, and dnsdist services do NOT contain read_only: false."""
        rendered = _render_compose(tmp_path)
        # coredns block
        coredns_start = rendered.index("\n  coredns:")
        dnsdist_start = rendered.index("\n  dnsdist:")
        coredns_block = rendered[coredns_start:dnsdist_start]
        assert "read_only: false" not in coredns_block
        # dnsdist block
        proxy_start = rendered.index("\n  proxy:")
        dnsdist_block = rendered[dnsdist_start:proxy_start]
        assert "read_only: false" not in dnsdist_block
        # proxy block
        core_start = rendered.index("\n  core:")
        proxy_block = rendered[proxy_start:core_start]
        assert "read_only: false" not in proxy_block

    # --- (x) baseline excludes list-valued properties ---
    def test_compose_baseline_excludes_list_valued(self, tmp_path: Path) -> None:
        """x-security-baseline block does NOT contain cap_add, sysctls, or tmpfs."""
        raw = (Path(__file__).parent.parent.parent / "src" / "templates" / "docker" / "compose.yml").read_text()
        start = raw.index("x-security-baseline:")
        end = raw.index("\nnetworks:")
        block = raw[start:end]
        assert "cap_add:" not in block
        assert "sysctls:" not in block
        assert "tmpfs:" not in block

    # --- (y) coredns cap_add ---
    def test_compose_coredns_cap_add(self, tmp_path: Path) -> None:
        """coredns service has cap_add: [NET_BIND_SERVICE]."""
        rendered = _render_compose(tmp_path)
        coredns_start = rendered.index("\n  coredns:")
        dnsdist_start = rendered.index("\n  dnsdist:")
        coredns_block = rendered[coredns_start:dnsdist_start]
        assert "cap_add:" in coredns_block
        assert "NET_BIND_SERVICE" in coredns_block

    # --- (z) proxy cap_add ---
    def test_compose_proxy_cap_add(self, tmp_path: Path) -> None:
        """proxy service has cap_add: [SETUID, SETGID]."""
        rendered = _render_compose(tmp_path)
        proxy_start = rendered.index("\n  proxy:")
        core_start = rendered.index("\n  core:")
        proxy_block = rendered[proxy_start:core_start]
        assert "cap_add:" in proxy_block
        assert "SETUID" in proxy_block
        assert "SETGID" in proxy_block


class TestFirecrawlMcpHttpTransport:
    """6.T RED: Firecrawl MCP transport migration to mcp-proxy Streamable HTTP."""

    # --- (a) entrypoint is mcp-proxy ---
    def test_mcp_firecrawl_entrypoint_is_mcp_proxy(self, tmp_path: Path) -> None:
        """mcp-firecrawl.yml entrypoint contains mcp-proxy --server stream --port 3000."""
        rendered = _render_extras(tmp_path, "mcp-firecrawl.yml")
        assert "mcp-proxy" in rendered
        assert "--server" in rendered
        assert "stream" in rendered
        assert "--port" in rendered
        assert "3000" in rendered

    # --- (b) firecrawl on isolated_net ---
    def test_mcp_firecrawl_on_isolated_net(self, tmp_path: Path) -> None:
        """Firecrawl service networks include isolated_net."""
        rendered = _render_extras(tmp_path, "mcp-firecrawl.yml")
        assert "isolated_net:" in rendered

    # --- (c) no mcp-ipc_vol ---
    def test_mcp_firecrawl_no_mcp_ipc_vol(self, tmp_path: Path) -> None:
        """Rendered mcp-firecrawl.yml does NOT contain mcp-ipc_vol."""
        rendered = _render_extras(tmp_path, "mcp-firecrawl.yml")
        assert "mcp-ipc_vol" not in rendered

    # --- (d) no /var/run/mcp ---
    def test_mcp_firecrawl_no_var_run_mcp(self, tmp_path: Path) -> None:
        """Rendered mcp-firecrawl.yml does NOT contain /var/run/mcp."""
        rendered = _render_extras(tmp_path, "mcp-firecrawl.yml")
        assert "/var/run/mcp" not in rendered


# ── Section W4: Integration Verification ────────────────────────────────────


class TestW4IntegrationVerification:
    """13.T: End-to-end integration verification for Wave 4 containment hardening."""

    def test_full_w4_template_validation(self, tmp_path: Path) -> None:
        """Build a complete Jinja2 context with all 7-tuple fields, validate all templates."""
        from core.hydration import InstanceConfig, build_jinja_context, validate_templates

        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = InstanceConfig.from_toml(str(toml_path))

        # Use base_index=0 for deterministic IPs
        context = build_jinja_context(config, base_index=0, proxy_password="testpass", instance_dir=str(tmp_path))
        context["in_container_workspace_bridge_gid"] = 1000

        # Verify 7-tuple fields exist in context
        assert "ipc_subnet" in context
        assert "core_ipc_ip" in context
        assert "admin_ipc_ip" in context
        assert "firecrawl_isolated_ip" in context

        validated, errors = validate_templates(context, db_postgres=True, mcp_firecrawl=True)
        assert errors == [], f"Template validation errors: {errors}"
        assert validated > 0

    def test_e2e_ipam_to_hydration_pipeline(self, tmp_path: Path) -> None:
        """Full pipeline: IPAM allocate → derive subnets/IPs → build context → validate templates."""
        from core.hydration import InstanceConfig, build_jinja_context, validate_templates
        from core.ipam import derive_static_ips, derive_subnets

        toml_path = tmp_path / "sandbox.toml"
        toml_path.write_text(VALID_TOML)
        config = InstanceConfig.from_toml(str(toml_path))

        # Simulate IPAM allocation at slot 0
        base_index = 0
        subnets = derive_subnets(base_index)
        assert len(subnets) == 7  # 7-tuple

        ips = derive_static_ips(base_index)
        assert "core_ipc_ip" in ips
        assert "admin_ipc_ip" in ips

        context = build_jinja_context(config, base_index, proxy_password="e2epass", instance_dir=str(tmp_path))
        context["in_container_workspace_bridge_gid"] = 1000

        validated, errors = validate_templates(context, db_postgres=True, mcp_firecrawl=True)
        assert errors == [], f"E2E pipeline errors: {errors}"
        assert validated > 0

    def test_scaffold_creates_secrets_dir(self, tmp_path: Path) -> None:
        """create_instance_dirs creates the secrets/ subdirectory."""
        from core.scaffold import create_instance_dirs

        instance_dir = tmp_path / "instance"
        create_instance_dirs(str(instance_dir))
        secrets_dir = instance_dir / "secrets"
        assert secrets_dir.is_dir()

    def test_acl_grant_plan_includes_secrets(self, tmp_path: Path) -> None:
        """_acl_grant_plan includes at least one entry targeting secrets/ directory."""
        from cli.main import _acl_grant_plan

        plan = _acl_grant_plan(str(tmp_path), "sandbox")
        secrets_entries = [desc for _, desc in plan if "secrets" in desc]
        assert len(secrets_entries) >= 1


class TestImagePin:
    """Group 1.T: ImagePin dataclass and IMAGE_REGISTRY validation."""

    def test_registry_is_dict_with_7_keys(self) -> None:
        """IMAGE_REGISTRY is a dict with exactly 7 keys."""
        assert isinstance(IMAGE_REGISTRY, dict)
        assert len(IMAGE_REGISTRY) == 7
        assert set(IMAGE_REGISTRY.keys()) == {
            "wolfi_base",
            "debian_trixie",
            "squid",
            "coredns",
            "dnsdist",
            "postgres",
            "busybox_musl",
        }

    def test_each_value_is_imagepin_with_str_fields(self) -> None:
        """Each value is an ImagePin instance with ref, tag, digest str fields."""
        for key, pin in IMAGE_REGISTRY.items():
            assert isinstance(pin, ImagePin), f"{key} is not an ImagePin"
            assert isinstance(pin.ref, str), f"{key}.ref is not str"
            assert isinstance(pin.tag, str), f"{key}.tag is not str"
            assert isinstance(pin.digest, str), f"{key}.digest is not str"

    def test_pinned_property_returns_digest_qualified_ref(self) -> None:
        """ImagePin.pinned returns f'{ref}@{digest}'."""
        pin = IMAGE_REGISTRY["coredns"]
        assert pin.pinned == f"{pin.ref}@{pin.digest}"

    def test_tagged_property_returns_tag_qualified_ref(self) -> None:
        """ImagePin.tagged returns f'{ref}:{tag}'."""
        pin = IMAGE_REGISTRY["coredns"]
        assert pin.tagged == f"{pin.ref}:{pin.tag}"

    def test_imagepin_is_frozen(self) -> None:
        """Assigning to an ImagePin field raises FrozenInstanceError."""
        from dataclasses import FrozenInstanceError

        pin = IMAGE_REGISTRY["coredns"]
        with pytest.raises(FrozenInstanceError):
            pin.digest = "x"  # type: ignore[misc]

    def test_busybox_musl_entry(self) -> None:
        """busybox_musl has tag='1.36.1-musl' and ref='busybox'."""
        pin = IMAGE_REGISTRY["busybox_musl"]
        assert pin.tag == "1.36.1-musl"
        assert pin.ref == "busybox"

    def test_all_digests_match_sha256_regex(self) -> None:
        """All digest values match ^sha256:[a-f0-9]{64}$."""
        import re

        pattern = re.compile(r"^sha256:[a-f0-9]{64}$")
        for key, pin in IMAGE_REGISTRY.items():
            assert pattern.match(pin.digest), f"{key}.digest does not match sha256 pattern: {pin.digest}"

    def test_legacy_image_digests_name_removed(self) -> None:
        """hydration.py source does NOT contain 'IMAGE_DIGESTS ='."""
        from pathlib import Path

        source = Path(__file__).resolve().parents[2] / "src" / "core" / "hydration.py"
        content = source.read_text()
        assert "IMAGE_DIGESTS =" not in content


class TestDownstreamConsumerMigration:
    """Group 2.T: Verify all downstream consumers use IMAGE_REGISTRY.pinned."""

    def test_context_proxy_image(self, tmp_path: Path) -> None:
        """ctx['proxy_image'] == IMAGE_REGISTRY['squid'].pinned."""
        ctx = _build_default_context(tmp_path)
        assert ctx["proxy_image"] == IMAGE_REGISTRY["squid"].pinned

    def test_context_dns_image(self, tmp_path: Path) -> None:
        """ctx['dns_image'] == IMAGE_REGISTRY['coredns'].pinned."""
        ctx = _build_default_context(tmp_path)
        assert ctx["dns_image"] == IMAGE_REGISTRY["coredns"].pinned

    def test_context_dnsdist_image(self, tmp_path: Path) -> None:
        """ctx['dnsdist_image'] == IMAGE_REGISTRY['dnsdist'].pinned."""
        ctx = _build_default_context(tmp_path)
        assert ctx["dnsdist_image"] == IMAGE_REGISTRY["dnsdist"].pinned

    def test_context_busybox_image(self, tmp_path: Path) -> None:
        """ctx['busybox_image'] == IMAGE_REGISTRY['busybox_musl'].pinned."""
        ctx = _build_default_context(tmp_path)
        assert ctx["busybox_image"] == IMAGE_REGISTRY["busybox_musl"].pinned

    def test_context_db_postgres_image(self, tmp_path: Path) -> None:
        """ctx['db_postgres_image'] == IMAGE_REGISTRY['postgres'].pinned (default)."""
        ctx = _build_default_context(tmp_path)
        assert ctx["db_postgres_image"] == IMAGE_REGISTRY["postgres"].pinned

    def test_core_config_default_uses_registry(self) -> None:
        """CoreConfig().base_image == IMAGE_REGISTRY['wolfi_base'].pinned."""
        assert CoreConfig().base_image == IMAGE_REGISTRY["wolfi_base"].pinned

    def test_admin_config_default_uses_registry(self) -> None:
        """AdminConfig().base_image == IMAGE_REGISTRY['debian_trixie'].pinned."""
        assert AdminConfig().base_image == IMAGE_REGISTRY["debian_trixie"].pinned

    def test_db_postgres_config_default_uses_registry(self) -> None:
        """DbPostgresConfig().image == IMAGE_REGISTRY['postgres'].pinned."""
        assert DbPostgresConfig().image == IMAGE_REGISTRY["postgres"].pinned

    def test_build_jinja_context_source_no_legacy(self) -> None:
        """build_jinja_context source does not contain IMAGE_DIGESTS[."""
        import inspect

        source = inspect.getsource(build_jinja_context)
        assert "IMAGE_DIGESTS[" not in source

    def test_pydantic_source_no_legacy(self) -> None:
        """CoreConfig, AdminConfig, DbPostgresConfig source does not contain IMAGE_DIGESTS[."""
        import inspect

        for cls in (CoreConfig, AdminConfig, DbPostgresConfig):
            source = inspect.getsource(cls)
            assert "IMAGE_DIGESTS[" not in source, f"{cls.__name__} still uses IMAGE_DIGESTS"

    def test_scaffold_imports_image_registry(self) -> None:
        """scaffold.py source contains IMAGE_REGISTRY import (not IMAGE_DIGESTS)."""
        from pathlib import Path

        source = Path(__file__).resolve().parents[2] / "src" / "core" / "scaffold.py"
        content = source.read_text()
        assert "IMAGE_REGISTRY" in content
        assert "IMAGE_DIGESTS" not in content


def _build_default_context(tmp_path: Path) -> dict[str, object]:
    """Build context via build_jinja_context with default InstanceConfig."""
    toml_path = tmp_path / "sandbox.toml"
    toml_path.write_text(VALID_TOML)
    config = InstanceConfig.from_toml(str(toml_path))
    return build_jinja_context(
        config=config,
        base_index=0,
        proxy_password="testpass",
        instance_dir=str(tmp_path / "instance"),
    )


def _get_dockerfile_lines(rel_path: str) -> list[str]:
    """Read a Dockerfile from the templates package and return its lines."""
    root = Path(__file__).resolve().parents[2] / "src" / "templates"
    return (root / rel_path).read_text().splitlines()


def _extract_stage(lines: list[str], stage_name: str) -> list[str]:
    """Extract lines belonging to a specific FROM ... AS <stage_name>."""
    result: list[str] = []
    in_stage = False
    for line in lines:
        stripped = line.strip()
        if stripped.upper().startswith("FROM ") and f"AS {stage_name}" in stripped:
            in_stage = True
            result.append(line)
            continue
        if in_stage:
            if stripped.upper().startswith("FROM "):
                break  # Next stage
            result.append(line)
    return result


class TestDockerfileUserContext:
    """Group 3.T: Validate USER context correctness in Dockerfiles."""

    def test_branch_typescript_user_root_before_staging_mkdir(self) -> None:
        """branch-typescript: USER root before mkdir /staging."""
        lines = _get_dockerfile_lines("docker/core/Dockerfile.core.wolfi")
        stage = _extract_stage(lines, "branch-typescript")
        stage_text = "\n".join(stage)
        # USER root must appear before the staging mkdir
        root_idx = next(
            (i for i, line in enumerate(stage) if line.strip() == "USER root"),
            None,
        )
        mkdir_idx = next(
            (i for i, line in enumerate(stage) if "mkdir" in line and "/staging" in line),
            None,
        )
        assert root_idx is not None, f"No 'USER root' in branch-typescript:\n{stage_text}"
        assert mkdir_idx is not None, f"No 'mkdir /staging' in branch-typescript:\n{stage_text}"
        assert root_idx < mkdir_idx, "USER root must appear before mkdir /staging in branch-typescript"

    def test_branch_typescript_user_unprivileged_before_npm(self) -> None:
        """branch-typescript: USER ${USERNAME} before npm install."""
        lines = _get_dockerfile_lines("docker/core/Dockerfile.core.wolfi")
        stage = _extract_stage(lines, "branch-typescript")
        user_switch_idx = next(
            (i for i, line in enumerate(stage) if "${USERNAME}" in line and line.strip().startswith("USER")),
            None,
        )
        npm_idx = next(
            (i for i, line in enumerate(stage) if "npm install" in line),
            None,
        )
        assert user_switch_idx is not None, "No USER ${USERNAME} in branch-typescript"
        assert npm_idx is not None, "No npm install in branch-typescript"
        assert user_switch_idx < npm_idx, "USER ${USERNAME} must appear before npm install in branch-typescript"

    def test_branch_python_user_root_before_staging_mkdir(self) -> None:
        """branch-python: USER root before staging mkdirs (both paths)."""
        lines = _get_dockerfile_lines("docker/core/Dockerfile.core.wolfi")
        stage = _extract_stage(lines, "branch-python")
        # All staging mkdirs must be under USER root
        user_context = "unknown"
        for line in stage:
            stripped = line.strip()
            if stripped.startswith("USER "):
                user_context = stripped.split()[1]
            if "mkdir" in stripped and "/staging" in stripped:
                assert user_context == "root", (
                    f"mkdir /staging under USER {user_context} (not root) in branch-python: {stripped}"
                )

    def test_branch_claude_user_root_before_staging_mkdir(self) -> None:
        """branch-claude: USER root before mkdir /staging."""
        lines = _get_dockerfile_lines("docker/core/Dockerfile.core.wolfi")
        stage = _extract_stage(lines, "branch-claude")
        root_idx = next(
            (i for i, line in enumerate(stage) if line.strip() == "USER root"),
            None,
        )
        mkdir_idx = next(
            (i for i, line in enumerate(stage) if "mkdir" in line and "/staging" in line),
            None,
        )
        assert root_idx is not None, "No USER root in branch-claude"
        assert mkdir_idx is not None, "No mkdir /staging in branch-claude"
        assert root_idx < mkdir_idx, "USER root must appear before mkdir /staging in branch-claude"

    def test_branch_claude_user_unprivileged_before_npm(self) -> None:
        """branch-claude: USER ${USERNAME} before npm install."""
        lines = _get_dockerfile_lines("docker/core/Dockerfile.core.wolfi")
        stage = _extract_stage(lines, "branch-claude")
        user_switch_idx = next(
            (i for i, line in enumerate(stage) if "${USERNAME}" in line and line.strip().startswith("USER")),
            None,
        )
        npm_idx = next(
            (i for i, line in enumerate(stage) if "npm install" in line),
            None,
        )
        assert user_switch_idx is not None, "No USER ${USERNAME} in branch-claude"
        assert npm_idx is not None, "No npm install in branch-claude"
        assert user_switch_idx < npm_idx, "USER ${USERNAME} must appear before npm install"

    def test_admin_user_root_before_entrypoint_copy(self) -> None:
        """Admin Dockerfile: USER root before COPY entrypoint.sh."""
        lines = _get_dockerfile_lines("docker/admin/Dockerfile.admin.debian")
        user_context = "unknown"
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("USER "):
                user_context = stripped.split()[1]
            if stripped.startswith("COPY") and "entrypoint.sh" in stripped:
                assert user_context == "root", f"COPY entrypoint.sh under USER {user_context} (not root)"

    def test_admin_user_unprivileged_before_entrypoint(self) -> None:
        """Admin Dockerfile: USER ${USERNAME} between chmod and ENTRYPOINT."""
        lines = _get_dockerfile_lines("docker/admin/Dockerfile.admin.debian")
        chmod_idx = next(
            (i for i, line in enumerate(lines) if "chmod" in line and "entrypoint" in line),
            None,
        )
        entrypoint_idx = next(
            (i for i, line in enumerate(lines) if line.strip().startswith("ENTRYPOINT")),
            None,
        )
        assert chmod_idx is not None, "No chmod entrypoint line"
        assert entrypoint_idx is not None, "No ENTRYPOINT line"
        # Find USER ${USERNAME} between chmod and ENTRYPOINT
        user_between = any(
            "${USERNAME}" in lines[i] and lines[i].strip().startswith("USER")
            for i in range(chmod_idx + 1, entrypoint_idx)
        )
        assert user_between, "USER ${USERNAME} must appear between chmod and ENTRYPOINT"

    def test_claude_local_bin_path(self) -> None:
        """Dockerfile.core.wolfi uses ${HOME_DIR}/.local/bin/claude (not .claude/local/claude)."""
        lines = _get_dockerfile_lines("docker/core/Dockerfile.core.wolfi")
        content = "\n".join(lines)
        assert "${HOME_DIR}/.local/bin/claude" in content or ".local/bin/claude" in content
        assert ".claude/local/claude" not in content


# ─── Dockerfile USER Structural Lint ─────────────────────────────────────────

_ROOT_OWNED_PREFIXES = ("/staging", "/usr", "/etc", "/var", "/run", "/opt")
_FS_OPS_PATTERN = re.compile(r"\b(mkdir|chmod|chown|touch|cp)\b")


def _lint_dockerfile_user_context(content: str) -> list[str]:
    """Lint a Dockerfile for filesystem operations under wrong USER context.

    State machine:
    - FROM resets USER to 'root' and clears chown grants
    - USER directives update tracked context
    - RUN lines with backslash continuations are joined into logical lines
    - chown under USER root records granted subtrees
    - Violations: mkdir/chmod/touch/cp targeting root-owned paths
      when USER is not 'root' AND the path is not covered by a prior chown grant
    """
    violations: list[str] = []
    current_user = "root"
    chown_grants: list[str] = []  # paths that have been chown'd to non-root
    raw_lines = content.splitlines()

    # Join backslash-continued lines
    logical_lines: list[tuple[int, str]] = []
    i = 0
    while i < len(raw_lines):
        line = raw_lines[i]
        start_lineno = i + 1  # 1-indexed
        while line.rstrip().endswith("\\") and i + 1 < len(raw_lines):
            line = line.rstrip()[:-1] + " " + raw_lines[i + 1].strip()
            i += 1
        logical_lines.append((start_lineno, line))
        i += 1

    _chown_path_re = re.compile(r"chown\s+(?:-R\s+)?[^\s]+\s+(.+)")

    for lineno, line in logical_lines:
        stripped = line.strip()

        # FROM resets USER to root and clears chown grants
        if stripped.upper().startswith("FROM "):
            current_user = "root"
            chown_grants.clear()
            continue

        # USER directive
        if stripped.startswith("USER "):
            current_user = stripped.split()[1]
            continue

        # Track chown grants under USER root
        if stripped.startswith("RUN ") and current_user == "root":
            m = _chown_path_re.search(stripped)
            if m:
                for path in m.group(1).split():
                    path = path.strip()
                    if path and path.startswith("/"):
                        chown_grants.append(path)
            continue

        # RUN directive — check for fs operations under non-root
        if stripped.startswith("RUN ") and current_user != "root" and _FS_OPS_PATTERN.search(stripped):
            for prefix in _ROOT_OWNED_PREFIXES:
                if prefix in stripped:
                    # Check if any chown grant covers this path
                    covered = any(prefix.startswith(grant) or grant.startswith(prefix) for grant in chown_grants)
                    if not covered:
                        violations.append(f"L{lineno}: {current_user} performing fs op on {prefix}: {stripped[:80]}")
                    break

    return violations


class TestDockerfileUserLint:
    """Group 4.T: Dockerfile USER context structural lint guard."""

    def test_core_wolfi_zero_violations(self) -> None:
        """Lint on current Dockerfile.core.wolfi returns zero violations."""
        content = "\n".join(_get_dockerfile_lines("docker/core/Dockerfile.core.wolfi"))
        violations = _lint_dockerfile_user_context(content)
        assert violations == [], f"Violations in Dockerfile.core.wolfi: {violations}"

    def test_admin_debian_zero_violations(self) -> None:
        """Lint on current Dockerfile.admin.debian returns zero violations."""
        content = "\n".join(_get_dockerfile_lines("docker/admin/Dockerfile.admin.debian"))
        violations = _lint_dockerfile_user_context(content)
        assert violations == [], f"Violations in Dockerfile.admin.debian: {violations}"

    def test_synthetic_violation_detected(self) -> None:
        """Synthetic Dockerfile with USER agent + mkdir /staging yields one violation."""
        synthetic = "FROM base\nUSER agent\nRUN mkdir -p /staging/foo\n"
        violations = _lint_dockerfile_user_context(synthetic)
        assert len(violations) == 1
        assert "agent" in violations[0]
        assert "/staging" in violations[0]
        assert "L3" in violations[0]

    def test_from_resets_user_to_root(self) -> None:
        """FROM ... AS stage resets tracked USER to root."""
        synthetic = "FROM base AS build\nUSER agent\nFROM base AS runtime\nRUN mkdir -p /staging/foo\n"
        violations = _lint_dockerfile_user_context(synthetic)
        assert violations == [], f"FROM should reset USER to root: {violations}"

    def test_backslash_continuation_joined(self) -> None:
        r"""RUN with \ continuations is joined into single logical line."""
        synthetic = "FROM base\nUSER agent\nRUN mkdir -p \\\n    /staging/foo\n"
        violations = _lint_dockerfile_user_context(synthetic)
        assert len(violations) == 1
        assert "/staging" in violations[0]


class TestHealthcheckFixes:
    """Group 5.T: CoreDNS Dockerfile and Compose healthcheck fixes."""

    def test_coredns_dockerfile_exists(self) -> None:
        """Dockerfile.coredns exists in templates/docker/coredns/."""
        root = Path(__file__).resolve().parents[2]
        df = root / "src" / "templates" / "docker" / "coredns" / "Dockerfile.coredns"
        assert df.exists(), "Dockerfile.coredns does not exist"

    def test_coredns_dockerfile_structure(self) -> None:
        """Dockerfile.coredns contains multi-stage build with probe stage."""
        root = Path(__file__).resolve().parents[2]
        content = (root / "src" / "templates" / "docker" / "coredns" / "Dockerfile.coredns").read_text()
        assert "FROM ${BUSYBOX_BASE} AS probe" in content
        assert "FROM ${CORE_BASE}" in content
        assert "COPY --from=probe /bin/wget /wget" in content
        assert "ARG CORE_BASE" in content
        assert "ARG BUSYBOX_BASE" in content
        # No Jinja markers
        assert "{{ }}" not in content
        assert "{{" not in content

    def test_compose_coredns_build_block(self) -> None:
        """compose.yml template has coredns with build: block (not image:)."""
        root = Path(__file__).resolve().parents[2]
        content = (root / "src" / "templates" / "docker" / "compose.yml").read_text()
        assert "coredns:" in content
        coredns_idx = content.index("  coredns:")
        # Section ends at next blank line (double newline)
        section_end = content.index("\n\n", coredns_idx)
        section = content[coredns_idx:section_end]
        assert "build:" in section, "coredns must use build: block"
        assert "Dockerfile.coredns" in section
        assert "CORE_BASE=" in section, "Missing CORE_BASE arg"
        assert "BUSYBOX_BASE=" in section, "Missing BUSYBOX_BASE arg"
        # Must NOT have image: directive within coredns section only
        assert "image:" not in section, "coredns must not have image: when using build:"

    def test_coredns_healthcheck_no_cmd_shell(self) -> None:
        """CoreDNS healthcheck uses CMD (not CMD-SHELL) with /wget."""
        root = Path(__file__).resolve().parents[2]
        content = (root / "src" / "templates" / "docker" / "compose.yml").read_text()
        # Find the coredns healthcheck test line
        coredns_idx = content.index("coredns:")
        # Find healthcheck test line within coredns section
        section = content[coredns_idx : content.index("\n\n", coredns_idx)]
        assert '"CMD"' in section or "'CMD'" in section, "Expected CMD in coredns healthcheck"
        assert '"CMD-SHELL"' not in section, "coredns healthcheck must not use CMD-SHELL"
        assert "/wget" in section, "coredns healthcheck must use /wget"

    def test_proxy_healthcheck_tcp_probe(self) -> None:
        """Proxy healthcheck uses TCP probe (not squidclient)."""
        root = Path(__file__).resolve().parents[2]
        content = (root / "src" / "templates" / "docker" / "compose.yml").read_text()
        # Find proxy service section
        proxy_idx = content.index("\n  proxy:")
        proxy_section = content[proxy_idx : content.index("\n\n", proxy_idx)]
        # Must use CMD, not CMD-SHELL
        assert '"CMD"' in proxy_section, "Expected CMD in proxy healthcheck"
        assert "/dev/tcp" in proxy_section, "Expected /dev/tcp in proxy healthcheck"
        assert "squidclient" not in proxy_section, "proxy healthcheck must not use squidclient"


class TestHydrationPipelineRegistration:
    """Group 6.T RED: CoreDNS Dockerfile pipeline registration in scaffold, hydration, and doctor."""

    def test_docker_coredns_in_instance_subdirs(self) -> None:
        """(a) 'docker/coredns' is in INSTANCE_SUBDIRS."""
        from core.scaffold import INSTANCE_SUBDIRS

        assert "docker/coredns" in INSTANCE_SUBDIRS

    def test_render_copies_coredns_dockerfile(self, tmp_path: Path) -> None:
        """(b) After render_templates(), docker/coredns/Dockerfile.coredns exists in instance
        and is identical to templates/docker/coredns/Dockerfile.coredns."""
        tooling = _build_minimal_tooling(tmp_path)
        instance = tmp_path / "instance"
        for d in [
            "docker/core",
            "docker/admin",
            "docker/extras",
            "docker/coredns",
            "config/admin",
            "config/core",
            "config/coredns",
            "config/dnsdist",
            "config/proxy",
        ]:
            (instance / d).mkdir(parents=True, exist_ok=True)

        ctx = _build_test_context(str(instance))
        render_templates(ctx, str(instance), db_postgres=False, mcp_firecrawl=False)

        rendered_df = instance / "docker" / "coredns" / "Dockerfile.coredns"
        assert rendered_df.exists(), "Dockerfile.coredns not copied to instance dir"
        source_df = (
            Path(__file__).resolve().parents[2] / "src" / "templates" / "docker" / "coredns" / "Dockerfile.coredns"
        )
        assert rendered_df.read_text() == source_df.read_text(), "Copied file differs from source"
        del tooling  # unused — render now uses packaged templates

    def test_validate_templates_counts_coredns_dockerfile(self, tmp_path: Path, monkeypatch: object) -> None:
        """(c) validate_templates() includes Dockerfile.coredns in its validated total."""
        from core.hydration import validate_templates

        tooling = _build_minimal_tooling(tmp_path)
        ctx = _build_test_context(str(tmp_path / "inst"))

        # Baseline count without coredns Dockerfile considered
        count, errors = validate_templates(ctx, db_postgres=False, mcp_firecrawl=False)
        assert errors == [], f"Unexpected errors: {errors}"

        # The coredns Dockerfile must be included in validated count.
        # We verify by checking that removing it causes a validation error.
        coredns_df = tooling / "docker" / "coredns" / "Dockerfile.coredns"
        coredns_df.unlink()
        _patch_templates_root(monkeypatch, tooling)
        count_missing, errors_missing = validate_templates(
            ctx,
            db_postgres=False,
            mcp_firecrawl=False,
        )
        assert any("Dockerfile.coredns" in e for e in errors_missing), (
            "validate_templates must report missing Dockerfile.coredns as error"
        )

    def test_unconditional_files_includes_coredns_dockerfile(self) -> None:
        """(d) _UNCONDITIONAL_FILES contains 'docker/coredns/Dockerfile.coredns' and has length 17."""
        from core.doctor import _UNCONDITIONAL_FILES

        assert "docker/coredns/Dockerfile.coredns" in _UNCONDITIONAL_FILES
        assert len(_UNCONDITIONAL_FILES) == 17

    def test_check_tooling_plane_references_17(self) -> None:
        """(e) check_tooling_plane detail string references '17' (not '16')."""
        from core.doctor import check_tooling_plane

        # Run against the actual repo — should pass (all files present)
        result = check_tooling_plane("testuser", None)
        # If it passes, the detail must say "17"
        if result.status == "pass":
            assert "17" in result.detail, f"Expected '17' in detail: {result.detail}"
            assert "16" not in result.detail, f"Detail still says '16': {result.detail}"


# ── Section: Integration Verification ─────────────────────────────────────────


class TestInfrastructureBugFixes:
    """9.T INTEGRATION: End-to-end validation across groups 1-8.

    All tests are expected to pass on first run. A failure is a regression.
    """

    def test_jinja_context_images_are_digest_pinned(self, tmp_path: Path) -> None:
        """(a) build_jinja_context produces digest-pinned infrastructure images."""
        ctx = _build_default_context(tmp_path)
        for key in ("busybox_image", "dns_image", "proxy_image", "dnsdist_image"):
            val = ctx[key]
            assert isinstance(val, str), f"{key} should be str"
            assert val, f"{key} should be non-empty"
            assert "@sha256:" in val, f"{key} should contain '@sha256:': {val}"

    def test_render_templates_produces_coredns_dockerfile(self, tmp_path: Path) -> None:
        """(b) render_templates() produces docker/coredns/Dockerfile.coredns in instance."""
        instance = tmp_path / "inst"
        for d in [
            "docker/core",
            "docker/admin",
            "docker/extras",
            "docker/coredns",
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

        ctx = _build_test_context(str(instance))
        render_templates(ctx, str(instance), db_postgres=False, mcp_firecrawl=False)

        coredns_dockerfile = instance / "docker" / "coredns" / "Dockerfile.coredns"
        assert coredns_dockerfile.exists(), "Dockerfile.coredns should be rendered"

    def test_validate_templates_zero_errors(self, tmp_path: Path) -> None:
        """(c) validate_templates() reports zero errors and counts include coredns Dockerfile."""
        from core.hydration import validate_templates

        ctx = _build_test_context(str(tmp_path / "inst"))
        count, errors = validate_templates(ctx, db_postgres=False, mcp_firecrawl=False)
        assert errors == [], f"Validation errors: {errors}"
        assert count > 0, "Expected at least one validated template"

    def test_compose_healthchecks_correct(self, tmp_path: Path) -> None:
        """(d) compose.yml: coredns has build block, proxy healthcheck uses /dev/tcp,
        coredns healthcheck uses /wget."""
        instance = tmp_path / "inst"
        for d in [
            "docker/core",
            "docker/admin",
            "docker/extras",
            "docker/coredns",
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

        ctx = _build_test_context(str(instance))
        render_templates(ctx, str(instance), db_postgres=False, mcp_firecrawl=False)

        compose = (instance / "docker" / "compose.yml").read_text()
        # coredns should have build: block, not image:
        assert "build:" in compose, "coredns should use build: block"
        # Proxy healthcheck uses /dev/tcp
        assert "/dev/tcp" in compose, "proxy healthcheck should use /dev/tcp"
        # coredns healthcheck uses /wget
        assert "/wget" in compose, "coredns healthcheck should use /wget"

    def test_dockerfile_user_lint_zero_violations(self) -> None:
        """(e) Dockerfile USER lint on both rendered Dockerfiles returns zero violations."""
        core_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "templates"
            / "docker"
            / "core"
            / "Dockerfile.core.wolfi"
        )
        admin_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "templates"
            / "docker"
            / "admin"
            / "Dockerfile.admin.debian"
        )

        for path in (core_path, admin_path):
            assert path.exists(), f"{path.name} not found"
            content = path.read_text()
            violations = _lint_dockerfile_user_context(content)
            assert violations == [], f"{path.name} lint violations: {violations}"

    def test_image_registry_integrity(self) -> None:
        """(f) IMAGE_REGISTRY has 7 entries with valid sha256 digests and consistent properties."""
        import re

        from core.hydration import IMAGE_REGISTRY, ImagePin

        assert len(IMAGE_REGISTRY) == 7, f"Expected 7 entries, got {len(IMAGE_REGISTRY)}"

        sha256_re = re.compile(r"^sha256:[a-f0-9]{64}$")
        for key, pin in IMAGE_REGISTRY.items():
            assert isinstance(pin, ImagePin), f"{key} is not an ImagePin"
            assert sha256_re.match(pin.digest), f"{key}: invalid digest {pin.digest}"
            assert pin.pinned == f"{pin.ref}@{pin.digest}", f"{key}: .pinned mismatch"
            assert pin.tagged == f"{pin.ref}:{pin.tag}", f"{key}: .tagged mismatch"


class TestDnsdistCommandArray:
    """Spec: dnsdist command array excludes binary name (dns-exfiltration-defense delta)."""

    def test_dnsdist_command_no_binary_name(self, tmp_path: Path) -> None:
        """Rendered compose.yml dnsdist command is arguments-only, no binary name."""
        from ruamel.yaml import YAML

        instance = tmp_path / "inst"
        for d in [
            "docker/core",
            "docker/admin",
            "docker/extras",
            "docker/coredns",
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

        ctx = _build_test_context(str(instance))
        render_templates(ctx, str(instance), db_postgres=False, mcp_firecrawl=False)

        compose_text = (instance / "docker" / "compose.yml").read_text()
        ry = YAML(typ="safe")
        compose_data = ry.load(compose_text)
        dnsdist_cmd = compose_data["services"]["dnsdist"]["command"]

        assert dnsdist_cmd == ["--supervised", "-C", "/etc/dnsdist/dnsdist.conf"], (
            f"Expected arguments-only command, got: {dnsdist_cmd}"
        )
        assert "dnsdist" not in dnsdist_cmd, "dnsdist binary name must not appear in the command array"


class TestTmuxGvisorPollingConfig:
    """Spec: tmux polling and activity monitoring tuned for gVisor (gvisor-resource-tuning delta)."""

    def test_tmux_gvisor_polling_config(self, tmp_path: Path) -> None:
        """Rendered .tmux.conf has gVisor-compatible polling: interval 30, activity off."""
        instance = tmp_path / "inst"
        for d in [
            "docker/core",
            "docker/admin",
            "docker/extras",
            "docker/coredns",
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

        ctx = _build_test_context(str(instance))
        render_templates(ctx, str(instance), db_postgres=False, mcp_firecrawl=False)

        tmux_text = (instance / "config" / "admin" / ".tmux.conf").read_text()

        # Positive assertions: correct gVisor-compatible values present
        assert "set -g status-interval 30" in tmux_text, "status-interval must be 30 for gVisor compatibility"
        assert "setw -g monitor-activity off" in tmux_text, "monitor-activity must be off for gVisor compatibility"
        assert "set -g visual-activity off" in tmux_text, "visual-activity must be off for gVisor compatibility"

        # Negative assertions: bare-metal defaults must not be present
        assert "status-interval 2" not in tmux_text, "bare-metal status-interval 2 must not remain in template"
        assert "monitor-activity on" not in tmux_text, "monitor-activity on must not remain in template"
        assert "visual-activity on" not in tmux_text, "visual-activity on must not remain in template"


class TestDbPostgresZeroCap:
    """1.T RED: db-postgres runs as user 70:70 with zero capabilities.

    Implements: compose-security-baseline/spec.md §Extras Services Zero-Capability Posture
    """

    def test_db_postgres_user_directive(self, tmp_path: Path) -> None:
        """db-postgres.yml rendered output contains user: "70:70"."""
        rendered = _render_extras(tmp_path, "db-postgres.yml")
        assert 'user: "70:70"' in rendered, (
            'db-postgres service must declare user: "70:70" (postgres uid:gid in Alpine)'
        )

    def test_db_postgres_no_cap_add(self, tmp_path: Path) -> None:
        """db-postgres.yml rendered output has no cap_add block."""
        rendered = _render_extras(tmp_path, "db-postgres.yml")
        assert "cap_add:" not in rendered, "db-postgres service must NOT contain a cap_add block"

    def test_db_postgres_cap_drop_all(self, tmp_path: Path) -> None:
        """db-postgres.yml rendered output contains cap_drop: [ALL]."""
        from ruamel.yaml import YAML

        rendered = _render_extras(tmp_path, "db-postgres.yml")
        ry = YAML(typ="safe")
        data = ry.load(rendered)
        svc = data["services"]["db-postgres"]
        assert svc.get("cap_drop") == ["ALL"], "db-postgres service must have cap_drop: [ALL]"

    def test_db_postgres_no_privilege_caps_in_cap_add(self, tmp_path: Path) -> None:
        """db-postgres.yml rendered output has no CHOWN/FOWNER/SETGID/SETUID in cap_add."""
        rendered = _render_extras(tmp_path, "db-postgres.yml")
        from ruamel.yaml import YAML

        ry = YAML(typ="safe")
        data = ry.load(rendered)
        svc = data["services"]["db-postgres"]
        cap_add = svc.get("cap_add", [])
        forbidden = {"CHOWN", "FOWNER", "SETGID", "SETUID"}
        present = forbidden & set(cap_add)
        assert present == set(), f"db-postgres cap_add must not contain {forbidden}, found: {present}"


class TestSecretsRemovalBindMounts:
    """2.T RED: Replace Docker Compose secrets: with bind-mounts.

    Implements: ssh-ipc-transport/spec.md §SSH Credential Mounts
    """

    def test_compose_no_top_level_secrets(self, tmp_path: Path) -> None:
        """compose.yml template source has no top-level secrets: block."""
        raw = (Path(__file__).parent.parent.parent / "src" / "templates" / "docker" / "compose.yml").read_text()
        # top-level secrets: is at column 0 (not indented)
        lines = raw.splitlines()
        top_level_secrets = [
            i for i, line in enumerate(lines, 1) if line.rstrip() == "secrets:" or line.startswith("secrets:")
        ]
        assert top_level_secrets == [], (
            f"compose.yml must NOT contain a top-level secrets: block, found at lines: {top_level_secrets}"
        )

    def test_compose_core_no_service_secrets(self, tmp_path: Path) -> None:
        """Core service block does NOT contain a secrets: entry."""
        rendered = _render_compose(tmp_path)
        core_start = rendered.index("\n  core:")
        admin_start = rendered.index("\n  admin:")
        core_block = rendered[core_start:admin_start]
        assert "secrets:" not in core_block, "Core service must NOT contain a secrets: entry"

    def test_compose_admin_no_service_secrets(self, tmp_path: Path) -> None:
        """Admin service block does NOT contain a secrets: entry."""
        rendered = _render_compose(tmp_path)
        admin_start = rendered.index("\n  admin:")
        admin_block = rendered[admin_start:]
        assert "secrets:" not in admin_block, "Admin service must NOT contain a secrets: entry"

    def test_compose_core_ipc_host_key_bind_mount(self, tmp_path: Path) -> None:
        """Core volumes include ipc_host_key:/run/secrets/ipc_host_key:ro bind-mount."""
        rendered = _render_compose(tmp_path)
        core_start = rendered.index("\n  core:")
        admin_start = rendered.index("\n  admin:")
        core_block = rendered[core_start:admin_start]
        assert "ipc_host_key:/run/secrets/ipc_host_key:ro" in core_block, (
            "Core volumes must include ipc_host_key bind-mount"
        )

    def test_compose_admin_ipc_ssh_key_bind_mount(self, tmp_path: Path) -> None:
        """Admin volumes include ipc_ssh_key:/run/secrets/ipc_ssh_key:ro bind-mount."""
        rendered = _render_compose(tmp_path)
        admin_start = rendered.index("\n  admin:")
        admin_block = rendered[admin_start:]
        assert "ipc_ssh_key:/run/secrets/ipc_ssh_key:ro" in admin_block, (
            "Admin volumes must include ipc_ssh_key bind-mount"
        )

    def test_compose_core_authorized_keys_unchanged(self, tmp_path: Path) -> None:
        """Core authorized_keys bind-mount unchanged."""
        rendered = _render_compose(tmp_path)
        core_start = rendered.index("\n  core:")
        admin_start = rendered.index("\n  admin:")
        core_block = rendered[core_start:admin_start]
        assert "authorized_keys:/run/secrets/authorized_keys:ro" in core_block

    def test_compose_admin_known_hosts_unchanged(self, tmp_path: Path) -> None:
        """Admin ipc_known_hosts bind-mount unchanged."""
        rendered = _render_compose(tmp_path)
        admin_start = rendered.index("\n  admin:")
        admin_block = rendered[admin_start:]
        assert "ipc_known_hosts:/run/secrets/ipc_known_hosts:ro" in admin_block


class TestCoreNonRootSshd:
    """3.T RED: Core Dockerfile transitions sshd to non-root agent-mode.

    Implements: ssh-ipc-transport/spec.md §Core Dockerfile USER for Non-Root sshd,
                §sshd-session File Capability for PTY Allocation
    """

    def test_dockerfile_final_user_is_agent(self) -> None:
        """Dockerfile.core.wolfi last USER directive before ENTRYPOINT is USER ${USERNAME}."""
        lines = _get_dockerfile_lines("docker/core/Dockerfile.core.wolfi")
        runtime_stage = _extract_stage(lines, "runtime")
        # Find the last USER directive in the runtime stage
        last_user = None
        for line in runtime_stage:
            stripped = line.strip()
            if stripped.startswith("USER "):
                last_user = stripped
        assert last_user is not None, "No USER directive in runtime stage"
        assert last_user == "USER ${USERNAME}", (
            f"Last USER directive must be 'USER ${'{'}USERNAME{'}'}', got: {last_user}"
        )

    def test_dockerfile_no_final_user_root(self) -> None:
        """Dockerfile.core.wolfi does NOT have USER root as the last USER directive."""
        lines = _get_dockerfile_lines("docker/core/Dockerfile.core.wolfi")
        runtime_stage = _extract_stage(lines, "runtime")
        last_user = None
        for line in runtime_stage:
            stripped = line.strip()
            if stripped.startswith("USER "):
                last_user = stripped
        assert last_user != "USER root", "Last USER directive in runtime stage must NOT be 'USER root'"

    def test_dockerfile_setcap_sshd_session(self) -> None:
        """Dockerfile.core.wolfi contains setcap cap_chown+ep /usr/lib/ssh/sshd-session."""
        lines = _get_dockerfile_lines("docker/core/Dockerfile.core.wolfi")
        content = "\n".join(lines)
        assert "setcap cap_chown+ep /usr/lib/ssh/sshd-session" in content, (
            "Dockerfile must contain setcap cap_chown+ep on sshd-session"
        )

    def test_dockerfile_setcap_not_targeting_sshd(self) -> None:
        """Dockerfile.core.wolfi does NOT contain setcap targeting /usr/sbin/sshd."""
        lines = _get_dockerfile_lines("docker/core/Dockerfile.core.wolfi")
        content = "\n".join(lines)
        assert "setcap" not in content or "/usr/sbin/sshd" not in content, (
            "setcap must NOT target /usr/sbin/sshd (must target /usr/lib/ssh/sshd-session)"
        )


class TestCoreCapAddSecurityOptTmpfs:
    """4.T RED: Core cap_add CHOWN, security_opt override, tmpfs mode=0755.

    Implements: compose-security-baseline/spec.md §Baseline Excludes List-Valued Properties,
                §tmpfs Mode for sshd StrictModes Compliance
    """

    def test_core_cap_add_chown(self, tmp_path: Path) -> None:
        """Core cap_add is [CHOWN], not [SETUID, SETGID]."""
        from ruamel.yaml import YAML

        rendered = _render_compose(tmp_path)
        ry = YAML(typ="safe")
        data = ry.load(rendered)
        cap_add = data["services"]["core"]["cap_add"]
        assert cap_add == ["CHOWN"], f"Core cap_add must be [CHOWN], got: {cap_add}"

    def test_core_security_opt_no_new_privs_false(self, tmp_path: Path) -> None:
        """Core has security_opt: [no-new-privileges:false]."""
        rendered = _render_compose(tmp_path)
        core_start = rendered.index("\n  core:")
        admin_start = rendered.index("\n  admin:")
        core_block = rendered[core_start:admin_start]
        assert "no-new-privileges:false" in core_block, "Core must have security_opt: [no-new-privileges:false]"

    def test_core_run_tmpfs_mode_0755(self, tmp_path: Path) -> None:
        """Core /run tmpfs has mode=0755."""
        rendered = _render_compose(tmp_path)
        core_start = rendered.index("\n  core:")
        admin_start = rendered.index("\n  admin:")
        core_block = rendered[core_start:admin_start]
        # Find /run tmpfs entry (not /var/run, not /home/...)
        assert "mode=0755" in core_block, "Core /run tmpfs must include mode=0755"

    def test_proxy_run_tmpfs_mode_0755(self, tmp_path: Path) -> None:
        """Proxy /run tmpfs has mode=0755."""
        rendered = _render_compose(tmp_path)
        proxy_start = rendered.index("\n  proxy:")
        core_start = rendered.index("\n  core:")
        proxy_block = rendered[proxy_start:core_start]
        assert "mode=0755" in proxy_block, "Proxy /run tmpfs must include mode=0755"

    def test_admin_no_run_tmpfs(self, tmp_path: Path) -> None:
        """Admin service has no /run tmpfs."""
        rendered = _render_compose(tmp_path)
        admin_start = rendered.index("\n  admin:")
        admin_block = rendered[admin_start:]
        # Check that no /run entry exists in admin tmpfs (but /home/.cache etc are fine)
        lines = admin_block.splitlines()
        in_tmpfs = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("tmpfs:"):
                in_tmpfs = True
                continue
            if in_tmpfs:
                if stripped.startswith("- /"):
                    # Check if this is a /run mount (not /var/run, not /home)
                    mount_path = stripped.lstrip("- ").split(":")[0]
                    assert mount_path != "/run", "Admin service must NOT have a /run tmpfs entry"
                elif not stripped.startswith("- "):
                    break  # End of tmpfs block

    def test_non_core_retain_no_new_privs_true(self, tmp_path: Path) -> None:
        """Non-core services (coredns, proxy, admin, dnsdist) retain no-new-privileges:true."""
        raw = (Path(__file__).parent.parent.parent / "src" / "templates" / "docker" / "compose.yml").read_text()
        # The baseline anchor must still have no-new-privileges:true
        start = raw.index("x-security-baseline:")
        end = raw.index("\nnetworks:")
        block = raw[start:end]
        assert "no-new-privileges:true" in block, "Security baseline must retain no-new-privileges:true"


class TestSshdPidFileEntrypoint:
    """5.T RED: sshd_config PidFile none + entrypoint cleanup.

    Implements: ssh-ipc-transport/spec.md §Hardened sshd_config Template,
                §Core Container sshd Runtime Directory
    """

    def test_sshd_config_pidfile_none(self) -> None:
        """sshd_config template contains PidFile none."""
        content = (
            Path(__file__).parent.parent.parent / "src" / "templates" / "config" / "core" / "sshd_config"
        ).read_text()
        assert "PidFile none" in content, "sshd_config must contain 'PidFile none' for non-root operation"

    def test_entrypoint_no_mkdir_run_sshd(self) -> None:
        """Core entrypoint.sh does NOT contain mkdir -p /run/sshd."""
        content = (
            Path(__file__).parent.parent.parent / "src" / "templates" / "docker" / "core" / "entrypoint.sh"
        ).read_text()
        assert "mkdir -p /run/sshd" not in content, (
            "Entrypoint must NOT contain 'mkdir -p /run/sshd' — PidFile none eliminates need"
        )


def _render_squid_conf(tmp_path: Path) -> str:
    """Render squid.conf template through Jinja2 with StrictUndefined."""
    import jinja2

    ctx = _build_test_context(str(tmp_path / "inst"))
    template_content = (
        Path(__file__).parent.parent.parent / "src" / "templates" / "config" / "proxy" / "squid.conf"
    ).read_text()
    env = jinja2.Environment(
        loader=jinja2.BaseLoader(),
        undefined=jinja2.StrictUndefined,
    )
    return env.from_string(template_content).render(ctx)


class TestSquidDnsNameservers:
    """6.T RED: Squid dns_nameservers points at CoreDNS egress IP.

    Implements: proxy-dns-resolution/spec.md §Squid Explicit DNS Nameserver
    """

    def test_squid_dns_nameservers_present(self, tmp_path: Path) -> None:
        """Rendered squid.conf contains dns_nameservers followed by the coredns egress IP."""
        rendered = _render_squid_conf(tmp_path)
        assert "dns_nameservers" in rendered, "squid.conf must contain a dns_nameservers directive"

    def test_squid_dns_nameservers_uses_coredns_egress_ip(self, tmp_path: Path) -> None:
        """Rendered squid.conf dns_nameservers resolves to the coredns egress IP."""
        rendered = _render_squid_conf(tmp_path)
        # coredns_egress_ip for slot 0 = 10.100.5.53
        lines = [line.strip() for line in rendered.splitlines() if line.strip().startswith("dns_nameservers")]
        assert len(lines) == 1, f"Expected exactly one dns_nameservers line, got: {lines}"
        assert "10.100.5.53" in lines[0], (
            f"dns_nameservers must resolve to coredns egress IP (10.100.5.53), got: {lines[0]}"
        )

    def test_squid_dns_nameservers_no_docker_dns(self, tmp_path: Path) -> None:
        """Rendered squid.conf does NOT contain 127.0.0.11 as DNS nameserver."""
        rendered = _render_squid_conf(tmp_path)
        assert "127.0.0.11" not in rendered, "squid.conf must NOT contain Docker internal DNS proxy 127.0.0.11"


class TestAdminRuntimeRunc:
    """7.T RED: Admin service hardcodes runc runtime.

    Implements: gvisor-resource-tuning/spec.md §Admin Service runc Runtime Override
    """

    def test_admin_runtime_is_runc(self) -> None:
        """Admin service in compose.yml template has runtime: "runc"."""
        raw = (Path(__file__).parent.parent.parent / "src" / "templates" / "docker" / "compose.yml").read_text()
        admin_start = raw.index("\n  admin:")
        admin_block = raw[admin_start:]
        # Find runtime: line in admin block
        runtime_lines = [line.strip() for line in admin_block.splitlines() if line.strip().startswith("runtime:")]
        assert len(runtime_lines) >= 1, "Admin service must have a runtime: directive"
        assert runtime_lines[0] == 'runtime: "runc"', f"Admin runtime must be 'runc', got: {runtime_lines[0]}"

    def test_admin_runtime_not_templated(self) -> None:
        """Admin service runtime is NOT the Jinja2 variable {{ runtime }}."""
        raw = (Path(__file__).parent.parent.parent / "src" / "templates" / "docker" / "compose.yml").read_text()
        admin_start = raw.index("\n  admin:")
        admin_block = raw[admin_start:]
        runtime_lines = [line.strip() for line in admin_block.splitlines() if line.strip().startswith("runtime:")]
        assert len(runtime_lines) >= 1
        assert "{{ runtime }}" not in runtime_lines[0], "Admin runtime must NOT use {{ runtime }} template variable"

    def test_core_retains_templated_runtime(self) -> None:
        """Core service retains runtime: "{{ runtime }}" (gVisor)."""
        raw = (Path(__file__).parent.parent.parent / "src" / "templates" / "docker" / "compose.yml").read_text()
        core_start = raw.index("\n  core:")
        admin_start = raw.index("\n  admin:")
        core_block = raw[core_start:admin_start]
        assert 'runtime: "{{ runtime }}"' in core_block, 'Core service must retain templated runtime: "{{ runtime }}"'


class TestTmuxPluginPaths:
    """8.T RED: tmux plugin paths match Dockerfile install location.

    Implements: admin-shell-config/spec.md §tmux Plugin Path Resolution
    """

    def test_tmux_plugin_manager_path_set(self) -> None:
        """.tmux.conf contains TMUX_PLUGIN_MANAGER_PATH set to /usr/local/tmux-plugins."""
        content = (
            Path(__file__).parent.parent.parent / "src" / "templates" / "config" / "admin" / ".tmux.conf"
        ).read_text()
        assert "set-environment -g TMUX_PLUGIN_MANAGER_PATH '/usr/local/tmux-plugins'" in content, (
            ".tmux.conf must set TMUX_PLUGIN_MANAGER_PATH to /usr/local/tmux-plugins"
        )

    def test_catppuccin_run_path_correct(self) -> None:
        """Catppuccin run path is /usr/local/tmux-plugins/catppuccin/tmux/catppuccin.tmux."""
        content = (
            Path(__file__).parent.parent.parent / "src" / "templates" / "config" / "admin" / ".tmux.conf"
        ).read_text()
        assert "run /usr/local/tmux-plugins/catppuccin/tmux/catppuccin.tmux" in content, (
            "Catppuccin run directive must reference /usr/local/tmux-plugins/"
        )

    def test_tpm_run_path_correct(self) -> None:
        """TPM run path is /usr/local/tmux-plugins/tpm/tpm."""
        content = (
            Path(__file__).parent.parent.parent / "src" / "templates" / "config" / "admin" / ".tmux.conf"
        ).read_text()
        assert "run /usr/local/tmux-plugins/tpm/tpm" in content, (
            "TPM run directive must reference /usr/local/tmux-plugins/"
        )

    def test_no_stale_home_config_plugin_paths(self) -> None:
        """.tmux.conf does NOT contain any ~/.config/tmux/plugins/ paths."""
        content = (
            Path(__file__).parent.parent.parent / "src" / "templates" / "config" / "admin" / ".tmux.conf"
        ).read_text()
        assert "~/.config/tmux/plugins/" not in content, (
            ".tmux.conf must NOT contain stale ~/.config/tmux/plugins/ paths"
        )


class TestLocaleTermExport:
    """9.T RED: C.UTF-8 locale and TERM default for admin shell.

    Implements: admin-shell-config/spec.md §C.UTF-8 Locale Configuration,
                §TERM Environment Variable Default
    """

    def test_zshrc_lang_c_utf8(self) -> None:
        """.zshrc contains LANG=C.UTF-8."""
        content = (
            Path(__file__).parent.parent.parent / "src" / "templates" / "config" / "admin" / ".zshrc"
        ).read_text()
        assert "LANG=C.UTF-8" in content, ".zshrc must set LANG=C.UTF-8"

    def test_zshrc_lc_all_c_utf8(self) -> None:
        """.zshrc contains LC_ALL=C.UTF-8."""
        content = (
            Path(__file__).parent.parent.parent / "src" / "templates" / "config" / "admin" / ".zshrc"
        ).read_text()
        assert "LC_ALL=C.UTF-8" in content, ".zshrc must set LC_ALL=C.UTF-8"

    def test_zshrc_no_en_us_utf8(self) -> None:
        """.zshrc does NOT contain en_US.UTF-8."""
        content = (
            Path(__file__).parent.parent.parent / "src" / "templates" / "config" / "admin" / ".zshrc"
        ).read_text()
        assert "en_US.UTF-8" not in content, ".zshrc must NOT contain en_US.UTF-8 — locale is not installed in image"

    def test_entrypoint_term_export_before_tmux(self) -> None:
        """Admin entrypoint.sh contains TERM export before tmux new-session."""
        content = (
            Path(__file__).parent.parent.parent / "src" / "templates" / "docker" / "admin" / "entrypoint.sh"
        ).read_text()
        assert 'export TERM="${TERM:-xterm-256color}"' in content, (
            "Admin entrypoint must export TERM with xterm-256color default"
        )
        # Verify ordering: TERM export before tmux
        term_pos = content.index('export TERM="${TERM:-xterm-256color}"')
        tmux_pos = content.index("tmux new-session")
        assert term_pos < tmux_pos, "TERM export must appear before tmux new-session"


class TestRootlessHardeningPosture:
    """11.T: Integration verification of the complete rootless hardening posture.

    Renders the full compose template + db-postgres extra and validates every
    security invariant from the 10-group hardening cycle in a single assertion
    group.  This is the capstone integration test — if this passes, the entire
    rootless migration is structurally correct at the template level.
    """

    def test_complete_rootless_security_posture(self, tmp_path: Path) -> None:
        """Full compose + db-postgres render validates the hardened security posture."""
        from ruamel.yaml import YAML

        compose_rendered = _render_compose(tmp_path)
        postgres_rendered = _render_extras(tmp_path, "db-postgres.yml")

        ry = YAML(typ="safe")
        compose = ry.load(compose_rendered)
        postgres = ry.load(postgres_rendered)

        errors: list[str] = []

        # ── Core: cap_add = [CHOWN] ───────────────────────────────────────
        core_svc = compose["services"]["core"]
        if core_svc.get("cap_add") != ["CHOWN"]:
            errors.append(f"Core cap_add must be ['CHOWN'], got: {core_svc.get('cap_add')}")

        # ── Core: no-new-privileges:false ─────────────────────────────────
        core_block = compose_rendered[compose_rendered.index("\n  core:") : compose_rendered.index("\n  admin:")]
        if "no-new-privileges:false" not in core_block:
            errors.append("Core must have security_opt containing no-new-privileges:false")

        # ── Core: /run tmpfs mode=0755 ────────────────────────────────────
        if "mode=0755" not in core_block:
            errors.append("Core /run tmpfs must include mode=0755")

        # ── Proxy: /run tmpfs mode=0755 ───────────────────────────────────
        proxy_block = compose_rendered[compose_rendered.index("\n  proxy:") : compose_rendered.index("\n  core:")]
        if "mode=0755" not in proxy_block:
            errors.append("Proxy /run tmpfs must include mode=0755")

        # ── Admin: runtime = runc ─────────────────────────────────────────
        admin_block = compose_rendered[compose_rendered.index("\n  admin:") :]
        admin_runtime_lines = [line.strip() for line in admin_block.splitlines() if line.strip().startswith("runtime:")]
        if not admin_runtime_lines or admin_runtime_lines[0] != 'runtime: "runc"':
            errors.append(f"Admin runtime must be 'runc', got: {admin_runtime_lines}")

        # ── No secrets: blocks ────────────────────────────────────────────
        raw_template = (
            Path(__file__).parent.parent.parent / "src" / "templates" / "docker" / "compose.yml"
        ).read_text()
        top_level_secrets = [
            line for line in raw_template.splitlines() if line.rstrip() == "secrets:" or line.startswith("secrets:")
        ]
        if top_level_secrets:
            errors.append(f"compose.yml must NOT contain a top-level secrets: block, found: {top_level_secrets}")

        if "secrets:" in core_block:
            errors.append("Core service must NOT contain a secrets: entry")

        if "secrets:" in admin_block:
            errors.append("Admin service must NOT contain a secrets: entry")

        # ── Bind-mounts present ───────────────────────────────────────────
        bind_mount_checks = {
            "core: ipc_host_key": (
                core_block,
                "ipc_host_key:/run/secrets/ipc_host_key:ro",
            ),
            "core: authorized_keys": (
                core_block,
                "authorized_keys:/run/secrets/authorized_keys:ro",
            ),
            "admin: ipc_ssh_key": (
                admin_block,
                "ipc_ssh_key:/run/secrets/ipc_ssh_key:ro",
            ),
            "admin: ipc_known_hosts": (
                admin_block,
                "ipc_known_hosts:/run/secrets/ipc_known_hosts:ro",
            ),
        }
        for label, (block, expected) in bind_mount_checks.items():
            if expected not in block:
                errors.append(f"Missing bind-mount in {label}: {expected}")

        # ── db-postgres: user=70:70, zero caps ───────────────────────────
        pg_svc = postgres["services"]["db-postgres"]
        if pg_svc.get("user") != "70:70":
            errors.append(f"db-postgres user must be '70:70', got: {pg_svc.get('user')}")
        if "cap_add" in pg_svc:
            errors.append(f"db-postgres must NOT have cap_add, found: {pg_svc['cap_add']}")
        if pg_svc.get("cap_drop") != ["ALL"]:
            errors.append(f"db-postgres cap_drop must be ['ALL'], got: {pg_svc.get('cap_drop')}")

        # ── Final verdict ─────────────────────────────────────────────────
        assert errors == [], "Rootless hardening posture violations:\n  - " + "\n  - ".join(errors)
