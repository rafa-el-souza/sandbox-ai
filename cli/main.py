"""Sandbox CLI orchestrator — full lifecycle implementation.

Commands: init, start, stop, attach, destroy, doctor, status.
All Docker operations cross the dev/sandbox privilege boundary via machinectl.
"""

import fcntl
import json as _json
import os
import shutil
import subprocess
from dataclasses import dataclass

import typer
from core.crypto import generate_credential, hash_proxy_password, write_htpasswd
from core.doctor import (
    build_check_registry,
    detect_distro,
    render_results,
    run_check_subset,
    run_checks,
)
from core.exceptions import SandboxExecutionError
from core.executor import Executor
from core.hydration import (
    SandboxConfig,
    build_jinja_context,
    render_templates,
    validate_templates,
)
from core.ipam import IPAMExhaustedError, IPAMLedger, derive_static_ips, derive_subnets
from core.registry import InstanceRegistry, generate_instance_id
from core.scaffold import (
    _detect_git_config,
    apply_default_acls,
    create_env_file,
    create_instance_dirs,
    prompt_secrets,
    write_initialized_sentinel,
    write_sandbox_toml,
)
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

app = typer.Typer()
console = Console()


# ─── Data Types ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ContainerInfo:
    """Parsed container status from `docker compose ps --format json`."""

    name: str
    service: str
    state: str
    health: str | None
    status: str


# ─── Resolution helpers ─────────────────────────────────────────────────────


def _resolve_sandbox_ai_home() -> str:
    """Resolve SANDBOX_AI_HOME from the orchestrator source location."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_project_dir() -> str:
    """Resolve the project directory from CWD."""
    return os.path.abspath(os.getcwd())


def _resolve_instance(
    sandbox_ai_home: str, project_dir: str
) -> tuple[str | None, str | None]:
    """Look up instance from registry. Returns (instance_dir, instance_id) or (None, None)."""
    registry_path = os.path.join(sandbox_ai_home, ".state", "instances.json")
    registry = InstanceRegistry(registry_path)
    instance_id = registry.lookup(project_dir)
    if instance_id is None:
        return None, None
    instance_dir = os.path.join(sandbox_ai_home, "sandboxes", instance_id)
    return instance_dir, instance_id


def _load_config(instance_dir: str) -> SandboxConfig:
    """Parse sandbox.toml from instance directory."""
    toml_path = os.path.join(instance_dir, "sandbox.toml")
    return SandboxConfig.from_toml(toml_path)


# ─── Warm state check ───────────────────────────────────────────────────────

def _container_status(
    instance_dir: str,
    name: str,
    host_user: str,
    config: SandboxConfig,
) -> list[ContainerInfo]:
    """Query container statuses via `docker compose ps --format json`.

    Returns a list of ContainerInfo parsed from NDJSON output.
    Returns an empty list if the instance is stopped or the executor errors.
    """
    compose_file = os.path.join(instance_dir, "docker", "compose.yml")
    if not os.path.exists(compose_file):
        return []

    compose_files = _build_compose_files(instance_dir, config)
    files_str = " ".join(f"-f {f}" for f in compose_files)

    executor = Executor()
    try:
        result = executor.run(
            [
                "sudo", "machinectl", "shell", f"{host_user}@.host",
                "/bin/bash", "-c",
                f"COMPOSE_PROJECT_NAME={name} docker compose {files_str} ps --format json",
            ]
        )
    except SandboxExecutionError:
        return []

    containers: list[ContainerInfo] = []
    if result.stdout:
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = _json.loads(line)
                containers.append(ContainerInfo(
                    name=data.get("Name", ""),
                    service=data.get("Service", ""),
                    state=data.get("State", ""),
                    health=data.get("Health", None) or None,
                    status=data.get("Status", ""),
                ))
            except _json.JSONDecodeError:
                continue
    return containers


def _warm_check(
    instance_dir: str, name: str, host_user: str
) -> bool:
    """Check if containers are already running. Returns True if warm.

    Delegates to _container_status (D-3) — returns True if any containers exist.
    """
    compose_file = os.path.join(instance_dir, "docker", "compose.yml")
    if not os.path.exists(compose_file):
        return False

    config = _load_config(instance_dir)
    return bool(_container_status(instance_dir, name, host_user, config))


# ─── Locking ─────────────────────────────────────────────────────────────────


def _acquire_state_lock(instance_dir: str) -> int:
    """Acquire per-instance state.lock. Returns fd. Raises BlockingIOError on contention."""
    lock_path = os.path.join(instance_dir, "state.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        raise


def _release_lock(fd: int) -> None:
    """Release and close a lock fd."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


