"""Tests for project-wide test conventions — enforces fixture/marker/suppression rules.

Three structural guarantees, all AST-based:

1. **No runtime imports of helper functions from a `conftest.py`.** The
   pytest-canonical pattern is fixtures injected via parameters, not
   helpers imported across modules. Type-only imports under
   ``TYPE_CHECKING`` are exempt — those are type aliases, not behavior.
2. **No suppression directives.** ``# noqa``, ``# type: ignore``,
   ``# pragma: no cover`` are forbidden across `src/` and `tests/` per
   the global rule. Restructure code or remove unreachable branches
   instead of silencing the linter.
3. **Every custom `@pytest.mark.X` is registered in `pyproject.toml`.**
   Catches typo'd or undeclared markers. Pytest builtins
   (``parametrize``, ``usefixtures``, ``skip``, ``skipif``, ``xfail``,
   ``filterwarnings``) are exempt.
"""

from __future__ import annotations

import ast
import re
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _REPO_ROOT / "src"
_TESTS_ROOT = _REPO_ROOT / "tests"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

# Pytest builtin marks — never need pyproject registration.
_PYTEST_BUILTIN_MARKS: frozenset[str] = frozenset(
    {"parametrize", "usefixtures", "skip", "skipif", "xfail", "filterwarnings"}
)

# Files that may legitimately reference suppression-directive substrings —
# meta-tests like this one quote them as forbidden patterns.
_SUPPRESSION_CHECK_EXEMPT: frozenset[str] = frozenset(
    {"tests/unit/test_conventions.py"}
)

# Specific (file, lineno-pattern) suppressions grandfathered from this gate.
# Each entry is a tech-debt anchor: it documents what is allowed today and
# is meant to be deleted once the underlying structural issue is fixed.
# Add a corresponding entry in `openspec/deferred.md` when adding here.
_GRANDFATHERED_SUPPRESSIONS: frozenset[tuple[str, int]] = frozenset(
    {
        # Late `import cli.main` deliberately captures `_seed_host_config_if_absent`
        # before an autouse patch replaces it. Removing the late import requires
        # redesigning the capture mechanism — non-trivial. Tracked in
        # openspec/deferred.md.
        ("tests/unit/cli/test_cli.py", 95),
    }
)


