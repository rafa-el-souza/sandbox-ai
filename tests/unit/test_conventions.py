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
_GRANDFATHERED_SUPPRESSIONS: frozenset[tuple[str, int]] = frozenset()


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


# ── 4. machinectl_cmd callers restricted to the dispatch allowlist ──────────


# The `host-config` capability documents exactly three allowlist categories for
# direct `machinectl_cmd` usage (runtime-dispatcher design D5):
#   1. src/core/host_config.py — defines the symbol.
#   2. src/core/dispatch.py    — the sanctioned orchestration module.
#   3. src/core/setup/*.py     — the setup-phase package (forward reference;
#      sister change `sandbox-setup`). Setup phases cross the boundary as root
#      before the dispatcher is installed, so cannot route through
#      `core.dispatch`. Enumerated AT TEST TIME via a bounded glob — NOT a free
#      `src/**` allow. The dir does not exist until `sandbox-setup` lands, so
#      the glob legitimately matches nothing today; that is correct, not a bug.
# Broadening beyond these three is a spec change, not a silent edit.
_MACHINECTL_CMD_LITERAL_ALLOWLIST: frozenset[str] = frozenset(
    {
        "src/core/host_config.py",
        "src/core/dispatch.py",
    }
)
_MACHINECTL_CMD_SETUP_GLOB_DIR = _SRC_ROOT / "core" / "setup"


def _machinectl_cmd_allowlist() -> frozenset[str]:
    """Return the repo-relative POSIX paths permitted to import/reference
    ``machinectl_cmd`` directly: the two literal modules plus whatever the
    bounded ``src/core/setup/*.py`` glob enumerates at test time (possibly
    empty — that is tolerated, not an error)."""
    glob_matches = {
        p.relative_to(_REPO_ROOT).as_posix()
        for p in _MACHINECTL_CMD_SETUP_GLOB_DIR.glob("*.py")
    }
    return _MACHINECTL_CMD_LITERAL_ALLOWLIST | frozenset(glob_matches)


def _machinectl_cmd_references(tree: ast.AST) -> list[int]:
    """Return the line numbers at which ``machinectl_cmd`` is imported or
    referenced as a name/attribute in ``tree``.

    AST-only by construction: only ``ast.ImportFrom`` (``from … import
    machinectl_cmd``), ``ast.Import`` (``import … machinectl_cmd`` / dotted),
    and ``ast.Name``/``ast.Attribute`` with the matching identifier are
    inspected. String literals, comments and docstrings are never AST name
    nodes, so deliberate documentation mentions (e.g.
    ``src/core/doctor/registry.py``'s docstring and
    ``src/core/doctor/checks/supply_chain.py``'s comments) are NOT flagged — a
    grep-based check would wrongly fail on those and is unacceptable here.
    """
    linenos: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "machinectl_cmd":
                    linenos.add(node.lineno)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                # `import a.b.machinectl_cmd` — last dotted component match.
                if alias.name.split(".")[-1] == "machinectl_cmd":
                    linenos.add(node.lineno)
        elif (isinstance(node, ast.Name) and node.id == "machinectl_cmd") or (
            isinstance(node, ast.Attribute) and node.attr == "machinectl_cmd"
        ):
            linenos.add(node.lineno)
    return sorted(linenos)


def _machinectl_cmd_violations(
    files: Iterator[Path], allowlist: frozenset[str]
) -> list[tuple[Path, list[int]]]:
    """Scan ``files``; return ``(path, linenos)`` for every file that imports
    or references ``machinectl_cmd`` while NOT being in ``allowlist``.

    Reusable detector seam (anti-hack rule 5): the file iterable and allowlist
    are parameters, not module state, so the deliberate-violation regression
    (:func:`test_machinectl_cmd_deliberate_violation_is_detected`) can drive the
    exact same logic against an arbitrary out-of-allowlist file without
    duplicating the AST predicate.
    """
    offenders: list[tuple[Path, list[int]]] = []
    for src in files:
        # A file outside the repo root can never be in the (repo-relative)
        # allowlist; treat its absolute path as the key so the deliberate-
        # violation regression (a tmp_path file) is correctly flagged.
        rel = (
            src.relative_to(_REPO_ROOT).as_posix()
            if src.is_relative_to(_REPO_ROOT)
            else src.as_posix()
        )
        if rel in allowlist:
            continue
        tree = ast.parse(src.read_text(), filename=str(src))
        linenos = _machinectl_cmd_references(tree)
        if linenos:
            offenders.append((src, linenos))
    return offenders


