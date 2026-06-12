# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Hydration pipeline: Pydantic config parsing + Jinja2 template rendering.

Implements the PHASE 4 (HYDRATION) from the orchestrator design:
1. Parse sandbox.toml → InstanceConfig (Pydantic v2 model)
2. Build Jinja2 context from config + IPAM values + proxy credentials
3. Render templates from tooling plane → instance directory
"""

import base64
import json
import logging
import os
import secrets
import tomllib
from dataclasses import dataclass as _dataclass
from enum import StrEnum
from importlib.resources import files as _resource_files
from typing import Any

import jinja2
from pydantic import BaseModel, ConfigDict, Field, model_validator

from core import host_resources
from core.host_config import (
    HostSettings,
    in_container_gid_for_host_gid,
    resolve_daemon_owner_settings,
    workspace_bridge_gid,
)
from core.ipam import derive_static_ips, derive_subnets

logger = logging.getLogger(__name__)

# Admin's CPU intent — the single source of truth that replaces the former
# `cpus: "4.0"` literal in compose.yml. Admin is a static binary with no
# configurable runtime knobs, so this is a module constant rather than config.
ADMIN_CPUS = 4.0


def _clamp_cpus(value: float, host_cpus: int) -> tuple[float, bool]:
    """Clamp a ``--cpus`` request to the host's online CPU count.

    Docker rejects ``--cpus`` above the host's online CPU count at
    container-create, so a configured value larger than the host would abort
    ``sandbox start``. Returns ``(effective, was_clamped)`` where ``was_clamped``
    is ``True`` iff the value was reduced.
    """
    if value > host_cpus:
        return float(host_cpus), True
    return value, False


def _warn_cpu_clamped(label: str, configured: float, host_cpus: int, effective: float) -> None:
    """Emit one operator-facing warning that a CPU limit was reduced to fit the host.

    Graceful degrade: hydration proceeds with the clamped value, so this only
    informs — it never raises. Routes through the module logger (the same warn
    surface `render_templates` already uses for whitelist warnings).
    """
    logger.warning(
        "%s=%s exceeds the host's %d CPUs — clamped to %s",
        label,
        configured,
        host_cpus,
        effective,
    )


# ─── Image Digest Registry ──────────────────────────────────────────────────
#
# Centralized image pin registry. Each entry carries ref, tag, and digest
# as separate fields. Derived properties:
#   .pinned → "ref@sha256:..."  (for Dockerfile FROM / compose image:)
#   .tagged → "ref:tag"         (for human-readable display / rotation)
#
# Rotation procedure: scripts/rotate_pins.py
#   Resolves current tag digests / binary sha512s, verifies signatures, patches this file.
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
    "golang_alpine": ImagePin(
        ref="golang",
        tag="1.23-alpine",
        digest="sha256:383395b794dffa5b53012a212365d40c8e37109a626ca30d6151c8348d380b5f",
    ),
}

# ─── Binary Pin Registry ────────────────────────────────────────────────────
#
# Centralized binary pin registry. Each entry carries a URL template (with
# only the literal `$(arch)` placeholder substituted at install time), the
# pinned upstream version, the expected sha512, and the fetch method.
#


class FetchMethod(StrEnum):
    """How a pinned binary is fetched and verified at install time."""

    GVISOR_TARBALL = "gvisor_tarball"


class BinaryPin(BaseModel):
    """Immutable structured binary pin (URL template, version, sha512, method)."""

    model_config = ConfigDict(frozen=True)

    url_template: str
    version: str
    sha512: str
    fetch_method: FetchMethod


BINARY_REGISTRY: dict[str, BinaryPin] = {
    "runsc": BinaryPin(
        # runsc pin: gVisor release 20260511, x86_64 (operator-verified 2026-05-18); sha512 from
        # https://storage.googleapis.com/gvisor/releases/release/20260511/x86_64/runsc.sha512
        url_template="https://storage.googleapis.com/gvisor/releases/release/20260511/$(arch)/runsc",
        version="20260511",
        sha512="e227a71c95e794119f6648a44083df945392c6cd457f36abbc49c2b6e0b87c7f01b94e6bf4632f4cb22ee34fbec7a2c34ca03d30efa9c689db76a6215a6e44e1",
        fetch_method=FetchMethod.GVISOR_TARBALL,
    ),
}

# The reserved gVisor runtime name registered in the daemon's daemon.json
# (`runtimes["sandbox-ai-runsc"]`), namespaced so it never clobbers an operator's
# own `runsc` runtime. Single source of truth for that name: the compose `runtime`
# value rendered here, the registration in `core.setup.l6_daemon_json`, and the
# `cli-doctor` verification all derive from this constant and MUST agree — a
# mismatch makes Docker reject container-create with `unknown or invalid runtime
# name`. (F-024 single-sourced the doctor literal but left the compose render
# hardcoded to the bare `runsc`; that mismatch stayed latent until the F-053 CPU
# clamp let `start` reach container-create and expose it.)
RESERVED_RUNTIME_KEY = "sandbox-ai-runsc"

# ─── Pydantic Models ─────────────────────────────────────────────────────────


class BootstrapMode(StrEnum):
    """How a workspace tree was populated at init / workspace add time."""

    COPY = "copy"
    EMPTY = "empty"


class WorkspaceConfig(BaseModel):
    """A single ``[workspaces.<name>]`` entry in sandbox.toml."""

    bootstrap_mode: BootstrapMode
    source: str | None = None
    path: str

    @model_validator(mode="after")
    def _check_copy_requires_source(self) -> WorkspaceConfig:
        if self.bootstrap_mode == BootstrapMode.COPY and not self.source:
            raise ValueError("bootstrap_mode=copy requires a non-empty 'source' field")
        return self


class SandboxInstanceSection(BaseModel):
    """[instance] section of sandbox.toml."""

    name: str
    host_uid: str


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
    workspaces: dict[str, WorkspaceConfig] = Field(min_length=1)
    core: CoreConfig = CoreConfig()
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
            "workspaces": raw.get("workspaces", {}),
            "core": raw.get("core", {}),
            "runtimes": {k: v for k, v in raw.get("runtimes", {}).items() if k != "node"},
            "runtimes_node": raw.get("runtimes", {}).get("node", {}),
            "components": {k: v for k, v in components_raw.items() if isinstance(v, bool)},
            "components_db_postgres": components_raw.get("db_postgres", {}),
            "components_ingress": components_raw.get("ingress", {}),
            "proxy_whitelist": raw.get("proxy", {}).get("whitelist", {}),
        }
        # Surface a legacy [admin] table (if present) so the model validator can
        # reject it with an operator-friendly message. Per admin-reframe, the
        # [admin] section is no longer recognized.
        if "admin" in raw:
            flat["admin"] = raw["admin"]
        return cls.model_validate(flat)

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_admin_section(cls, data: Any) -> Any:
        """Reject sandbox.toml inputs that carry a legacy [admin] section.

        Per admin-reframe, admin is a static binary with no configurable runtime
        knobs; the [admin] table is no longer recognized. Surface a concrete
        operator-facing message rather than Pydantic's generic "extra fields"
        text.
        """
        if isinstance(data, dict) and "admin" in data:
            raise ValueError(
                "Legacy [admin] section detected in sandbox.toml. Per "
                "admin-reframe, the [admin] table is no longer recognized — "
                "admin is a static binary with no configurable runtime knobs. "
                "Remove the [admin] section from the file and re-run."
            )
        return data


# ─── Jinja2 Context Builder ──────────────────────────────────────────────────


def _read_optional_file(path: str) -> str:
    """Return file contents (trailing whitespace stripped) if exists, '' otherwise."""
    if os.path.exists(path):
        with open(path) as f:
            return f.read().rstrip()
    return ""


def _dnsdist_console_key() -> str:
    """A base64-encoded 32-byte key for dnsdist's localhost-only management console.

    dnsdist refuses every console connection unless ``setKey()`` is configured, and
    the ``dnsdist -e 'showServers()'`` healthcheck connects to that console — so
    without a key the healthcheck always fails and dnsdist never reports healthy
    (blocking ``core``, which ``depends_on`` it). The console binds 127.0.0.1 inside
    the container's isolated network namespace (console ACL 127.0.0.0/8), so this is
    not an external trust boundary; it satisfies dnsdist's "console requires a key"
    contract so the healthcheck can run. Generated per hydration — the server and the
    healthcheck client both read the same rendered ``dnsdist.conf``, so they always
    agree. Standard base64 (not ``token_urlsafe``'s URL-safe alphabet), which
    dnsdist's key decoder accepts.
    """
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def build_jinja_context(
    config: InstanceConfig,
    base_index: int,
    proxy_password: str,
    instance_dir: str,
    *,
    host: HostSettings | None = None,
) -> dict[str, Any]:
    """Build the complete Jinja2 template context from parsed config + IPAM.

    Context contract:
        All template variable defaults are resolved here. Templates use bare
        ``{{ var }}`` without Jinja2 ``| default()`` filters. The Jinja2
        ``StrictUndefined`` configuration enforces completeness — any variable
        referenced in a template but absent from this context raises
        ``UndefinedError`` at render time and during ``--dry-run`` validation.

    When ``host`` is provided, ``in_container_workspace_bridge_gid`` is added to
    the context. Errors from :func:`core.host_config.workspace_bridge_gid` (group
    missing or out-of-range) propagate so hydration aborts before rendering;
    callers should translate to a "run sandbox doctor" message.
    """
    isolated, core_proxy, dns, egress, ipc = derive_subnets(base_index)
    ips = derive_static_ips(base_index)

    # Clamp every --cpus request to the host's online CPU count so `sandbox
    # start` never dies at container-create on a sub-4-vCPU host (the modal
    # 2-vCPU cloud VM / CI runner). Detected once; shared by core + admin.
    host_cpus = host_resources.host_cpu_count()
    core_cpus, core_clamped = _clamp_cpus(config.core.cpus, host_cpus)
    admin_cpus, admin_clamped = _clamp_cpus(ADMIN_CPUS, host_cpus)
    if core_clamped:
        _warn_cpu_clamped("[core].cpus", config.core.cpus, host_cpus, core_cpus)
    if admin_clamped:
        _warn_cpu_clamped("admin cpus", ADMIN_CPUS, host_cpus, admin_cpus)

    extra: dict[str, Any] = {}
    if host is not None:
        bridge_gid = workspace_bridge_gid(host)
        extra["in_container_workspace_bridge_gid"] = in_container_gid_for_host_gid(
            bridge_gid, resolve_daemon_owner_settings(host)
        )

    return {
        # Network — 5-subnet topology
        "isolated_subnet": isolated,
        "core_proxy_subnet": core_proxy,
        "dns_subnet": dns,
        "egress_subnet": egress,
        "ipc_subnet": ipc,
        **ips,
        # Credentials
        "proxy_password": proxy_password,
        "proxy_url_core": f"http://proxyuser:{proxy_password}@proxy:3128",
        # dnsdist console key — enables the localhost-only console so its
        # healthcheck (`dnsdist -e`) can connect (see `_dnsdist_console_key`).
        "dnsdist_console_key": _dnsdist_console_key(),
        # Paths
        "instance_dir": instance_dir,
        "workspaces": [
            {
                "name": name,
                "path": ws.path,
                "bootstrap_mode": ws.bootstrap_mode.value,
                "source": ws.source,
            }
            for name, ws in sorted(config.workspaces.items())
        ],
        "custom_config_core": "/home/agent/.sandbox/custom",
        # Project
        "instance_name": config.instance.name,
        "host_uid": config.instance.host_uid,
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
        "core_cpus": str(core_cpus),
        "admin_cpus": str(admin_cpus),
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
        "runtime": RESERVED_RUNTIME_KEY,
        "dns_image": IMAGE_REGISTRY["coredns"].pinned,
        "proxy_image": IMAGE_REGISTRY["squid"].pinned,
        "dnsdist_image": IMAGE_REGISTRY["dnsdist"].pinned,
        "busybox_image": IMAGE_REGISTRY["busybox_musl"].pinned,
        "golang_alpine_image": IMAGE_REGISTRY["golang_alpine"].pinned,
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
        **extra,
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
]

# Static files copied as-is (no Jinja2 processing)
_STATIC_CONFIG_CORE: list[str] = []
_STATIC_CONFIG_PROXY = ["ERR_SANDBOX_403"]


def render_templates(
    context: dict[str, Any],
    instance_dir: str,
    *,
    db_postgres: bool,
    mcp_firecrawl: bool,
) -> None:
    """Render all templates from the packaged `templates` module into instance directory.

    Templates are loaded via Jinja2 PackageLoader anchored at the `templates`
    package; static files are read via importlib.resources.
    Skips: precious state (sandbox.toml, .sandbox.env, custom/, cache/, log/)
    """
    env = jinja2.Environment(
        loader=jinja2.PackageLoader("templates", package_path=""),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )

    # ── Standard registry-driven templates ─────────────────────────────────
    # _JINJA_RENDERED_DOCKER and _JINJA_RENDERED_CONFIG are the authoritative
    # file registries. All standard templates are iterated here.

    for src_rel, dst_rel in _JINJA_RENDERED_DOCKER:
        _render_file(env, f"docker/{src_rel}", instance_dir, dst_rel, context)

    for src_rel, dst_rel in _JINJA_RENDERED_CONFIG:
        _render_file(env, f"config/{src_rel}", instance_dir, dst_rel, context)

    # ── Distro-selected Dockerfiles (dynamic template path) ───────────────

    core_family = context["core_distro_family"]
    _render_file(
        env,
        f"docker/core/Dockerfile.core.{core_family}",
        instance_dir,
        "docker/core/Dockerfile.core",
        context,
    )

    # ── Static copies (no Jinja2 rendering) ───────────────────────────────

    _copy_file("docker/admin/Dockerfile.admin", instance_dir, "docker/admin/Dockerfile.admin")
    _copy_file("docker/admin/fwd.go", instance_dir, "docker/admin/fwd.go")
    _copy_file("docker/core/entrypoint.sh", instance_dir, "docker/core/entrypoint.sh")
    _copy_file("docker/coredns/Dockerfile.coredns", instance_dir, "docker/coredns/Dockerfile.coredns")

    # ── Feature-gated extras ──────────────────────────────────────────────

    if db_postgres:
        _render_file(env, "docker/extras/db-postgres.yml", instance_dir, "docker/extras/db-postgres.yml", context)
    if mcp_firecrawl:
        _render_file(env, "docker/extras/mcp-firecrawl.yml", instance_dir, "docker/extras/mcp-firecrawl.yml", context)
        _copy_file(
            "docker/extras/Dockerfile.mcp-firecrawl",
            instance_dir,
            "docker/extras/Dockerfile.mcp-firecrawl",
        )

    # ── Programmatic generation ───────────────────────────────────────────

    # Generate allowed_domains.txt from whitelist
    domains = context.get("proxy_whitelist_domains", [])
    domains_path = os.path.join(instance_dir, "config/proxy/allowed_domains.txt")
    write_restricted(domains_path, "".join(f"{d}\n" for d in domains), RESTRICTIVE_RO_MODE)

    # Generate read_only_domains.txt from whitelist (N4)
    read_only_domains = context.get("proxy_whitelist_read_only_domains", [])
    read_only_path = os.path.join(instance_dir, "config/proxy/read_only_domains.txt")
    write_restricted(read_only_path, "".join(f"{d}\n" for d in read_only_domains), RESTRICTIVE_RO_MODE)

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
        _copy_file(f"config/proxy/{filename}", instance_dir, f"config/proxy/{filename}")

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
    write_restricted(
        claude_json_path,
        json.dumps(claude_json_data, indent=2) + "\n",
        RESTRICTIVE_RO_MODE,
    )


RESTRICTIVE_RO_MODE = 0o640
"""Default mode for read-only config files written by hydration.

Group-readable so that helper-cp+chown can chgrp to a consumer's group
without exposing ``other::r--`` during the render → helper-chown intermediate
window (Decision 6 of acl-ownership-recipes)."""

RESTRICTIVE_SECRET_MODE = 0o600
"""Mode for secrets written by hydration / scaffolding (private keys, env files)."""


def write_restricted(path: str, content: str | bytes, mode: int) -> None:
    """Atomically write ``content`` to ``path`` at exactly ``mode`` bits, bypassing umask.

    Uses ``os.open(O_WRONLY|O_CREAT|O_TRUNC, mode)`` so the resulting file mode
    is the requested mode regardless of process umask. Existing files are
    truncated; the requested mode is re-applied via ``os.fchmod`` to defeat
    POSIX's "open with O_CREAT does not set mode if file exists" semantics.
    """
    payload = content.encode("utf-8") if isinstance(content, str) else content
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, mode)
    try:
        os.fchmod(fd, mode)
        os.write(fd, payload)
    finally:
        os.close(fd)


def _render_file(
    env: jinja2.Environment,
    template_rel: str,
    instance_dir: str,
    output_rel: str,
    context: dict[str, Any],
    *,
    mode: int = RESTRICTIVE_RO_MODE,
) -> None:
    """Render a single Jinja2 template to the instance directory at the given mode."""
    template = env.get_template(template_rel)
    rendered = template.render(context)
    output_path = os.path.join(instance_dir, output_rel)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    write_restricted(output_path, rendered, mode)


def _copy_file(
    src_rel: str,
    instance_dir: str,
    dst_rel: str,
    *,
    mode: int = RESTRICTIVE_RO_MODE,
) -> None:
    """Copy a static file from the templates package to the instance directory at the given mode."""
    resource = _resource_files("templates").joinpath(src_rel)
    dst = os.path.join(instance_dir, dst_rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    write_restricted(dst, resource.read_bytes(), mode)


# ─── Dry-Run Validation ─────────────────────────────────────────────────────


def validate_templates(
    context: dict[str, Any],
    *,
    db_postgres: bool,
    mcp_firecrawl: bool,
) -> tuple[int, list[str]]:
    """Render all Jinja2 templates to memory without writing.

    Returns (count_of_validated_templates, list_of_errors).
    Empty errors list means all templates are valid.
    """
    env = jinja2.Environment(
        loader=jinja2.PackageLoader("templates", package_path=""),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )

    errors: list[str] = []
    validated = 0

    # Build the full list of templates from the authoritative registries
    templates: list[str] = (
        [f"docker/{src_rel}" for src_rel, _ in _JINJA_RENDERED_DOCKER]
        + [f"config/{src_rel}" for src_rel, _ in _JINJA_RENDERED_CONFIG]
        + [
            f"docker/core/Dockerfile.core.{context.get('core_distro_family', 'wolfi')}",
        ]
    )

    if db_postgres:
        templates.append("docker/extras/db-postgres.yml")
    if mcp_firecrawl:
        templates.append("docker/extras/mcp-firecrawl.yml")

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

    # Verify static files exist in the templates package
    static_files = (
        [f"config/proxy/{f}" for f in _STATIC_CONFIG_PROXY]
        + [f"config/core/{f}" for f in _STATIC_CONFIG_CORE]
        + ["docker/core/entrypoint.sh"]
        + ["docker/admin/Dockerfile.admin"]
        + ["docker/admin/fwd.go"]
        + ["docker/coredns/Dockerfile.coredns"]
    )
    if mcp_firecrawl:
        static_files.append("docker/extras/Dockerfile.mcp-firecrawl")

    templates_root = _resource_files("templates")
    for rel_path in static_files:
        if templates_root.joinpath(rel_path).is_file():
            validated += 1
        else:
            errors.append(f"Static file missing: {rel_path}")

    return validated, errors
