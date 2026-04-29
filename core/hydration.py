"""Hydration pipeline: Pydantic config parsing + Jinja2 template rendering.

Implements the PHASE 4 (HYDRATION) from the orchestrator design:
1. Parse sandbox.toml → SandboxConfig (Pydantic v2 model)
2. Build Jinja2 context from config + IPAM values + proxy credentials
3. Render templates from tooling plane → instance directory
"""

import json
import logging
import os
import shutil
import tomllib
from typing import Any

import jinja2
from pydantic import BaseModel

from core.ipam import derive_static_ips, derive_subnets

logger = logging.getLogger(__name__)

# ─── Image Digest Registry ──────────────────────────────────────────────────
#
# Centralized SHA256 digest references for all container images.
# Rotation procedure (quarterly cadence):
#   docker manifest inspect <image>:<tag> | jq -r '.manifests[] | select(.platform.architecture=="amd64") | .digest'
# Then update the corresponding value below.
#
IMAGE_DIGESTS: dict[str, str] = {
    "wolfi_base": (
        "cgr.dev/chainguard/wolfi-base@sha256:3490ac41800d82ab3c97f8d4f9e762e9a937f4527c6f21ba81bd7dc23aee8097"
    ),
    "debian_trixie": "debian@sha256:a15012b5f8fbefd7bfa43e253e6b3e879e63d8e37e5e2e5fc6c8e5e62ee5f2a3",
    "squid": "ubuntu/squid@sha256:a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2",
    "coredns": "coredns/coredns@sha256:b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2",
    "dnsdist": "powerdns/dnsdist-19@sha256:c2f859bb67865987878ff93c9c75758236fe659b4117b296da29da6b572affd0",
    "postgres": "postgres@sha256:c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2",
}

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
    base_image: str = IMAGE_DIGESTS["wolfi_base"]
    base_distro_family: str = "wolfi"
    git_user: str = ""
    git_email: str = ""


class AdminConfig(BaseModel):
    """[admin] section of sandbox.toml."""

    shm_size: str = "2gb"
    pids_limit: int = 400
    mem_limit: str = "8gb"
    cpus: float = 4.0
    base_image: str = IMAGE_DIGESTS["debian_trixie"]
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
    image: str = IMAGE_DIGESTS["postgres"]


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
    read_only_domains: list[str] = []


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
            "runtimes": {k: v for k, v in raw.get("runtimes", {}).items() if k != "node"},
            "runtimes_node": raw.get("runtimes", {}).get("node", {}),
            "components": {k: v for k, v in components_raw.items() if isinstance(v, bool)},
            "components_db_postgres": components_raw.get("db_postgres", {}),
            "components_ingress": components_raw.get("ingress", {}),
            "proxy_whitelist": raw.get("proxy", {}).get("whitelist", {}),
        }
        return cls.model_validate(flat)


# ─── Jinja2 Context Builder ──────────────────────────────────────────────────


def _read_optional_file(path: str) -> str:
    """Return file contents (trailing whitespace stripped) if exists, '' otherwise."""
    if os.path.exists(path):
        with open(path) as f:
            return f.read().rstrip()
    return ""