# ─── Phase implementations ──────────────────────────────────────────────────


def _phase_ipam(sandbox_ai_home: str, instance_id: str) -> int:
    """Phase 2: IPAM allocation. Returns base_index."""
    ledger_path = os.path.join(sandbox_ai_home, ".state", "ipam.json")
    ledger = IPAMLedger(ledger_path)
    return ledger.allocate(instance_id)


def _phase_credentials(instance_dir: str) -> str:
    """Phase 3: Generate proxy credentials. Returns proxy password."""
    password = generate_credential()
    hashed = hash_proxy_password(password)
    htpasswd_line = f"proxyuser:{hashed}"
    config_proxy_dir = os.path.join(instance_dir, "config", "proxy")
    write_htpasswd(config_proxy_dir, htpasswd_line)
    return password


def _phase_hydrate(
    config: SandboxConfig,
    base_index: int,
    proxy_password: str,
    sandbox_ai_home: str,
    instance_dir: str,
) -> None:
    """Phase 4: Pydantic + Jinja2 hydration pipeline."""
    context = build_jinja_context(config, base_index, proxy_password, instance_dir)
    render_templates(
        context,
        sandbox_ai_home,
        instance_dir,
        db_postgres=config.components_db_postgres.enabled,
        mcp_firecrawl=config.components.mcp_firecrawl,
    )


def _phase_acl_grant(instance_dir: str, host_user: str) -> None:
    """Phase 5: Grant sandbox user read access to docker/ and config/ (Pattern A)."""
    for subdir in ["docker/", "config/"]:
        target = os.path.join(instance_dir, subdir)
        subprocess.run(
            ["setfacl", "-R", "-m", f"u:{host_user}:rX", target],
            check=True,
        )


def _build_compose_files(instance_dir: str, config: SandboxConfig) -> list[str]:
    """Build the compose file list including component-conditional extras."""
    files = ["-f", os.path.join(instance_dir, "docker", "compose.yml")]
    if config.components_db_postgres.enabled:
        extras = os.path.join(instance_dir, "docker", "extras", "db-postgres.yml")
        files.extend(["-f", extras])
    if config.components.mcp_firecrawl:
        extras = os.path.join(instance_dir, "docker", "extras", "mcp-firecrawl.yml")
        files.extend(["-f", extras])
    return files


def _phase_compose_up(
    instance_dir: str, name: str, host_user: str, config: SandboxConfig
) -> None:
    """Phase 6: docker compose up -d --build --wait via machinectl."""
    compose_files = _build_compose_files(instance_dir, config)
    files_str = " ".join(compose_files)
    cmd = f"COMPOSE_PROJECT_NAME={name} docker compose {files_str} up -d --build --wait"
    executor = Executor()
    executor.run(
        [
            "sudo", "machinectl", "shell", f"{host_user}@.host",
            "/bin/bash", "-c", cmd,
        ]
    )


def _phase_handover(
    name: str, host_user: str, warmup_prompt: str = ""
) -> None:
    """Phase 7: PTY handover — docker exec -it via machinectl."""
    executor = Executor()
    exec_args = ["exec"]
    if warmup_prompt:
        exec_args.extend(["-e", f"SANDBOX_WARMUP_PROMPT={warmup_prompt}"])
    exec_args.extend(["-it", f"{name}-admin-1", "zsh"])

    executor.run(
        [
            "sudo", "machinectl", "shell", f"{host_user}@.host",
            "/usr/bin/docker", *exec_args,
        ],
        interactive=True,
    )


# ─── Compose down / ACL revoke ──────────────────────────────────────────────


def _compose_down(
    instance_dir: str,
    name: str,
    host_user: str,
    config: SandboxConfig,
    *,
    volumes: bool = False,
) -> None:
    """Run docker compose down via machinectl. Pass volumes=True for -v."""
    compose_files = _build_compose_files(instance_dir, config)
    files_str = " ".join(compose_files)
    v_flag = " -v" if volumes else ""
    cmd = f"COMPOSE_PROJECT_NAME={name} docker compose {files_str} down{v_flag}"
    executor = Executor()
    executor.run(
        [
            "sudo", "machinectl", "shell", f"{host_user}@.host",
            "/bin/bash", "-c", cmd,
        ]
    )


