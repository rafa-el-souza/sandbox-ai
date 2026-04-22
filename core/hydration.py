"""Hydration pipeline: Pydantic config parsing + Jinja2 template rendering.

Implements the PHASE 4 (HYDRATION) from the orchestrator design:
1. Parse sandbox.toml → SandboxConfig (Pydantic v2 model)
2. Build Jinja2 context from config + IPAM values + proxy credentials
3. Render templates from tooling plane → instance directory
"""

import os
import shutil
import tomllib
from typing import Any

import jinja2
from pydantic import BaseModel

from core.ipam import derive_static_ips, derive_subnets

# ─── Pydantic Models ─────────────────────────────────────────────────────────


class ProjectConfig(BaseModel):
    """[project] section of sandbox.toml."""

    name: str
    user_project_root: str
    host_unprivileged_user: str
    host_uid: str
    warmup_prompt: str = ""


class CoreConfig(BaseModel):
    """[core] section of sandbox.toml."""

    shm_size: str = "2gb"
    pids_limit: int = 400
    mem_limit: str = "8gb"
    cpus: float = 4.0
    base_image: str = "cgr.dev/chainguard/wolfi-base:latest"
    base_distro_family: str = "wolfi"
    git_user: str = ""
    git_email: str = ""


class AdminConfig(BaseModel):
    """[admin] section of sandbox.toml."""

    shm_size: str = "2gb"
    pids_limit: int = 400
    mem_limit: str = "8gb"
    cpus: float = 4.0
    base_image: str = "debian:trixie-slim"
    base_distro_family: str = "debian"


class RuntimesConfig(BaseModel):
    """[runtimes] section of sandbox.toml."""

    python: bool = True
    typescript: bool = True
    rust: bool = True
    go: bool = False


class NodeConfig(BaseModel):
    """[runtimes.node] section of sandbox.toml."""

    version: str = "20.12.2"
    nvm_version: str = "0.39.7"


class DbPostgresConfig(BaseModel):
    """[components.db_postgres] section."""

    enabled: bool = True
    expose_host_ports: list[int] = [5432]
    pg_user: str = "sandbox"
    pg_db: str = "sandbox_db"


class IngressConfig(BaseModel):
    """[components.ingress] section."""

    web_ports: list[int] = [3000, 8080]


class ComponentsConfig(BaseModel):
    """[components] section of sandbox.toml (flat boolean toggles only)."""

    mcp_firecrawl: bool = False
    mcp_puppeteer: bool = False


class ProxyWhitelistConfig(BaseModel):
    """[proxy.whitelist] section of sandbox.toml."""

    domains: list[str] = []


class SandboxConfig(BaseModel):
    """Top-level Pydantic model for sandbox.toml."""

    project: ProjectConfig
    core: CoreConfig = CoreConfig()
    admin: AdminConfig = AdminConfig()
    runtimes: RuntimesConfig = RuntimesConfig()
    runtimes_node: NodeConfig = NodeConfig()
    components: ComponentsConfig = ComponentsConfig()
    components_db_postgres: DbPostgresConfig = DbPostgresConfig()
    components_ingress: IngressConfig = IngressConfig()
    proxy_whitelist: ProxyWhitelistConfig = ProxyWhitelistConfig()

    @classmethod
    def from_toml(cls, toml_path: str) -> SandboxConfig:
        """Parse sandbox.toml into a validated SandboxConfig."""
        with open(toml_path, "rb") as f:
            raw = tomllib.load(f)

        # Flatten nested TOML tables into the model's expected structure
        components_raw = raw.get("components", {})
        flat: dict[str, Any] = {
            "project": raw.get("project", {}),
            "core": raw.get("core", {}),
            "admin": raw.get("admin", {}),
            "runtimes": {
                k: v
                for k, v in raw.get("runtimes", {}).items()
                if k != "node"
            },
            "runtimes_node": raw.get("runtimes", {}).get("node", {}),
            "components": {
                k: v
                for k, v in components_raw.items()
                if isinstance(v, bool)
            },
            "components_db_postgres": components_raw.get("db_postgres", {}),
            "components_ingress": components_raw.get("ingress", {}),
            "proxy_whitelist": raw.get("proxy", {}).get("whitelist", {}),
        }
        return cls.model_validate(flat)


# ─── Jinja2 Context Builder ──────────────────────────────────────────────────


