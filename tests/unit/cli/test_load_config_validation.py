"""Tests for `_load_config` — pins the `ValidationError` wrap at the CLI boundary.

Pins D4's contract:

- Pydantic ``ValidationError`` is reformatted to one
  ``Invalid <toml>: <field>: <reason>`` line per error and exits with
  code 1; the raw Pydantic traceback is suppressed (`from None`).
- Non-validation errors (``FileNotFoundError``, ``tomllib.TOMLDecodeError``)
  propagate intact with their full traceback — the narrow ``except``
  must not over-suppress.

Output is captured via Rich's ``capture()`` since the CLI prints through
the module-level ``console``, not stdout directly.
"""

import os
import tomllib
from pathlib import Path

import pytest
import typer
from cli.main import _load_config, console


def _flatten(text: str) -> str:
    """Collapse Rich's soft-wrap whitespace so substring asserts work regardless of console width."""
    return " ".join(text.split())


def _write_toml(instance_dir: Path, body: str) -> Path:
    instance_dir.mkdir(parents=True, exist_ok=True)
    toml = instance_dir / "sandbox.toml"
    toml.write_text(body)
    return toml


def _assert_no_pydantic_internals(text: str) -> None:
    """Pydantic internals MUST NOT leak through the formatted output."""
    assert "pydantic" not in text.lower()
    assert "errors.pydantic.dev" not in text
    assert "validation error for" not in text.lower()


class TestSingleError:
    def test_missing_required_field_emits_one_line(self, tmp_path: Path) -> None:
        toml_path = _write_toml(
            tmp_path / "inst",
            """
[instance]
name = "t"
host_uid = "1000"

[workspaces.main]
bootstrap_mode = "empty"
""",
        )
        with console.capture() as cap, pytest.raises(typer.Exit) as exc_info:
            _load_config(str(toml_path.parent))

        assert exc_info.value.exit_code == 1
        out = _flatten(cap.get())
        assert f"Invalid {toml_path}: workspaces.main.path:" in out
        assert out.count("Invalid ") == 1
        _assert_no_pydantic_internals(out)


class TestMultipleErrors:
    def test_multi_error_emits_one_line_each_in_order(self, tmp_path: Path) -> None:
        toml_path = _write_toml(
            tmp_path / "inst",
            """
[instance]
name = "t"
host_uid = "1000"

[workspaces.main]
bootstrap_mode = "empty"

[workspaces.scratch]
bootstrap_mode = "empty"
""",
        )
        with console.capture() as cap, pytest.raises(typer.Exit) as exc_info:
            _load_config(str(toml_path.parent))

        assert exc_info.value.exit_code == 1
        out = _flatten(cap.get())
        assert f"Invalid {toml_path}: workspaces.main.path:" in out
        assert f"Invalid {toml_path}: workspaces.scratch.path:" in out
        assert out.count("Invalid ") == 2
        # Errors appear in the order Pydantic reports them (main before scratch by dict order)
        assert out.index("workspaces.main.path") < out.index("workspaces.scratch.path")
        _assert_no_pydantic_internals(out)


class TestZeroWorkspaces:
    def test_empty_workspaces_section_formatted(self, tmp_path: Path) -> None:
        toml_path = _write_toml(
            tmp_path / "inst",
            """
[instance]
name = "t"
host_uid = "1000"

[workspaces]
""",
        )
        with console.capture() as cap, pytest.raises(typer.Exit) as exc_info:
            _load_config(str(toml_path.parent))

        assert exc_info.value.exit_code == 1
        out = _flatten(cap.get())
        assert f"Invalid {toml_path}: workspaces:" in out
        _assert_no_pydantic_internals(out)


class TestNonValidationExceptionsPropagate:
    """D4 narrow-`except` mitigation: non-validation errors propagate unmodified."""

    def test_missing_sandbox_toml_raises_filenotfounderror(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "no-such-instance"
        with pytest.raises((FileNotFoundError, OSError)) as exc_info:
            _load_config(str(nonexistent))
        # FileNotFoundError points at sandbox.toml, not swallowed
        assert "sandbox.toml" in str(exc_info.value) or exc_info.value.errno == os.errno.ENOENT  # type: ignore[attr-defined]

    def test_invalid_toml_syntax_raises_tomldecodeerror(self, tmp_path: Path) -> None:
        toml_path = _write_toml(
            tmp_path / "inst",
            "[instance\nname = broken",
        )
        with pytest.raises(tomllib.TOMLDecodeError):
            _load_config(str(toml_path.parent))