def _revoke_acls(instance_dir: str, host_user: str) -> None:
    """Revoke sandbox user's ACL entries on docker/ and config/ (Pattern A revoke)."""
    for subdir in ["docker/", "config/"]:
        target = os.path.join(instance_dir, subdir)
        subprocess.run(
            ["setfacl", "-R", "-x", f"u:{host_user}", target],
            check=True,
        )



# ─── Dry-Run Pipeline ───────────────────────────────────────────────────────


def _dry_run_pipeline(sandbox_ai_home: str, project_dir: str) -> None:
    """Simulate the full start pipeline without side effects.

    Validates config parsing, IPAM allocation, template rendering, secret
    completeness, and previews the subprocess commands that would execute.
    """
    console.print("\n[bold]Dry-run: sandbox start[/bold]\n")

    # ── Instance resolution ──────────────────────────────────────────────
    instance_dir, instance_id = _resolve_instance(sandbox_ai_home, project_dir)

    if instance_dir is None or instance_id is None:
        console.print(
            "No sandbox instance found. Run `sandbox init --user <user>` first.",
            style="red",
        )
        raise typer.Exit(code=1)

    console.print(f"  Instance: [green]{instance_id}[/green] (existing)")
    config = _load_config(instance_dir)
    host_user = config.project.host_unprivileged_user

    name = config.project.name

    # ── IPAM preview ─────────────────────────────────────────────────────
    ipam_path = os.path.join(sandbox_ai_home, ".state", "ipam.json")
    ledger = IPAMLedger(ipam_path)
    try:
        slot, is_existing = ledger.peek_next_slot(instance_id)
        isolated, proxy, egress = derive_subnets(slot)
        status = "existing" if is_existing else "preview — subject to concurrent changes"
        console.print(f"\n  IPAM slot: {slot} ({status})")
        console.print(f"    Isolated: {isolated}")
        console.print(f"    Proxy:    {proxy}")
        console.print(f"    Egress:   {egress}")
    except IPAMExhaustedError as e:
        console.print(f"\n  [red]IPAM: {e}[/red]")
        raise typer.Exit(code=1) from None

    # ── Template validation ──────────────────────────────────────────────
    context = build_jinja_context(config, slot, "DRY_RUN_PASSWORD", instance_dir)
    validated, errors = validate_templates(
        context,
        sandbox_ai_home,
        db_postgres=config.components_db_postgres.enabled,
        mcp_firecrawl=config.components.mcp_firecrawl,
    )

    if errors:
        console.print("\n  [red]Template validation failed:[/red]")
        for err in errors:
            console.print(f"    ✗ {err}", style="red")
        raise typer.Exit(code=1) from None
    else:
        console.print(f"\n  Templates: [green]{validated} validated[/green]")

    # ── Secret completeness check ────────────────────────────────────────
    env_path = os.path.join(instance_dir, ".sandbox.env")
    missing_secrets = _check_secrets(env_path, config)
    if missing_secrets:
        console.print("\n  [yellow]Missing/empty secrets:[/yellow]")
        for secret in missing_secrets:
            console.print(f"    ⊘ {secret}")

    # ── Command preview ──────────────────────────────────────────────────
    compose_files = _build_compose_files(instance_dir, config)
    files_str = " ".join(compose_files)

    console.print("\n  [bold]Commands that would execute:[/bold]")

    # ACL grants
    for subdir in ["docker/", "config/"]:
        target = os.path.join(instance_dir, subdir)
        console.print(f"    $ setfacl -R -m u:{host_user}:rX {target}", style="dim")

    # Compose up
    compose_cmd = (
        f"sudo machinectl shell {host_user}@.host -- /bin/bash -c "
        f"'COMPOSE_PROJECT_NAME={name} docker compose {files_str} up -d --build --wait'"
    )
    console.print(f"    $ {compose_cmd}", style="dim")

    # Handover
    handover_cmd = (
        f"sudo machinectl shell {host_user}@.host -- "
        f"/usr/bin/docker exec -it {name}-admin-1 zsh"
    )
    console.print(f"    $ {handover_cmd}", style="dim")

    console.print("\n  [green bold]Dry-run complete — all validations passed[/green bold]\n")