def test_machinectl_cmd_callers_restricted() -> None:
    """Direct ``machinectl_cmd`` usage is forbidden outside the dispatch
    allowlist (runtime-dispatcher design D5).

    Every orchestrator-to-sandbox crossing must route through
    :mod:`core.dispatch`; a hand-rolled ``machinectl_cmd(...)`` would silently
    bypass the dispatcher's typed narrowing. The allowlist is exactly the three
    `host-config`-documented categories (two literal modules + the bounded
    ``src/core/setup/*.py`` glob); broadening it is a spec change, not a silent
    edit. ``sandbox-setup`` does NOT amend this list — its modules simply match
    the pre-existing setup glob.
    """
    allowlist = _machinectl_cmd_allowlist()
    offenders = _machinectl_cmd_violations(_python_files(_SRC_ROOT), allowlist)

    if offenders:
        details = "\n".join(
            f"  {p.relative_to(_REPO_ROOT)}: line(s) {linenos}"
            for p, linenos in offenders
        )
        pytest.fail(
            f"{len(offenders)} file(s) import/reference machinectl_cmd outside "
            f"the dispatch allowlist.\n{details}\n\n"
            f"Allowlist: {sorted(allowlist)}\n"
            "Fix: route the crossing through core.dispatch (add an op to the "
            "typed Op surface). Do NOT broaden the allowlist — that is a spec "
            "change (runtime-dispatcher design D5), not a silent edit."
        )


def test_machinectl_cmd_deliberate_violation_is_detected(
    tmp_path: Path,
) -> None:
    """A file that imports ``machinectl_cmd`` from OUTSIDE the allowlist must
    be reported by the shared detector, with its path in the failure surface.

    Drives the SAME :func:`_machinectl_cmd_violations` detector used by
    :func:`test_machinectl_cmd_callers_restricted` (no duplicated AST logic) so
    the convention check is provably catching the bug class, not merely the
    absence of the symptom.
    """
    rogue = tmp_path / "rogue_caller.py"
    rogue.write_text("from core.host_config import machinectl_cmd\n")

    # The temp file is well outside src/; an allowlist of just the two literal
    # modules is sufficient — the detector keys off repo-relative path
    # membership, and tmp_path is never in it.
    offenders = _machinectl_cmd_violations(
        iter([rogue]), _MACHINECTL_CMD_LITERAL_ALLOWLIST
    )

    assert offenders, "deliberate machinectl_cmd violation was not detected"
    offending_paths = {p for p, _ in offenders}
    assert rogue in offending_paths, (
        f"detector did not report the rogue file {rogue} "
        f"(reported: {sorted(str(p) for p in offending_paths)})"
    )
    # The path is in the surfaced failure detail the gate would print.
    detail = "\n".join(
        f"  {p}: line(s) {linenos}" for p, linenos in offenders
    )
    assert str(rogue) in detail


# ── D7 regression guard: runtime owner resolved via resolve_daemon_owner ──────

# host-config "Daemon Owner Resolution" (design D7): the runtime layer MUST resolve
# the rootless-daemon owner via ``resolve_daemon_owner`` / ``resolve_daemon_owner_settings``,
# NEVER by reading ``host.docker_unprivileged_user`` directly — in operator-rootless
# the owner is the invoking operator, so a direct read resolves to the stale
# ``"sandbox"`` default and silently corrupts on-disk ownership. The single sanctioned
# reader (for owner purposes) is the resolver in ``core.host_config``; this guard scopes
# the *runtime* modules and allowlists the functions that read the field for NON-owner,
# separate-user-only purposes. A new reader elsewhere (e.g. a lifecycle command binding
# ``host_user`` from the field) is the regression this catches.
_DOCKER_USER_READ_ALLOWLIST: dict[str, frozenset[str]] = {
    "src/cli/main.py": frozenset(
        {
            "_build_attach_argv",  # separate-user ProxyCommand pipe_cmd crossing only
            "init",                # seeds + auth-probes the separate-user dedicated user
            "doctor",              # separate-user boundary validation (mode-awareness → C-005)
        }
    ),
    "src/core/hydration.py": frozenset(),  # owner via resolve_daemon_owner_settings only
}


