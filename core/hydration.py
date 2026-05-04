"""Hydration pipeline: Pydantic config parsing + Jinja2 template rendering.

Implements the PHASE 4 (HYDRATION) from the orchestrator design:
1. Parse sandbox.toml → InstanceConfig (Pydantic v2 model)
2. Build Jinja2 context from config + IPAM values + proxy credentials
3. Render templates from tooling plane → instance directory
"""

import json
import logging
import os
import shutil
import tomllib
from dataclasses import dataclass as _dataclass
from typing import Any

import jinja2
from pydantic import BaseModel

from core.ipam import derive_static_ips, derive_subnets

logger = logging.getLogger(__name__)


# ─── Image Digest Registry ──────────────────────────────────────────────────
#
# Centralized image pin registry. Each entry carries ref, tag, and digest
# as separate fields. Derived properties:
#   .pinned → "ref@sha256:..."  (for Dockerfile FROM / compose image:)
#   .tagged → "ref:tag"         (for human-readable display / rotation)
#
# Rotation procedure: scripts/rotate_digests.py
#   Resolves current tag digests, verifies signatures, patches this file.
#


@_dataclass(frozen=True)
class ImagePin:
    """Immutable structured image pin with ref, tag, and digest fields."""

    ref: str
    tag: str
    digest: str

    @property
    def pinned(self) -> str:
        """Return digest-qualified reference: ref@sha256:..."""
        return f"{self.ref}@{self.digest}"

    @property
    def tagged(self) -> str:
        """Return tag-qualified reference: ref:tag."""
        return f"{self.ref}:{self.tag}"


IMAGE_REGISTRY: dict[str, ImagePin] = {
    "wolfi_base": ImagePin(
        ref="cgr.dev/chainguard/wolfi-base",
        tag="latest",
        digest="sha256:d6e31fc9dad5fd76d7160ba833502865e17c226ab261cb6893a0624b68198d7b",
    ),
    "debian_trixie": ImagePin(
        ref="debian",
        tag="trixie",
        digest="sha256:35b8ff74ead4880f22090b617372daff0ccae742eb5674455d542bef71ef1999",
    ),
    "squid": ImagePin(
        ref="ubuntu/squid",
        tag="latest",
        digest="sha256:6a097f68bae708cedbabd6188d68c7e2e7a38cedd05a176e1cc0ba29e3bbe029",
    ),
    "coredns": ImagePin(
        ref="coredns/coredns",
        tag="1.11.1",
        digest="sha256:1eeb4c7316bacb1d4c8ead65571cd92dd21e27359f0d4917f1a5822a73b75db1",
    ),
    "dnsdist": ImagePin(
        ref="powerdns/dnsdist-19",
        tag="1.9.14",
        digest="sha256:c2f859bb67865987878ff93c9c75758236fe659b4117b296da29da6b572affd0",
    ),
    "postgres": ImagePin(
        ref="postgres",
        tag="16-alpine",
        digest="sha256:4e6e670bb069649261c9c18031f0aded7bb249a5b6664ddec29c013a89310d50",
    ),
    "busybox_musl": ImagePin(
        ref="busybox",
        tag="1.36.1-musl",
        digest="sha256:3c6ae8008e2c2eedd141725c30b20d9c36b026eb796688f88205845ef17aa213",
    ),
}

# ─── Pydantic Models ─────────────────────────────────────────────────────────


class SandboxInstanceSection(BaseModel):
    """[project] section of sandbox.toml."""

    name: str
    user_project_root: str
    host_uid: str
    warmup_prompt: str = ""


class CoreConfig(BaseModel):
    """[core] section of sandbox.toml."""

    shm_size: str = "2gb"
    pids_limit: int = 400
    mem_limit: str = "8gb"
    cpus: float = 4.0
    base_image: str = IMAGE_REGISTRY["wolfi_base"].pinned
    base_distro_family: str = "wolfi"
    git_user: str = ""
    git_email: str = ""


class AdminConfig(BaseModel):
    """[admin] section of sandbox.toml."""

    shm_size: str = "2gb"
    pids_limit: int = 400
    mem_limit: str = "8gb"
    cpus: float = 4.0
    base_image: str = IMAGE_REGISTRY["debian_trixie"].pinned
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
    image: str = IMAGE_REGISTRY["postgres"].pinned


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


class InstanceConfig(BaseModel):
    """Top-level Pydantic model for sandbox.toml."""

    instance: SandboxInstanceSection
    core: CoreConfig = CoreConfig()
    admin: AdminConfig = AdminConfig()
    runtimes: RuntimesConfig = RuntimesConfig()
    runtimes_node: NodeConfig = NodeConfig()
    components: ComponentsConfig = ComponentsConfig()
    components_db_postgres: DbPostgresConfig = DbPostgresConfig()
    components_ingress: IngressConfig = IngressConfig()
    proxy_whitelist: ProxyWhitelistConfig = ProxyWhitelistConfig()

    @classmethod
    def from_toml(cls, toml_path: str) -> InstanceConfig:
        """Parse sandbox.toml into a validated InstanceConfig."""
        with open(toml_path, "rb") as f:
            raw = tomllib.load(f)

        # Flatten nested TOML tables into the model's expected structure
        components_raw = raw.get("components", {})
        flat: dict[str, Any] = {
            "instance": raw.get("instance", {}),
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
    config: InstanceConfig,
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
        "user_project_root": config.instance.user_project_root,
        "custom_config_core": "/home/agent/.sandbox/custom",
        "custom_config_admin": "/home/human/.sandbox/custom",
        "tmux_resurrect_dir": "/home/human/.sandbox/tmux_resurrect",
        # Project
        "project_name": config.instance.name,
        "host_uid": config.instance.host_uid,
        "warmup_prompt": config.instance.warmup_prompt,
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
        "dns_image": IMAGE_REGISTRY["coredns"].pinned,
        "proxy_image": IMAGE_REGISTRY["squid"].pinned,
        "dnsdist_image": IMAGE_REGISTRY["dnsdist"].pinned,
        "busybox_image": IMAGE_REGISTRY["busybox_musl"].pinned,
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
    _copy_file(tooling_plane, ".docker/coredns/Dockerfile.coredns", instance_dir, "docker/coredns/Dockerfile.coredns")

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
        + [".docker/coredns/Dockerfile.coredns"]
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
