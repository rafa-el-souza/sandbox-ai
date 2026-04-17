from unittest.mock import patch
from typer.testing import CliRunner
from cli.main import app

def test_cli_start():
    runner = CliRunner()
    with patch("cli.main.Executor") as mock_executor_cls:
        mock_executor = mock_executor_cls.return_value
        result = runner.invoke(app, ["start"])
        assert result.exit_code == 0
        mock_executor.run.assert_called_once_with(["echo", "Starting sandbox"])

def test_cli_stop_default():
    runner = CliRunner()
    with patch("cli.main.Executor") as mock_executor_cls:
        mock_executor = mock_executor_cls.return_value
        result = runner.invoke(app, ["stop"])
        assert result.exit_code == 0
        mock_executor.run.assert_called_once_with(["setfacl", "-R", "-x", "u:sandbox", "."])

def test_cli_stop_clean():
    runner = CliRunner()
    with patch("cli.main.Executor") as mock_executor_cls:
        mock_executor = mock_executor_cls.return_value
        result = runner.invoke(app, ["stop", "--clean"])
        assert result.exit_code == 0
        mock_executor.run.assert_called_once_with(["docker", "compose", "down", "-v"])