def _docker_user_read_functions(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, enclosing-function-name)`` for each ``.docker_unprivileged_user``
    attribute read in ``tree`` (AST-only — string literals / comments are never
    ``ast.Attribute`` nodes, so message text mentioning the field is not flagged)."""
    funcs = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    ]

    def enclosing(lineno: int) -> str:
        best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
        for f in funcs:
            if f.lineno <= lineno <= (f.end_lineno or f.lineno) and (
                best is None or f.lineno > best.lineno
            ):
                best = f
        return best.name if best is not None else "<module>"

    return [
        (node.lineno, enclosing(node.lineno))
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "docker_unprivileged_user"
    ]


def _docker_user_owner_violations(
    files: Iterator[Path], allowlist: dict[str, frozenset[str]]
) -> list[tuple[Path, list[tuple[int, str]]]]:
    """Scan the allowlisted runtime modules; return ``(path, [(lineno, func), …])``
    for every ``.docker_unprivileged_user`` read in a NON-sanctioned function.

    Reusable detector seam (anti-hack rule 5): ``files`` + ``allowlist`` are
    parameters, so the deliberate-violation regression drives the same predicate."""
    offenders: list[tuple[Path, list[tuple[int, str]]]] = []
    for src in files:
        rel = (
            src.relative_to(_REPO_ROOT).as_posix()
            if src.is_relative_to(_REPO_ROOT)
            else src.as_posix()
        )
        sanctioned = allowlist.get(rel)
        if sanctioned is None:
            continue  # only the enumerated runtime modules are scoped
        tree = ast.parse(src.read_text(), filename=str(src))
        bad = [
            (ln, fn) for ln, fn in _docker_user_read_functions(tree) if fn not in sanctioned
        ]
        if bad:
            offenders.append((src, bad))
    return offenders


def test_no_op_rootless_docker_user_owner_read() -> None:
    """No runtime owner-resolution reads ``docker_unprivileged_user`` directly (D7).

    The daemon owner MUST flow through ``resolve_daemon_owner(_settings)`` so the
    operator-rootless owner is the invoking operator, never the stale ``"sandbox"``
    default. Reads outside the allowlisted (separate-user-only / non-owner) functions
    fail — route the owner through the resolver instead of broadening the allowlist.
    """
    offenders = _docker_user_owner_violations(
        _python_files(_SRC_ROOT), _DOCKER_USER_READ_ALLOWLIST
    )
    if offenders:
        details = "\n".join(
            f"  {p.relative_to(_REPO_ROOT)}: {[(ln, fn) for ln, fn in bad]}"
            for p, bad in offenders
        )
        pytest.fail(
            f"{len(offenders)} runtime module(s) read host.docker_unprivileged_user "
            f"in a non-sanctioned function (D7).\n{details}\n\n"
            "Fix: resolve the daemon owner via core.host_config.resolve_daemon_owner"
            "(_settings) — in operator-rootless a direct read corrupts ownership "
            "(stale 'sandbox' default). Do NOT broaden the allowlist without a "
            "separate-user-only / non-owner justification."
        )


def test_docker_user_owner_guard_detects_violation(tmp_path: Path) -> None:
    """The D7 detector flags a ``.docker_unprivileged_user`` read in a non-sanctioned
    function — proving the guard catches the bug class, not just its absence."""
    rogue = tmp_path / "rogue_command.py"
    rogue.write_text(
        "def start(host_config):\n"
        "    host_user = host_config.host.docker_unprivileged_user\n"
        "    return host_user\n"
    )
    # Map the rogue file into the allowlist with NO sanctioned functions.
    offenders = _docker_user_owner_violations(
        iter([rogue]), {rogue.as_posix(): frozenset()}
    )
    assert offenders, "deliberate D7 owner-read violation was not detected"
    assert offenders[0][0] == rogue
    assert offenders[0][1] == [(2, "start")]


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