def _python_files(root: Path) -> Iterator[Path]:
    yield from (p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


# ── 1. No runtime imports from conftest ──────────────────────────────────────


def _conftest_runtime_imports(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, module)`` for every ``from tests...conftest import …``
    that is NOT under a ``TYPE_CHECKING`` block."""
    offenders: list[tuple[int, str]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.in_type_checking = False

        def visit_If(self, node: ast.If) -> None:
            test = node.test
            is_tc = (
                (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING")
                or (
                    isinstance(test, ast.Attribute)
                    and test.attr == "TYPE_CHECKING"
                )
            )
            if is_tc:
                prev = self.in_type_checking
                self.in_type_checking = True
                for stmt in node.body:
                    self.visit(stmt)
                self.in_type_checking = prev
                for stmt in node.orelse:
                    self.visit(stmt)
                return
            self.generic_visit(node)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if self.in_type_checking:
                return
            mod = node.module or ""
            if mod.startswith("tests.") and mod.endswith(".conftest"):
                offenders.append((node.lineno, mod))

    Visitor().visit(tree)
    return offenders


def test_no_runtime_imports_from_conftest() -> None:
    offenders: list[tuple[Path, int, str]] = []
    for src in _python_files(_TESTS_ROOT):
        tree = ast.parse(src.read_text(), filename=str(src))
        for lineno, mod in _conftest_runtime_imports(tree):
            offenders.append((src, lineno, mod))

    if offenders:
        details = "\n".join(
            f"  {p.relative_to(_REPO_ROOT)}:{lineno}: from {mod} import …"
            for p, lineno, mod in offenders
        )
        pytest.fail(
            f"{len(offenders)} runtime import(s) from a conftest.\n{details}\n\n"
            "Fix: convert the helper into a pytest fixture in conftest.py and "
            "inject it via a test parameter. If you need a TYPE-ONLY import "
            "(e.g., a Protocol or NamedTuple defined in conftest), wrap the "
            "import in `if TYPE_CHECKING:`."
        )


# ── 2. No suppression directives ─────────────────────────────────────────────


_SUPPRESSION_PATTERNS = (
    re.compile(r"#\s*noqa(?:\b|:)"),
    re.compile(r"#\s*type:\s*ignore"),
    re.compile(r"#\s*pragma:\s*no\s*cover"),
)


def test_no_suppression_directives() -> None:
    offenders: list[tuple[Path, int, str]] = []
    for root in (_SRC_ROOT, _TESTS_ROOT):
        for src in _python_files(root):
            rel = src.relative_to(_REPO_ROOT).as_posix()
            if rel in _SUPPRESSION_CHECK_EXEMPT:
                continue
            for lineno, line in enumerate(src.read_text().splitlines(), start=1):
                for pattern in _SUPPRESSION_PATTERNS:
                    if pattern.search(line):
                        if (rel, lineno) in _GRANDFATHERED_SUPPRESSIONS:
                            break
                        offenders.append((src, lineno, line.strip()))
                        break

    if offenders:
        details = "\n".join(
            f"  {p.relative_to(_REPO_ROOT)}:{lineno}: {line}"
            for p, lineno, line in offenders
        )
        pytest.fail(
            f"{len(offenders)} suppression directive(s) found.\n{details}\n\n"
            "Fix: restructure the code or remove unreachable branches. Do NOT "
            "silence ruff/mypy/coverage; understand why they fired."
        )


# ── 3. Every custom marker is registered in pyproject.toml ───────────────────


def _registered_markers() -> set[str]:
    """Parse pyproject.toml's ``[tool.pytest.ini_options].markers`` list."""
    data = tomllib.loads(_PYPROJECT.read_text())
    raw_markers = data["tool"]["pytest"]["ini_options"]["markers"]
    out: set[str] = set()
    for entry in raw_markers:
        # Each entry looks like "name: description". Take everything up to ":".
        name = entry.split(":", 1)[0].strip()
        out.add(name)
    return out


def _custom_marks_in_file(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, mark_name)`` for every ``@pytest.mark.X`` in the AST,
    skipping pytest builtins."""
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        # Match `pytest.mark.<name>` — possibly chained: pytest.mark.foo(...)
        # AST shape: Attribute(value=Attribute(value=Name('pytest'), attr='mark'), attr='<name>')
        value = node.value
        if (
            isinstance(value, ast.Attribute)
            and value.attr == "mark"
            and isinstance(value.value, ast.Name)
            and value.value.id == "pytest"
        ):
            name = node.attr
            if name not in _PYTEST_BUILTIN_MARKS:
                out.append((node.lineno, name))
    return out


def _systemd_run_string_literals(tree: ast.AST) -> list[tuple[int, ast.AST | None]]:
    """Return ``(lineno, parent_function_or_None)`` for every string literal in
    ``tree`` whose value is exactly ``systemd-run``.

    Walks the module AST and records the enclosing ``FunctionDef`` (or
    ``AsyncFunctionDef``) for each match, so the caller can assert the literal
    only appears inside a specific function. Matching by AST string-literal
    (not raw text) means comments like ``# avoid systemd-run`` or unrelated
    identifiers would not produce false positives.
    """
    out: list[tuple[int, ast.AST | None]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.func_stack: list[ast.AST] = []

        def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            self.func_stack.append(node)
            self.generic_visit(node)
            self.func_stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_func(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_func(node)

        def visit_Constant(self, node: ast.Constant) -> None:
            if isinstance(node.value, str) and "systemd-run" in node.value:
                enclosing = self.func_stack[-1] if self.func_stack else None
                out.append((node.lineno, enclosing))

    Visitor().visit(tree)
    return out


def test_no_raw_systemd_run_outside_pipe_cmd() -> None:
    """The literal ``systemd-run`` may only appear inside ``host_config.pipe_cmd``.

    Mirrors the "Never hardcode `sudo machinectl`" gate but for the byte-pipe
    primitive: every ``systemd-run`` invocation must go through
    :func:`core.host_config.pipe_cmd` so call sites stay swappable and the
    PAM-skip trade-off is documented in exactly one place. Docstring mentions
    of ``systemd-run`` inside ``pipe_cmd`` are allowed — that IS the
    canonical location.
    """
    pipe_cmd_path = _SRC_ROOT / "core" / "host_config.py"
    offenders: list[tuple[Path, int, str]] = []

    for src in _python_files(_SRC_ROOT):
        tree = ast.parse(src.read_text(), filename=str(src))
        matches = _systemd_run_string_literals(tree)
        if not matches:
            continue
        if src != pipe_cmd_path:
            for lineno, _fn in matches:
                offenders.append((src, lineno, "literal outside core.host_config"))
            continue
        # Inside host_config.py: every match must be enclosed by `pipe_cmd`.
        for lineno, fn in matches:
            if not (isinstance(fn, ast.FunctionDef) and fn.name == "pipe_cmd"):
                where = (
                    f"function {fn.name!r}"
                    if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
                    else "module scope"
                )
                offenders.append((src, lineno, f"literal in {where}, expected pipe_cmd"))

    if offenders:
        details = "\n".join(
            f"  {p.relative_to(_REPO_ROOT)}:{lineno}: {note}"
            for p, lineno, note in offenders
        )
        pytest.fail(
            f"{len(offenders)} raw 'systemd-run' literal(s) outside pipe_cmd.\n{details}\n\n"
            "Fix: route the call through core.host_config.pipe_cmd(user). The "
            "byte-pipe primitive is centralized so the PAM-skip trade-off is "
            "documented in exactly one place; ad-hoc 'systemd-run' invocations "
            "bypass that contract."
        )


def test_every_custom_marker_is_registered() -> None:
    registered = _registered_markers()
    offenders: list[tuple[Path, int, str]] = []
    for src in _python_files(_TESTS_ROOT):
        tree = ast.parse(src.read_text(), filename=str(src))
        for lineno, name in _custom_marks_in_file(tree):
            if name not in registered:
                offenders.append((src, lineno, name))

    if offenders:
        details = "\n".join(
            f"  {p.relative_to(_REPO_ROOT)}:{lineno}: @pytest.mark.{name}"
            for p, lineno, name in offenders
        )
        pytest.fail(
            f"{len(offenders)} unregistered custom marker(s).\n{details}\n\n"
            f"Registered markers: {sorted(registered)}.\n"
            "Fix: add the marker (with a one-line description) to "
            "pyproject.toml's [tool.pytest.ini_options].markers list."
        )