def _check_secrets(env_path: str, config: SandboxConfig) -> list[str]:
    """Check for missing or empty secrets in .sandbox.env."""
    required = ["CORE_ANTHROPIC_API_KEY"]
    if config.components_db_postgres.enabled:
        required.append("PG_PASSWORD")
    if config.components.mcp_firecrawl:
        required.append("FIRECRAWL_API_KEY")

    missing: list[str] = []
    env_vars: dict[str, str] = {}

    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, _, val = line.partition("=")
                    env_vars[key.strip()] = val.strip().strip('"')

    for key in required:
        if key not in env_vars or not env_vars[key]:
            missing.append(key)

    return missing


# ─── CLI Commands ────────────────────────────────────────────────────────────


@app.command()
def init(
    user: str = typer.Option(..., "--user", help="Unprivileged user for the sandbox"),
    git_user: str = typer.Option("", "--git-user", help="Git user.name (auto-detected if omitted)"),
    git_email: str = typer.Option("", "--git-email", help="Git user.email (auto-detected if omitted)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview scaffold without writing"),
) -> None:
    """Initialize a new sandbox instance for the current project."""
    sandbox_ai_home = _resolve_sandbox_ai_home()
    project_dir = _resolve_project_dir()

    # Re-init guard (D-6)
    instance_dir, instance_id = _resolve_instance(sandbox_ai_home, project_dir)
    if instance_dir is not None:
        console.print(
            "Instance already initialized for this directory. "
            "Run `sandbox destroy` first.",
            style="red",
        )
        raise typer.Exit(code=1)

    # Doctor pre-flight: Chain 2 (Filesystem) + Chain 3 (Repo Integrity)
    if not dry_run:
        distro = detect_distro()
        preflight_results = run_check_subset(
            ["Filesystem", "Repo Integrity"], user, distro
        )
        has_failures = any(r.status == "fail" for r in preflight_results)
        if has_failures:
            render_results(preflight_results, console=console)
            raise typer.Exit(code=1)

    # Git config auto-detection (D-8)
    if not git_user or not git_email:
        detected_name, detected_email = _detect_git_config()
        if not git_user:
            git_user = detected_name
        if not git_email:
            git_email = detected_email

    # Derive instance identity
    instance_id = generate_instance_id(project_dir)
    instance_dir = os.path.join(sandbox_ai_home, "sandboxes", instance_id)
    project_name = os.path.basename(project_dir)

    if dry_run:
        console.print("\n[bold]Dry-run: sandbox init[/bold]\n")
        console.print(f"  Instance ID: {instance_id}")
        console.print(f"  Directory: {instance_dir}")
        console.print(f"  User: {user}")
        console.print(f"  Git: {git_user} <{git_email}>")
        console.print(f"  Project: {project_name}")
        console.print("\n  [green bold]Dry-run complete — no files written[/green bold]\n")
        return

    # S1: Directory tree
    create_instance_dirs(instance_dir)

    # S2: sandbox.toml
    write_sandbox_toml(
        instance_dir, project_name, project_dir, user,
        git_user=git_user, git_email=git_email,
    )

    # S3: .sandbox.env
    config = _load_config(instance_dir)
    env_path = os.path.join(instance_dir, ".sandbox.env")
    create_env_file(
        env_path,
        db_postgres=config.components_db_postgres.enabled,
        mcp_firecrawl=config.components.mcp_firecrawl,
    )

    # S4: Default ACLs (Pattern B)
    dev_user = os.environ.get("USER", "dev")
    apply_default_acls(instance_dir, config.project.user_project_root, dev_user)

    # S5: Register
    registry_path = os.path.join(sandbox_ai_home, ".state", "instances.json")
    registry = InstanceRegistry(registry_path)
    registry.register(project_dir, instance_id)

    # S6: Secret prompting (non-TTY safe)
    required_secrets: list[tuple[str, str]] = [
        ("CORE_ANTHROPIC_API_KEY", "Anthropic API key"),
        ("CORE_GITHUB_TOKEN", "GitHub personal access token"),
    ]
    if config.components_db_postgres.enabled:
        required_secrets.append(("PG_PASSWORD", "PostgreSQL password"))
    if config.components.mcp_firecrawl:
        required_secrets.append(("FIRECRAWL_API_KEY", "Firecrawl API key"))
    prompt_secrets(env_path, required_secrets)

    # S7: Sentinel
    write_initialized_sentinel(instance_dir)

    console.print(f"Sandbox '{project_name}' initialized. Run `sandbox start` to launch.")