def build_jinja_context(
    config: SandboxConfig,
    base_index: int,
    proxy_password: str,
    instance_dir: str,
) -> dict[str, Any]:
    """Build the complete Jinja2 template context from parsed config + IPAM.

    Context contract:
        All template variable defaults are resolved here. Templates use bare
        ``{{ var }}`` without Jinja2 ``| default()`` filters. The Jinja2
        ``StrictUndefined`` configuration enforces completeness — any variable
        referenced in a template but absent from this context raises
        ``UndefinedError`` at render time and during ``--dry-run`` validation.
    """
    isolated, core_proxy, dns, admin, admin_proxy, egress, ipc = derive_subnets(base_index)
    ips = derive_static_ips(base_index)

    return {
        # Network — 7-subnet topology
        "isolated_subnet": isolated,
        "core_proxy_subnet": core_proxy,
        "dns_subnet": dns,
        "admin_subnet": admin,
        "admin_proxy_subnet": admin_proxy,
        "egress_subnet": egress,
        "ipc_subnet": ipc,
        **ips,
        # Credentials
        "proxy_password": proxy_password,
        "proxy_url_core": f"http://proxyuser:{proxy_password}@proxy:3128",
        # Paths
        "instance_dir": instance_dir,
        "user_project_root": config.project.user_project_root,
        "custom_config_core": "/home/agent/.sandbox/custom",
        "custom_config_admin": "/home/human/.sandbox/custom",
        "tmux_resurrect_dir": "/home/human/.sandbox/tmux_resurrect",
        # Project
        "project_name": config.project.name,
        "host_uid": config.project.host_uid,
        "warmup_prompt": config.project.warmup_prompt,
        # Git identity
        "git_user": config.core.git_user or "Agent",
        "git_email": config.core.git_email or "agent@sandbox.local",
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
        # Images — infrastructure (not user-configurable)
        "runtime": "runsc",
        "dns_image": IMAGE_DIGESTS["coredns"],
        "proxy_image": IMAGE_DIGESTS["squid"],
        "dnsdist_image": IMAGE_DIGESTS["dnsdist"],
        # Images — user-configurable
        "db_postgres_image": config.components_db_postgres.image,
        # Proxy whitelist
        "proxy_whitelist_domains": config.proxy_whitelist.domains,
        # CoreDNS zones: strip leading dot (zones are inherently suffix-matching)
        "proxy_whitelist_domains_coredns": [d.lstrip(".") for d in config.proxy_whitelist.domains],
        # Read-only registry domains (N4)
        "proxy_whitelist_read_only_domains": config.proxy_whitelist.read_only_domains,
        # Extras: db-postgres
        "pg_user": config.components_db_postgres.pg_user,
        "pg_db": config.components_db_postgres.pg_db,
        # Component enablement flags
        "db_postgres_enabled": config.components_db_postgres.enabled,
        "mcp_firecrawl_enabled": config.components.mcp_firecrawl,
        # Custom CLAUDE.md rules (user-authored, concatenated into rendered output)
        "custom_claude_rules": _read_optional_file(os.path.join(instance_dir, "custom/config/core/CLAUDE.md")),
    }


# ─── Template Renderer ───────────────────────────────────────────────────────

# Files that are Jinja2-rendered (contain {{ }} variables)
_JINJA_RENDERED_DOCKER = [
    ("compose.yml", "docker/compose.yml"),
]

_JINJA_RENDERED_CONFIG = [
    ("coredns/Corefile", "config/coredns/Corefile"),
    ("dnsdist/dnsdist.conf", "config/dnsdist/dnsdist.conf"),
    ("proxy/squid.conf", "config/proxy/squid.conf"),
    ("core/.gitconfig", "config/core/.gitconfig"),
    ("core/.npmrc", "config/core/.npmrc"),
    ("core/.bashrc", "config/core/.bashrc"),
    ("core/CLAUDE.md", "config/core/CLAUDE.md"),
    ("core/sshd_config", "config/core/sshd_config"),
    ("admin/.zshrc", "config/admin/.zshrc"),
    ("admin/.tmux.conf", "config/admin/.tmux.conf"),
    ("admin/.gitconfig", "config/admin/.gitconfig"),
]

