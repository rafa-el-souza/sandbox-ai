"""Sandbox CLI orchestrator — full lifecycle implementation.

Commands: start, stop, attach, destroy, doctor.
All Docker operations cross the dev/sandbox privilege boundary via machinectl.
"""

import fcntl
import os
import shutil
import subprocess

import typer
from core.crypto import generate_proxy_password, hash_proxy_password, write_htpasswd
from core.doctor import (
    build_check_registry,
    detect_distro,
    render_results,
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
from core.ipam import IPAMExhaustedError, IPAMLedger, derive_subnets
from core.registry import InstanceRegistry, generate_instance_id
from core.scaffold import (
    apply_default_acls,
    create_env_file,
    create_instance_dirs,
    prompt_secrets,
    write_initialized_sentinel,
    write_sandbox_toml,
)
from rich.console import Console

app = typer.Typer()
console = Console()


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


def _warm_check(
    instance_dir: str, name: str, host_user: str
) -> bool:
    """Check if containers are already running. Returns True if warm."""
    compose_file = os.path.join(instance_dir, "docker", "compose.yml")
    if not os.path.exists(compose_file):
        return False
    executor = Executor()
    try:
        result = executor.run(
            [
                "sudo", "machinectl", "shell", f"{host_user}@.host",
                "/bin/bash", "-c",
                f'COMPOSE_PROJECT_NAME={name} docker compose -f {compose_file} ps -q',
            ]
        )
        return bool(result.stdout and result.stdout.strip())
    except SandboxExecutionError:
        return False


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
    password = generate_proxy_password()
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


# ─── Scaffolding ─────────────────────────────────────────────────────────────


def _scaffold_instance(
    sandbox_ai_home: str, project_dir: str
) -> tuple[str, str]:
    """Full scaffold sub-sequence for a new instance. Returns (instance_dir, instance_id)."""
    instance_id = generate_instance_id(project_dir)
    instance_dir = os.path.join(sandbox_ai_home, "sandboxes", instance_id)
    project_name = os.path.basename(project_dir)

    # S1: Directory tree
    create_instance_dirs(instance_dir)

    # S2: sandbox.toml
    write_sandbox_toml(instance_dir, project_name, project_dir, "sandbox")

    # S3: .sandbox.env
    config = _load_config(instance_dir)
    env_path = os.path.join(instance_dir, ".sandbox.env")
    create_env_file(
        env_path,
        db_postgres=config.components_db_postgres.enabled,
        mcp_firecrawl=config.components.mcp_firecrawl,
    )

    # S4: Default ACLs (Pattern B — never revoked)
    dev_user = os.environ.get("USER", "dev")
    apply_default_acls(instance_dir, config.project.user_project_root, dev_user)

    # S5: Register
    registry_path = os.path.join(sandbox_ai_home, ".state", "instances.json")
    registry = InstanceRegistry(registry_path)
    registry.register(project_dir, instance_id)

    # S6: Secret prompting
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

    return instance_dir, instance_id


# ─── Dry-Run Pipeline ───────────────────────────────────────────────────────


def _dry_run_pipeline(sandbox_ai_home: str, project_dir: str) -> None:
    """Simulate the full start pipeline without side effects.

    Validates config parsing, IPAM allocation, template rendering, secret
    completeness, and previews the subprocess commands that would execute.
    """
    console.print("\n[bold]Dry-run: sandbox start[/bold]\n")

    # ── Instance resolution ──────────────────────────────────────────────
    instance_dir, instance_id = _resolve_instance(sandbox_ai_home, project_dir)
    is_new = instance_dir is None or instance_id is None

    if is_new:
        console.print("  Instance: [yellow]NEW[/yellow] (would be scaffolded)")
        # Build a default config for simulation
        project_name = os.path.basename(project_dir)
        host_user = "sandbox"
        host_uid = str(os.getuid())
        config = SandboxConfig.model_validate({
            "project": {
                "name": project_name,
                "user_project_root": project_dir,
                "host_unprivileged_user": host_user,
                "host_uid": host_uid,
            }
        })
        instance_id = f"{project_name}-dry000"
        instance_dir = os.path.join(sandbox_ai_home, "sandboxes", instance_id)
        console.print(f"  Project: {project_name}")
        console.print(f"  User: {host_user}")
        console.print(f"  Directory: {instance_dir} [dim](simulated)[/dim]")
    else:
        assert instance_dir is not None
        assert instance_id is not None
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
    if not is_new:
        env_path = os.path.join(instance_dir, ".sandbox.env")
        missing_secrets = _check_secrets(env_path, config)
        if missing_secrets:
            console.print("\n  [yellow]Missing/empty secrets:[/yellow]")
            for secret in missing_secrets:
                console.print(f"    ⊘ {secret}")
    else:
        console.print("\n  Secrets: [dim]would be prompted during scaffold[/dim]")

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
        # New instance — scaffold
        instance_dir, instance_id = _scaffold_instance(sandbox_ai_home, project_dir)

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

        # Phase 3: Credentials
        proxy_password = _phase_credentials(instance_dir)

        # Phase 4: Hydration
        _phase_hydrate(config, base_index, proxy_password, sandbox_ai_home, instance_dir)

        # Phase 5: ACL grants (Pattern A)
        _phase_acl_grant(instance_dir, host_user)

        # Phase 6: Compose up
        _phase_compose_up(instance_dir, name, host_user, config)

    except (IPAMExhaustedError, SandboxExecutionError) as e:
        console.print(f"[FATAL] {e}", style="red bold")
        if lock_fd is not None:
            _release_lock(lock_fd)
        raise typer.Exit(code=1) from None

    # Phase 7: Handover — release lock first
    if lock_fd is not None:
        _release_lock(lock_fd)

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


if __name__ == "__main__":
    app()
