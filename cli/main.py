import typer
from rich.console import Console

app = typer.Typer()
console = Console()

@app.command()
def start() -> None:
    """Start the sandbox."""
    console.print("Starting sandbox...")

@app.command()
def stop(clean: bool = False) -> None:
    """Stop the sandbox."""
    if clean:
        console.print("Unrecoverable docker compose down -v...")
    else:
        console.print("Recursion lockout logic setfacl -R -x u:sandbox...")

if __name__ == "__main__":
    app()