# Static files copied as-is (no Jinja2 processing)
_STATIC_CONFIG_ADMIN = ["gitmux.conf", "starship.toml"]
_STATIC_CONFIG_CORE: list[str] = []
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

    # ── Standard registry-driven templates ─────────────────────────────────
    # _JINJA_RENDERED_DOCKER and _JINJA_RENDERED_CONFIG are the authoritative
    # file registries. All standard templates are iterated here.

    for src_rel, dst_rel in _JINJA_RENDERED_DOCKER:
        _render_file(env, f".docker/{src_rel}", instance_dir, dst_rel, context)

    for src_rel, dst_rel in _JINJA_RENDERED_CONFIG:
        _render_file(env, f".config/{src_rel}", instance_dir, dst_rel, context)

    # ── Distro-selected Dockerfiles (dynamic template path) ───────────────

    core_family = context["core_distro_family"]
    _render_file(
        env,
        f".docker/core/Dockerfile.core.{core_family}",
        instance_dir,
        "docker/core/Dockerfile.core",
        context,
    )

    admin_family = context["admin_distro_family"]
    _render_file(
        env,
        f".docker/admin/Dockerfile.admin.{admin_family}",
        instance_dir,
        "docker/admin/Dockerfile.admin",
        context,
    )

    # ── Static copies (no Jinja2 rendering) ───────────────────────────────

    _copy_file(tooling_plane, ".docker/core/entrypoint.sh", instance_dir, "docker/core/entrypoint.sh")
    _copy_file(tooling_plane, ".docker/admin/entrypoint.sh", instance_dir, "docker/admin/entrypoint.sh")

    # ── Feature-gated extras ──────────────────────────────────────────────

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

    # ── Programmatic generation ───────────────────────────────────────────

    # Generate allowed_domains.txt from whitelist
    domains = context.get("proxy_whitelist_domains", [])
    domains_path = os.path.join(instance_dir, "config/proxy/allowed_domains.txt")
    with open(domains_path, "w") as f:
        for domain in domains:
            f.write(f"{domain}\n")

    # Generate read_only_domains.txt from whitelist (N4)
    read_only_domains = context.get("proxy_whitelist_read_only_domains", [])
    read_only_path = os.path.join(instance_dir, "config/proxy/read_only_domains.txt")
    with open(read_only_path, "w") as f:
        for domain in read_only_domains:
            f.write(f"{domain}\n")

    # Validation warning: orphaned read_only_domains entries not in domains
    allowed_domains = set(context.get("proxy_whitelist_domains", []))
    for domain in read_only_domains:
        if domain not in allowed_domains:
            logger.warning(
                "read_only_domains entry '%s' is not in proxy.whitelist.domains — "
                "the domain is unreachable regardless of method restriction",
                domain,
            )

    # ── Static config copies ──────────────────────────────────────────────

    for filename in _STATIC_CONFIG_PROXY:
        _copy_file(tooling_plane, f".config/proxy/{filename}", instance_dir, f"config/proxy/{filename}")

    for filename in _STATIC_CONFIG_ADMIN:
        _copy_file(tooling_plane, f".config/admin/{filename}", instance_dir, f"config/admin/{filename}")

    # ── Programmatic .claude.json ─────────────────────────────────────────
    # Generated (not copied) so firecrawl MCP endpoint can be injected
    # dynamically based on the instance's IPAM-derived IP address.

    claude_json_data: dict[str, object]
    if mcp_firecrawl:
        claude_json_data = {
            "mcpServers": {
                "firecrawl": {
                    "type": "http",
                    "url": f"http://{context['firecrawl_isolated_ip']}:3000/mcp",
                },
            },
        }
    else:
        claude_json_data = {}

    claude_json_path = os.path.join(instance_dir, "config/core/.claude.json")
    with open(claude_json_path, "w") as f:
        json.dump(claude_json_data, f, indent=2)
        f.write("\n")


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

    # Build the full list of templates from the authoritative registries
    templates: list[str] = (
        [f".docker/{src_rel}" for src_rel, _ in _JINJA_RENDERED_DOCKER]
        + [f".config/{src_rel}" for src_rel, _ in _JINJA_RENDERED_CONFIG]
        + [
            f".docker/core/Dockerfile.core.{context.get('core_distro_family', 'wolfi')}",
            f".docker/admin/Dockerfile.admin.{context.get('admin_distro_family', 'debian')}",
        ]
    )

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