def build_jinja_context(
    config: SandboxConfig,
    base_index: int,
    proxy_password: str,
    instance_dir: str,
) -> dict[str, Any]:
    """Build the complete Jinja2 template context from parsed config + IPAM."""
    isolated, proxy, egress = derive_subnets(base_index)
    ips = derive_static_ips(base_index)

    return {
        # Network
        "isolated_subnet": isolated,
        "proxy_subnet": proxy,
        "egress_subnet": egress,
        **ips,
        # Credentials
        "proxy_password": proxy_password,
        # Paths
        "instance_dir": instance_dir,
        "user_project_root": config.project.user_project_root,
        # Project
        "project_name": config.project.name,
        "host_uid": config.project.host_uid,
        "warmup_prompt": config.project.warmup_prompt,
        # Core
        "core_base_image": config.core.base_image,
        "core_distro_family": config.core.base_distro_family,
        "core_pids_limit": config.core.pids_limit,
        "core_shm_size": config.core.shm_size,
        "core_mem_limit": config.core.mem_limit,
        "core_memswap_limit": config.core.mem_limit,
        "core_cpus": str(config.core.cpus),
        # Admin
        "admin_base_image": config.admin.base_image,
        "admin_distro_family": config.admin.base_distro_family,
        "admin_pids_limit": config.admin.pids_limit,
        "admin_shm_size": config.admin.shm_size,
        "admin_mem_limit": config.admin.mem_limit,
        "admin_memswap_limit": config.admin.mem_limit,
        "admin_cpus": str(config.admin.cpus),
        # Runtimes
        "runtimes": {
            "python": config.runtimes.python,
            "typescript": config.runtimes.typescript,
            "rust": config.runtimes.rust,
            "go": config.runtimes.go,
        },
        "nvm_version": config.runtimes_node.nvm_version,
        "node_version": config.runtimes_node.version,
        # Images (defaults — can be made configurable later)
        "runtime": "runsc",
        "dns_image": "coredns/coredns:1.11.1",
        "proxy_image": "ubuntu/squid:latest",
        # Proxy whitelist
        "proxy_whitelist_domains": config.proxy_whitelist.domains,
        # Extras: db-postgres
        "pg_user": config.components_db_postgres.pg_user,
        "pg_db": config.components_db_postgres.pg_db,
    }


# ─── Template Renderer ───────────────────────────────────────────────────────

# Files that are Jinja2-rendered (contain {{ }} variables)
_JINJA_RENDERED_DOCKER = [
    ("compose.yml", "docker/compose.yml"),
]

_JINJA_RENDERED_CONFIG = [
    ("dns-sidecar/Corefile", "config/dns-sidecar/Corefile"),
    ("proxy/squid.conf", "config/proxy/squid.conf"),
]

# Static files copied as-is (no Jinja2 processing)
_STATIC_CONFIG_ADMIN = [".zshrc", ".tmux.conf", "gitmux.conf", "starship.toml"]
_STATIC_CONFIG_CORE = [".bashrc", ".npmrc", ".gitconfig", ".claude.json", "CLAUDE.md"]
_STATIC_CONFIG_PROXY = ["ERR_SANDBOX_403"]


def render_templates(
    context: dict[str, Any],
    tooling_plane: str,
    instance_dir: str,
    *,
    db_postgres: bool,
    mcp_firecrawl: bool,
) -> None:
    """Render all templates from tooling plane into instance directory.

    Renders: .docker/ → instance/docker/, .config/ → instance/config/
    Skips: precious state (sandbox.toml, .sandbox.env, custom/, cache/, log/)
    """
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(tooling_plane),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )

    # ── Docker templates ──────────────────────────────────────────────────

    # compose.yml
    _render_file(env, ".docker/compose.yml", instance_dir, "docker/compose.yml", context)

    # Core Dockerfile (selected by distro family)
    core_family = context["core_distro_family"]
    _render_file(
        env,
        f".docker/core/Dockerfile.core.{core_family}",
        instance_dir,
        "docker/core/Dockerfile.core",
        context,
    )
    # Core entrypoint (static copy)
    _copy_file(tooling_plane, ".docker/core/entrypoint.sh", instance_dir, "docker/core/entrypoint.sh")

    # Admin Dockerfile (selected by distro family)
    admin_family = context["admin_distro_family"]
    _render_file(
        env,
        f".docker/admin/Dockerfile.admin.{admin_family}",
        instance_dir,
        "docker/admin/Dockerfile.admin",
        context,
    )
    # Admin entrypoint (static copy)
    _copy_file(tooling_plane, ".docker/admin/entrypoint.sh", instance_dir, "docker/admin/entrypoint.sh")

    # Component-conditional extras
    if db_postgres:
        _render_file(env, ".docker/extras/db-postgres.yml", instance_dir, "docker/extras/db-postgres.yml", context)
    if mcp_firecrawl:
        _render_file(env, ".docker/extras/mcp-firecrawl.yml", instance_dir, "docker/extras/mcp-firecrawl.yml", context)
        _copy_file(
            tooling_plane,
            ".docker/extras/Dockerfile.mcp-firecrawl",
            instance_dir,
            "docker/extras/Dockerfile.mcp-firecrawl",
        )

    # ── Config templates ──────────────────────────────────────────────────

    # DNS sidecar Corefile (Jinja2)
    _render_file(env, ".config/dns-sidecar/Corefile", instance_dir, "config/dns-sidecar/Corefile", context)

    # Proxy squid.conf (Jinja2)
    _render_file(env, ".config/proxy/squid.conf", instance_dir, "config/proxy/squid.conf", context)

    # Generate allowed_domains.txt from whitelist
    domains = context.get("proxy_whitelist_domains", [])
    domains_path = os.path.join(instance_dir, "config/proxy/allowed_domains.txt")
    with open(domains_path, "w") as f:
        for domain in domains:
            f.write(f"{domain}\n")

    # Generate trusted_clients.acl
    acl_path = os.path.join(instance_dir, "config/proxy/trusted_clients.acl")
    with open(acl_path, "w") as f:
        f.write(f"{context['isolated_subnet']}\n")
        f.write(f"{context['proxy_subnet']}\n")

    # Static proxy files
    for filename in _STATIC_CONFIG_PROXY:
        _copy_file(tooling_plane, f".config/proxy/{filename}", instance_dir, f"config/proxy/{filename}")

    # Static admin configs
    for filename in _STATIC_CONFIG_ADMIN:
        _copy_file(tooling_plane, f".config/admin/{filename}", instance_dir, f"config/admin/{filename}")

    # Static core configs
    for filename in _STATIC_CONFIG_CORE:
        _copy_file(tooling_plane, f".config/core/{filename}", instance_dir, f"config/core/{filename}")