@app.command()
def start(
    dry_run: bool = typer.Option(False, "--dry-run", help="Simulate start without side effects"),
) -> None:
    """Start the sandbox."""
    sandbox_ai_home = _resolve_sandbox_ai_home()
    project_dir = _resolve_project_dir()

    if dry_run:
        _dry_run_pipeline(sandbox_ai_home, project_dir)
        return

    # Phase 0: Instance resolution
    instance_dir, instance_id = _resolve_instance(sandbox_ai_home, project_dir)

    if instance_dir is None or instance_id is None:
        console.print(
            "No sandbox instance found. Run `sandbox init --user <user>` first.",
            style="red",
        )
        raise typer.Exit(code=1)

    # Sentinel check: verify init completed
    sentinel_path = os.path.join(instance_dir, ".initialized")
    if not os.path.exists(sentinel_path):
        console.print(
            "Instance partially initialized. Run `sandbox destroy` then `sandbox init`.",
            style="red",
        )
        raise typer.Exit(code=1)

    config = _load_config(instance_dir)
    name = config.project.name
    host_user = config.project.host_unprivileged_user

    # Project name immutability check (sandbox-toml-schema spec)
    # instance_id format: <project_name>-<md5[:6]> — strip last 7 chars to recover original name
    original_name = instance_id[:-7]
    if name != original_name:
        console.print(
            "WARNING: project.name has changed since init. "
            "COMPOSE_PROJECT_NAME mismatch may orphan running containers.",
            style="yellow",
        )

    # Pre-flight: Secret completeness gate (D-7 — before lock acquisition)
    env_path = os.path.join(instance_dir, ".sandbox.env")
    missing_secrets = _check_secrets(env_path, config)
    if missing_secrets:
        console.print("Missing required secrets:", style="red")
        for secret in missing_secrets:
            console.print(f"  ⊘ {secret}", style="red")
        console.print(
            "\nPopulate secrets in .sandbox.env and retry.",
            style="red",
        )
        raise typer.Exit(code=1)

    # Pre-flight: Doctor Chain 1 — Privilege Boundary (before warm check)
    distro = detect_distro()
    preflight_results = run_check_subset(
        ["Privilege Boundary"], host_user, distro
    )
    has_preflight_failures = any(r.status == "fail" for r in preflight_results)
    if has_preflight_failures:
        render_results(preflight_results, console=console)
        raise typer.Exit(code=1)

    # Pre-lock warm check (D-52)
    if _warm_check(instance_dir, name, host_user):
        console.print(
            f"Sandbox '{name}' is already running. Use 'sandbox attach' to reconnect."
        )
        return

    # Phase 1: Locking
    lock_fd: int | None = None
    try:
        lock_fd = _acquire_state_lock(instance_dir)
    except BlockingIOError:
        console.print(
            "Another sandbox start is already in progress for this instance.",
            style="red bold",
        )
        raise typer.Exit(code=1) from None

    try:
        # Phase 2: IPAM
        base_index = _phase_ipam(sandbox_ai_home, instance_id)
        console.print("✓ IPAM — network allocation complete")

        # Phase 3: Credentials
        proxy_password = _phase_credentials(instance_dir)
        console.print("✓ Credentials — proxy auth configured")

        # Phase 4: Hydration
        _phase_hydrate(config, base_index, proxy_password, sandbox_ai_home, instance_dir)
        console.print("✓ Hydration — templates rendered")

        # Phase 5: ACL grants (Pattern A)
        _phase_acl_grant(instance_dir, host_user)
        console.print("✓ ACL — filesystem permissions granted")

        # Phase 6: Compose up (D-5 — spinner for long-running phase)
        with console.status("⟳ Compose — starting containers…"):
            _phase_compose_up(instance_dir, name, host_user, config)
        console.print("✓ Compose — containers healthy")

    except (IPAMExhaustedError, SandboxExecutionError) as e:
        console.print(f"[FATAL] {e}", style="red bold")
        if lock_fd is not None:
            _release_lock(lock_fd)
        raise typer.Exit(code=1) from None

    # Phase 7: Handover — release lock first
    if lock_fd is not None:
        _release_lock(lock_fd)

    console.print("→ Handing over to admin shell")
    _phase_handover(name, host_user, config.project.warmup_prompt)


