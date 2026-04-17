import typer
from pathlib import Path
from rich.console import Console

from core.executor import Executor
from core.exceptions import SandboxExecutionError

app = typer.Typer()
console = Console()

def execute_safely(cmd: list[str]) -> None:
    """Wraps executor.run inside a strict SandboxExecutionError boundary."""
    executor = Executor()
    try:
        executor.run(cmd)
    except SandboxExecutionError as e:
        console.print("[FATAL]: Sandbox Orchestration Fault. Check logs for details.", style="red bold")
        log_dir = Path("./.sandbox/logs/orchestrator")
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "orchestrator.log"
        with open(log_file, "a") as f:
            f.write(f"{str(e)}\n\n")
        raise typer.Exit(code=1)

@app.command()
def start() -> None:
    """Start the sandbox."""
    console.print("Starting sandbox...")
    execute_safely(["echo", "Starting sandbox"])

@app.command()
def stop(clean: bool = False) -> None:
    """Stop the sandbox."""
    if clean:
        console.print("Unrecoverable docker compose down -v...")
        execute_safely(["docker", "compose", "down", "-v"])
    else:
        console.print("Recursion lockout logic setfacl -R -x u:sandbox...")
        execute_safely(["setfacl", "-R", "-x", "u:sandbox", "."])

if __name__ == "__main__":
    app()