def _render_file(
    env: jinja2.Environment,
    template_rel: str,
    instance_dir: str,
    output_rel: str,
    context: dict[str, Any],
) -> None:
    """Render a single Jinja2 template to the instance directory."""
    template = env.get_template(template_rel)
    rendered = template.render(context)
    output_path = os.path.join(instance_dir, output_rel)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        f.write(rendered)


def _copy_file(
    tooling_plane: str,
    src_rel: str,
    instance_dir: str,
    dst_rel: str,
) -> None:
    """Copy a static file from tooling plane to instance directory."""
    src = os.path.join(tooling_plane, src_rel)
    dst = os.path.join(instance_dir, dst_rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)


# ─── Dry-Run Validation ─────────────────────────────────────────────────────


def validate_templates(
    context: dict[str, Any],
    tooling_plane: str,
    *,
    db_postgres: bool,
    mcp_firecrawl: bool,
) -> tuple[int, list[str]]:
    """Render all Jinja2 templates to memory without writing.

    Returns (count_of_validated_templates, list_of_errors).
    Empty errors list means all templates are valid.
    """
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(tooling_plane),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )

    errors: list[str] = []
    validated = 0

    # Build the full list of templates to validate
    templates: list[str] = [
        ".docker/compose.yml",
        f".docker/core/Dockerfile.core.{context.get('core_distro_family', 'wolfi')}",
        f".docker/admin/Dockerfile.admin.{context.get('admin_distro_family', 'debian')}",
        ".config/dns-sidecar/Corefile",
        ".config/proxy/squid.conf",
    ]

    if db_postgres:
        templates.append(".docker/extras/db-postgres.yml")
    if mcp_firecrawl:
        templates.append(".docker/extras/mcp-firecrawl.yml")

    for template_rel in templates:
        try:
            template = env.get_template(template_rel)
            template.render(context)
            validated += 1
        except jinja2.TemplateNotFound:
            errors.append(f"Template not found: {template_rel}")
        except jinja2.TemplateSyntaxError as e:
            errors.append(f"Syntax error in {template_rel}: {e}")
        except jinja2.UndefinedError as e:
            errors.append(f"Undefined variable in {template_rel}: {e}")

    # Verify static files exist
    static_files = (
        [f".config/proxy/{f}" for f in _STATIC_CONFIG_PROXY]
        + [f".config/admin/{f}" for f in _STATIC_CONFIG_ADMIN]
        + [f".config/core/{f}" for f in _STATIC_CONFIG_CORE]
        + [".docker/core/entrypoint.sh", ".docker/admin/entrypoint.sh"]
    )
    if mcp_firecrawl:
        static_files.append(".docker/extras/Dockerfile.mcp-firecrawl")

    for rel_path in static_files:
        abs_path = os.path.join(tooling_plane, rel_path)
        if os.path.exists(abs_path):
            validated += 1
        else:
            errors.append(f"Static file missing: {rel_path}")

    return validated, errors