@app.command()
def stop(clean: bool = False) -> None:
    """Stop the sandbox."""
    sandbox_ai_home = _resolve_sandbox_ai_home()
    project_dir = _resolve_project_dir()

    instance_dir, instance_id = _resolve_instance(sandbox_ai_home, project_dir)
    if instance_dir is None or instance_id is None:
        console.print("No sandbox instance found for this directory.", style="red")
        raise typer.Exit(code=1)

    config = _load_config(instance_dir)
    name = config.project.name
    host_user = config.project.host_unprivileged_user

    # Warm check
    if not _warm_check(instance_dir, name, host_user):
        console.print(f"Sandbox '{name}' is not running. Nothing to stop.")
        return

    # Compose down
    _compose_down(instance_dir, name, host_user, config, volumes=clean)

    # ACL revocation (Pattern A)
    _revoke_acls(instance_dir, host_user)

    if clean:
        console.print(
            f"Sandbox '{name}' stopped. Named volumes destroyed — data unrecoverable."
        )
    else:
        console.print(f"Sandbox '{name}' stopped. Named volumes preserved.")


@app.command()
def attach() -> None:
    """Attach to a running sandbox."""
    sandbox_ai_home = _resolve_sandbox_ai_home()
    project_dir = _resolve_project_dir()

    instance_dir, instance_id = _resolve_instance(sandbox_ai_home, project_dir)
    if instance_dir is None or instance_id is None:
        console.print("No sandbox instance found for this directory.", style="red")
        raise typer.Exit(code=1)

    config = _load_config(instance_dir)
    name = config.project.name
    host_user = config.project.host_unprivileged_user

    # Warm check — reject if cold
    if not _warm_check(instance_dir, name, host_user):
        console.print(
            f"Sandbox '{name}' is not running. Use 'sandbox start' to launch."
        )
        raise typer.Exit(code=1)

    # Direct handover — no hydration, no credentials, no locking
    _phase_handover(name, host_user)


@app.command()
def destroy(force: bool = False) -> None:
    """Permanently destroy a sandbox instance."""
    sandbox_ai_home = _resolve_sandbox_ai_home()
    project_dir = _resolve_project_dir()

    instance_dir, instance_id = _resolve_instance(sandbox_ai_home, project_dir)
    if instance_dir is None or instance_id is None:
        console.print("No sandbox instance found for this directory.", style="red")
        raise typer.Exit(code=1)

    # Prefix guard — before anything else
    sandboxes_prefix = os.path.join(sandbox_ai_home, "sandboxes")
    if not instance_dir.startswith(sandboxes_prefix):
        console.print(
            "[FATAL] Instance directory path fails prefix guard. Aborting.",
            style="red bold",
        )
        raise typer.Exit(code=1)

    config = _load_config(instance_dir)
    name = config.project.name
    host_user = config.project.host_unprivileged_user

    # Phase 0: Confirmation
    if not force:
        console.print(
            f"WARNING: This permanently deletes sandbox '{name}' and all its state."
        )
        console.print(
            f"         Your project at {config.project.user_project_root} is NOT affected."
        )
        typed_name = typer.prompt("Type the sandbox name to confirm")
        if typed_name != name:
            console.print("Aborted.")
            return

    # Phase 1: Locking
    lock_fd = _acquire_state_lock(instance_dir)

    try:
        # Phase 2: Container and volume teardown
        _compose_down(instance_dir, name, host_user, config, volumes=True)

        # Phase 3: ACL revocation
        _revoke_acls(instance_dir, host_user)

        # Phase 4: State cleanup — IPAM
        ipam_path = os.path.join(sandbox_ai_home, ".state", "ipam.json")
        ledger = IPAMLedger(ipam_path)
        ledger.release(instance_id)

        # Phase 4: State cleanup — Registry
        registry_path = os.path.join(sandbox_ai_home, ".state", "instances.json")
        registry = InstanceRegistry(registry_path)
        registry.remove(project_dir)

        # Phase 5: Directory removal
        shutil.rmtree(instance_dir)

    finally:
        # Close lock fd (file already deleted by rmtree)
        _release_lock(lock_fd)

    console.print(
        f"Sandbox '{name}' permanently destroyed. IPAM slot freed for reuse."
    )


