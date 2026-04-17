from typer.testing import CliRunner
from cli.main import app

runner = CliRunner()

def test_cli_start():
    result = runner.invoke(app, ["start"])
    # Not checking full execution logic since we just mocked Typer.
    # Just verify the command ingestion is valid.
    assert result.exit_code == 0
    assert "Starting sandbox" in result.stdout

def test_cli_stop_default():
    result = runner.invoke(app, ["stop"])
    assert result.exit_code == 0
    assert "setfacl -R -x u:sandbox" in result.stdout

def test_cli_stop_clean():
    result = runner.invoke(app, ["stop", "--clean"])
    assert result.exit_code == 0
    assert "docker compose down -v" in result.stdout