@app.command()
def doctor(
    user: str = typer.Option(..., "--user", help="Unprivileged user to validate"),
) -> None:
    """Run host readiness diagnostics."""
    distro = detect_distro()
    checks = build_check_registry()
    results = run_checks(checks, user, distro)
    render_results(results, console=console)

    has_failures = any(r.status == "fail" for r in results)
    if has_failures:
        raise typer.Exit(code=1)


@app.command()
def status() -> None:
    """Show sandbox instance status and diagnostics."""
    sandbox_ai_home = _resolve_sandbox_ai_home()
    project_dir = _resolve_project_dir()

    # Instance resolution
    instance_dir, instance_id = _resolve_instance(sandbox_ai_home, project_dir)
    if instance_dir is None or instance_id is None:
        console.print("No sandbox instance found for this directory.", style="red")
        raise typer.Exit(code=1)

    config = _load_config(instance_dir)
    name = config.project.name
    host_user = config.project.host_unprivileged_user

    # Container status
    containers = _container_status(instance_dir, name, host_user, config)
    is_running = len(containers) > 0
    has_unhealthy = any(
        c.health is not None and c.health.lower() in ("unhealthy", "starting")
        for c in containers
    )

    # Determine state
    if is_running and has_unhealthy:
        state_label = "⚠ degraded"
        border_color = "yellow"
    elif is_running:
        state_label = "● running"
        border_color = "green"
    else:
        state_label = "○ stopped"
        border_color = "red"

    # Instance header panel
    header_lines = [
        f"[bold]Name:[/bold]    {name}",
        f"[bold]ID:[/bold]      {instance_id}",
        f"[bold]Path:[/bold]    {config.project.user_project_root}",
        f"[bold]User:[/bold]    {host_user}",
        f"[bold]State:[/bold]   {state_label}",
    ]
    panel = Panel(
        "\n".join(header_lines),
        title=f"Sandbox: {name}",
        border_style=border_color,
    )
    console.print(panel)

    # Container grid (running only)
    if is_running:
        # Derive static IPs for network column
        ipam_path = os.path.join(sandbox_ai_home, ".state", "ipam.json")
        ledger = IPAMLedger(ipam_path)
        ip_map: dict[str, str] = {}
        try:
            slot, _is_existing = ledger.peek_next_slot(instance_id)
            if _is_existing:
                ips = derive_static_ips(slot)
                # Map service names to IPs from IPAM
                ip_map = {
                    "core": ips.get("agent_isolated_ip", ""),
                    "admin": ips.get("admin_isolated_ip", ""),
                    "dns-sidecar": ips.get("dns_sidecar_ip", ""),
                    "db-postgres": ips.get("db_postgres_ip", ""),
                    "proxy": ips.get("proxy_ip", ""),
                }
        except IPAMExhaustedError:
            pass

        table = Table(title="Containers")
        table.add_column("Service", style="cyan")
        table.add_column("State")
        table.add_column("Health")
        table.add_column("Network")
        table.add_column("Status")

        for c in containers:
            health_display = c.health or "—"
            if c.health and c.health.lower() == "unhealthy":
                health_display = f"[red]{c.health}[/red]"
            elif c.health and c.health.lower() == "healthy":
                health_display = f"[green]{c.health}[/green]"
            network = ip_map.get(c.service, "")
            table.add_row(c.service, c.state, health_display, network, c.status)

        console.print(table)

    # IPAM display
    ipam_path = os.path.join(sandbox_ai_home, ".state", "ipam.json")
    ledger = IPAMLedger(ipam_path)
    try:
        slot, _is_existing = ledger.peek_next_slot(instance_id)
        if _is_existing:
            isolated, proxy, egress = derive_subnets(slot)
            console.print(f"\n[bold]IPAM[/bold] slot {slot}")
            console.print(f"  Isolated: {isolated}")
            console.print(f"  Proxy:    {proxy}")
            console.print(f"  Egress:   {egress}")
    except IPAMExhaustedError:
        pass

    # Config completeness warnings
    env_path = os.path.join(instance_dir, ".sandbox.env")
    if os.path.exists(env_path):
        missing = _check_secrets(env_path, config)
        if missing:
            console.print("\n[yellow bold]Warnings[/yellow bold]")
            for secret in missing:
                console.print(f"  ⊘ Missing secret: {secret}", style="yellow")


if __name__ == "__main__":
    app()
