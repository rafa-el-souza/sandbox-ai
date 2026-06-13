# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for project-wide test conventions — enforces fixture/marker/suppression rules.

Three structural guarantees, all AST-based:

1. **No runtime imports of helper functions from a `conftest.py`.** The
   pytest-canonical pattern is fixtures injected via parameters, not
   helpers imported across modules. Type-only imports under
   ``TYPE_CHECKING`` are exempt — those are type aliases, not behavior.
2. **No suppression directives.** ``# noqa``, ``# type: ignore``,
   ``# pragma: no cover``, ``# pyright: ignore`` are forbidden across `src/`
   and `tests/` per the global rule. Restructure code or remove unreachable
   branches instead of silencing the linter/type-checker.
3. **Every custom `@pytest.mark.X` is registered in `pyproject.toml`.**
   Catches typo'd or undeclared markers. Pytest builtins
   (``parametrize``, ``usefixtures``, ``skip``, ``skipif``, ``xfail``,
   ``filterwarnings``) are exempt.
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import TypeGuard

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
    re.compile(r"#\s*pyright:\s*ignore"),
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


# ── 4b. Single-sourced execution-mode default (F-051) ───────────────────────
#
# host-config "Single-Sourced Execution-Mode Default" (finding F-051): there is
# exactly ONE named execution-mode default in the system —
# ``core.host_config.DEFAULT_PROVISIONING_MODE``. A bare ``DockerExecutionMode``
# enum member (``SEPARATE_USER`` / ``OPERATOR_ROOTLESS``) MUST NOT appear as a
# function-parameter default or a field (``AnnAssign``) default RHS anywhere in
# ``src/`` except the module that DEFINES the constant. Two opposite-valued
# scattered defaults (the pre-F-051 state) crystallize into the public docs and
# silently diverge; the single named constant keeps the default coherent.
#
# This is enforced STRUCTURALLY (the ``_machinectl_cmd_violations`` pattern): an
# ``ast`` walk that flags the bare member ONLY in a default-slot position —
# ``FunctionDef``/``AsyncFunctionDef`` ``args.defaults`` + ``args.kw_defaults``,
# or an ``AnnAssign`` value (a field default). It deliberately does NOT flag
# ``Set`` elements (``applies_in=frozenset({DockerExecutionMode.SEPARATE_USER})``
# membership sets), ``Compare`` operands (``mode is DockerExecutionMode.…``), or
# ``Call`` arguments (an explicit ``write_mode_root_owned(…, SEPARATE_USER)`` or
# ``minimal_host_config(u, a, SEPARATE_USER)``) — those are not defaults.
_MODE_DEFAULT_ALLOWLIST: frozenset[str] = frozenset({"src/core/host_config.py"})
_DOCKER_EXECUTION_MODE_MEMBERS: frozenset[str] = frozenset(
    {"SEPARATE_USER", "OPERATOR_ROOTLESS"}
)


def _is_bare_mode_member(node: ast.expr | None) -> TypeGuard[ast.Attribute]:
    """True iff ``node`` is a bare ``DockerExecutionMode.<MEMBER>`` attribute
    reference (the enum-default literal the gate forbids in a default slot).

    A ``TypeGuard`` so callers can read ``.lineno`` off the narrowed
    ``ast.Attribute`` without mypy flagging the ``expr | None`` input (an
    ``ast.Attribute`` is never ``None``)."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr in _DOCKER_EXECUTION_MODE_MEMBERS
        and isinstance(node.value, ast.Name)
        and node.value.id == "DockerExecutionMode"
    )


def _bare_mode_default_lines(tree: ast.AST) -> list[int]:
    """Return line numbers where a bare ``DockerExecutionMode`` member appears in
    a DEFAULT position: a function param default (positional or keyword-only) or
    an ``AnnAssign`` (field-annotation) value. Membership-set elements,
    comparisons, and call arguments are NOT default positions and are ignored."""
    linenos: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            args = node.args
            for default in (*args.defaults, *args.kw_defaults):
                if _is_bare_mode_member(default):
                    linenos.add(default.lineno)
        elif isinstance(node, ast.AnnAssign) and _is_bare_mode_member(node.value):
            linenos.add(node.value.lineno)
    return sorted(linenos)


def _mode_default_violations(
    files: Iterator[Path], allowlist: frozenset[str]
) -> list[tuple[Path, list[int]]]:
    """Scan ``files``; return ``(path, linenos)`` for every file with a bare
    ``DockerExecutionMode`` member in a default slot while NOT in ``allowlist``.

    Reusable detector seam (anti-hack rule 5): the file iterable + allowlist are
    parameters, not module state, so the deliberate-violation regression drives
    the SAME predicate against an arbitrary out-of-allowlist file."""
    offenders: list[tuple[Path, list[int]]] = []
    for src in files:
        rel = (
            src.relative_to(_REPO_ROOT).as_posix()
            if src.is_relative_to(_REPO_ROOT)
            else src.as_posix()
        )
        if rel in allowlist:
            continue
        tree = ast.parse(src.read_text(), filename=str(src))
        linenos = _bare_mode_default_lines(tree)
        if linenos:
            offenders.append((src, linenos))
    return offenders


def test_no_bare_mode_literal_defaults() -> None:
    """A bare ``DockerExecutionMode`` member is forbidden as a param/field default
    outside ``core.host_config`` (host-config "Single-Sourced Execution-Mode
    Default", F-051).

    Every default must reference ``DEFAULT_PROVISIONING_MODE`` — the single named
    constant — so the system never carries two opposite-valued defaults. The
    allowlist is exactly the module that DEFINES the constant; broadening it
    re-introduces the scatter the gate exists to prevent.
    """
    offenders = _mode_default_violations(_python_files(_SRC_ROOT), _MODE_DEFAULT_ALLOWLIST)

    if offenders:
        details = "\n".join(
            f"  {p.relative_to(_REPO_ROOT)}: line(s) {linenos}"
            for p, linenos in offenders
        )
        pytest.fail(
            f"{len(offenders)} file(s) use a bare DockerExecutionMode member as a "
            f"param/field default outside core.host_config.\n{details}\n\n"
            f"Allowlist: {sorted(_MODE_DEFAULT_ALLOWLIST)}\n"
            "Fix: reference core.host_config.DEFAULT_PROVISIONING_MODE — the single "
            "system-wide execution-mode default (F-051). Do NOT spell the bare enum "
            "member in a default slot; that re-creates the two-default scatter."
        )


def test_mode_default_deliberate_violation_is_detected(tmp_path: Path) -> None:
    """A file with a bare ``DockerExecutionMode`` member in BOTH default-slot kinds
    (a function param default and an ``AnnAssign`` field default) is reported by
    the shared detector — proving the guard catches the bug class, not just the
    absence of the symptom. A membership-set element, a comparison, and a call
    argument in the SAME file must NOT be flagged (the structural carve-outs)."""
    rogue = tmp_path / "rogue_default.py"
    rogue.write_text(
        "def f(mode=DockerExecutionMode.SEPARATE_USER):\n"  # param default → FLAG
        "    return mode\n"
        "\n"
        "class C:\n"
        "    m: DockerExecutionMode = DockerExecutionMode.OPERATOR_ROOTLESS\n"  # field → FLAG
        "\n"
        "S = frozenset({DockerExecutionMode.SEPARATE_USER})\n"  # set elem → NOT flagged
        "ok = x is DockerExecutionMode.SEPARATE_USER\n"  # compare → NOT flagged
        "build(DockerExecutionMode.SEPARATE_USER)\n"  # call arg → NOT flagged
    )

    offenders = _mode_default_violations(iter([rogue]), _MODE_DEFAULT_ALLOWLIST)

    assert offenders, "deliberate bare-mode-default violation was not detected"
    offending_paths = {p for p, _ in offenders}
    assert rogue in offending_paths
    # Exactly the two default-slot lines (param default line 1, field default
    # line 5) — the set/compare/call lines (7, 8, 9) are NOT flagged.
    (_path, linenos) = offenders[0]
    assert linenos == [1, 5], (
        f"detector flagged {linenos}; expected only the param + field default "
        "lines (the set/compare/call carve-outs must not fire)"
    )


# ── 5. Streaming-op discipline: fwd is reachable ONLY via proxy_argv ─────────
#
# The runtime-dispatcher "Streaming ProxyCommand Entrypoint" requirement pins,
# structurally (the test_machinectl_cmd_callers_restricted pattern), that:
#   (a) no src/ call site passes a streaming op (Op.FWD / "fwd") to
#       core.dispatch.invoke()/probe() — those capture output the orchestrator
#       would branch on, and a streaming op carries zero such content; and
#   (b) no src/ module OTHER than core.dispatch constructs a ``dispatch fwd``
#       payload or its docker-exec target argv (the ``/fwd`` dial) directly —
#       core.dispatch.proxy_argv is the single sanctioned producer.
# A hand-rolled fwd payload elsewhere would reintroduce the forgery surface the
# streaming carve-out narrowed; this guard fails the gate if it reappears.

# core.dispatch is the sole sanctioned home for fwd payload/argv construction.
_STREAMING_DISPATCH_MODULE = "src/core/dispatch.py"
# The wire payload prefix that crosses the boundary, and the in-container dial
# binary of the docker-exec target argv. The payload SUBSTRING appearing in any
# literal, or a literal EQUAL to the standalone ``/fwd`` argv element, OUTSIDE
# core.dispatch means a module is hand-building the streaming crossing. The
# argv-element match is by EQUALITY (not substring) so an unrelated path literal
# like ``docker/admin/fwd.go`` (which merely contains ``/fwd``) is not a false
# positive — only the discrete docker-exec ``"/fwd"`` argv element is the dial.
# core.dispatch.proxy_argv is the single sanctioned producer of either literal;
# there are no exemptions (cli.main._build_attach_argv and the start dry-run
# preview obtain the ProxyCommand from proxy_argv, holding neither literal).
_FWD_PAYLOAD_SUBSTR = "dispatch fwd"
_FWD_TARGET_BINARY = "/fwd"


def _fwd_invoke_probe_call_lines(tree: ast.AST) -> list[int]:
    """Return line numbers of ``invoke(...)``/``probe(...)`` calls whose FIRST
    positional argument names the streaming op (``Op.FWD`` attribute or the
    ``"fwd"`` wire-name constant).

    AST-only: the callee must be a bare ``invoke``/``probe`` name or a
    ``….invoke``/``….probe`` attribute (e.g. ``dispatch.invoke``), and the first
    arg is inspected for ``Op.FWD`` (``ast.Attribute`` ``attr == "FWD"``) or the
    string constant ``"fwd"``.
    """
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        callee = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else None
        )
        if callee not in {"invoke", "probe"}:
            continue
        first = node.args[0]
        is_fwd = (isinstance(first, ast.Attribute) and first.attr == "FWD") or (
            isinstance(first, ast.Constant) and first.value == "fwd"
        )
        if is_fwd:
            lines.append(node.lineno)
    return sorted(lines)


def _fwd_payload_literal_lines(tree: ast.AST) -> tuple[list[int], list[int]]:
    """Return ``(payload_lines, target_argv_lines)`` for string-literal nodes.

    ``payload_lines`` embed the ``dispatch fwd`` wire payload; ``target_argv_lines``
    embed the bare ``/fwd`` docker-exec target binary. Both are sanctioned only
    in core.dispatch. AST-only (``ast.Constant`` string nodes) so
    comments/docstrings that merely *mention* the op in prose are NOT flagged —
    only real string literals a module would interpolate into a crossing argv."""
    payload_lines: list[int] = []
    target_lines: list[int] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if _FWD_PAYLOAD_SUBSTR in node.value:
            payload_lines.append(node.lineno)
        elif node.value == _FWD_TARGET_BINARY:
            target_lines.append(node.lineno)
    return sorted(payload_lines), sorted(target_lines)


def _streaming_op_violations(
    files: Iterator[Path],
) -> list[tuple[Path, str, list[int]]]:
    """Scan ``files``; return ``(path, kind, linenos)`` for each violation.

    ``kind`` is ``"invoke/probe"`` (a streaming op passed to invoke/probe — flagged
    in EVERY scanned file incl. core.dispatch, which must never do it either) or
    ``"payload"`` (a ``dispatch fwd`` / ``/fwd`` literal OUTSIDE core.dispatch).

    Reusable detector seam (anti-hack rule 5): ``files`` is a parameter so the
    deliberate-violation regression drives the same predicate."""
    offenders: list[tuple[Path, str, list[int]]] = []
    for src in files:
        rel = (
            src.relative_to(_REPO_ROOT).as_posix()
            if src.is_relative_to(_REPO_ROOT)
            else src.as_posix()
        )
        tree = ast.parse(src.read_text(), filename=str(src))
        ip_lines = _fwd_invoke_probe_call_lines(tree)
        if ip_lines:
            offenders.append((src, "invoke/probe", ip_lines))
        # Payload/argv construction is sanctioned ONLY in core.dispatch.
        if rel != _STREAMING_DISPATCH_MODULE:
            payload_lines, target_lines = _fwd_payload_literal_lines(tree)
            if payload_lines:
                # The ``dispatch fwd`` wire payload is sanctioned only here.
                offenders.append((src, "payload", payload_lines))
            if target_lines:
                # The bare ``/fwd`` docker-exec argv is sanctioned only here.
                offenders.append((src, "target-argv", target_lines))
    return offenders


def test_streaming_op_reachable_only_via_proxy_argv() -> None:
    """``fwd`` (the streaming op) is reachable ONLY via core.dispatch.proxy_argv.

    (a) No src/ call site passes ``Op.FWD``/``"fwd"`` to invoke()/probe(); (b) no
    src/ module other than core.dispatch builds a ``dispatch fwd`` payload or its
    ``/fwd`` docker-exec target argv. Enforces the runtime-dispatcher "Streaming
    ProxyCommand Entrypoint" invariant structurally (C-010 D3).
    """
    offenders = _streaming_op_violations(_python_files(_SRC_ROOT))
    if offenders:
        details = "\n".join(
            f"  {p.relative_to(_REPO_ROOT)} [{kind}]: line(s) {linenos}"
            for p, kind, linenos in offenders
        )
        pytest.fail(
            f"{len(offenders)} streaming-op discipline violation(s).\n{details}\n\n"
            "Fix: route streaming-op crossings through core.dispatch.proxy_argv "
            "(it constructs but never executes the ProxyCommand argv); never pass "
            "a streaming op to invoke()/probe(), and never hand-build a "
            "'dispatch fwd' payload or its '/fwd' docker-exec argv outside "
            "core.dispatch (runtime-dispatcher 'Streaming ProxyCommand Entrypoint')."
        )


def test_streaming_op_deliberate_violations_are_detected(tmp_path: Path) -> None:
    """All three violation kinds are caught by the shared detector (proves the
    guard catches the bug class, not just the symptom's absence)."""
    rogue_invoke = tmp_path / "rogue_invoke.py"
    rogue_invoke.write_text("invoke(Op.FWD, ['myinst'], hc)\nprobe('fwd', ['x'], hc)\n")
    rogue_payload = tmp_path / "rogue_payload.py"
    rogue_payload.write_text('CMD = "/usr/local/libexec/sandbox-ai/dispatch fwd myinst"\n')
    rogue_target = tmp_path / "rogue_target.py"
    rogue_target.write_text('ARGV = ["docker", "exec", "-i", "p-admin-1", "/fwd", "10.100.0.7:9999"]\n')

    offenders = _streaming_op_violations(iter([rogue_invoke, rogue_payload, rogue_target]))
    kinds_by_path = {(p, kind) for p, kind, _ in offenders}
    assert (rogue_invoke, "invoke/probe") in kinds_by_path
    assert (rogue_payload, "payload") in kinds_by_path
    assert (rogue_target, "target-argv") in kinds_by_path


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
#
# Scanned-module set (C-005 1.7): the original two runtime modules (``cli/main.py``,
# ``hydration.py``) PLUS every ``core/doctor/checks/*.py`` module — now that the doctor
# checks are op-rootless-reachable (the runner threads the active ``mode`` + the operator
# owner into them, C-005 1.1-1.6), an *unguarded* owner-read of ``docker_unprivileged_user``
# in a check is exactly the regression this broadening catches. A scanned file with NO
# allowlist entry is scanned with an empty sanctioned set: ANY read fails the guard.
_DOCKER_USER_SCANNED_DIRS: tuple[Path, ...] = (_SRC_ROOT / "core" / "doctor" / "checks",)
_DOCKER_USER_SCANNED_FILES: tuple[Path, ...] = (
    _SRC_ROOT / "cli" / "main.py",
    _SRC_ROOT / "core" / "hydration.py",
)
_DOCKER_USER_READ_ALLOWLIST: dict[str, frozenset[str]] = {
    "src/cli/main.py": frozenset(
        {
            "_build_attach_argv",  # separate-user ProxyCommand pipe_cmd crossing only
            "init",                # seeds + auth-probes the separate-user dedicated user
            "doctor",              # separate-user boundary validation (mode-awareness → C-005)
        }
    ),
    "src/core/hydration.py": frozenset(),  # owner via resolve_daemon_owner_settings only
    # The sudoers-rule body audit re-renders the SUDO rule that enumerates the
    # dedicated ``sandbox_user``; the whole ``setup_invariants`` check is
    # ``applies_in=separate-user`` (registry), so this read is correctly
    # mode-guarded and is NOT an owner-resolution read.
    # ``_audit_rule_body`` re-renders the SUDO rule that enumerates the dedicated
    # ``sandbox_user``; ``_audit_daemon_user_no_admin`` reads the dedicated daemon
    # user to verify it is in NO admin group. Both are separate-user-only sub-audits
    # of the both-mode ``setup_invariants`` check (the caller runs them only on the
    # separate-user branch, after the operator-rootless early-return) — sanctioned
    # reads of the dedicated user, NOT operator-rootless owner-resolution reads.
    "src/core/doctor/checks/setup_invariants.py": frozenset(
        {"_audit_rule_body", "_audit_daemon_user_no_admin"}
    ),
}


def _docker_user_scanned_files() -> list[Path]:
    """The D7-scanned module set: the two runtime files + every doctor-check module.

    Defining the scanned set independently of the allowlist (rather than scanning
    only allowlist keys) is the load-bearing 1.7 broadening: a brand-new doctor
    check that reads ``docker_unprivileged_user`` is scanned with an empty
    sanctioned set and fails the guard, instead of being silently skipped."""
    files = list(_DOCKER_USER_SCANNED_FILES)
    for d in _DOCKER_USER_SCANNED_DIRS:
        files.extend(_python_files(d))
    return files


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
    """Scan the given module set; return ``(path, [(lineno, func), …])``
    for every ``.docker_unprivileged_user`` read in a NON-sanctioned function.
    A scanned file absent from ``allowlist`` defaults to an empty sanctioned set.

    Reusable detector seam (anti-hack rule 5): ``files`` + ``allowlist`` are
    parameters, so the deliberate-violation regression drives the same predicate."""
    offenders: list[tuple[Path, list[tuple[int, str]]]] = []
    for src in files:
        rel = (
            src.relative_to(_REPO_ROOT).as_posix()
            if src.is_relative_to(_REPO_ROOT)
            else src.as_posix()
        )
        # Every scanned file is in-scope; a file with no allowlist entry has an
        # empty sanctioned set, so ANY owner-read in it fails the guard.
        sanctioned = allowlist.get(rel, frozenset())
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
        iter(_docker_user_scanned_files()), _DOCKER_USER_READ_ALLOWLIST
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


def test_docker_user_owner_guard_catches_unguarded_doctor_check(tmp_path: Path) -> None:
    """A NEW doctor check that reads ``docker_unprivileged_user`` for owner purposes
    fails the broadened guard (C-005 1.7).

    Proves the scanned-set broadening: a check module with NO allowlist entry is
    scanned with an empty sanctioned set (the ``allowlist.get(rel, frozenset())``
    default), so an unguarded op-rootless owner-read is flagged rather than silently
    skipped — mirroring how ``test_docker_user_owner_guard_detects_violation`` proves
    the catch for the original two-module scope."""
    rogue_check = tmp_path / "rogue_check.py"
    rogue_check.write_text(
        "def check_rogue(user, distro, host_config):\n"
        "    owner = host_config.host.docker_unprivileged_user\n"
        "    return owner\n"
    )
    # No allowlist entry for the rogue check → empty sanctioned set → any read fails.
    offenders = _docker_user_owner_violations(iter([rogue_check]), _DOCKER_USER_READ_ALLOWLIST)
    assert offenders, "broadened D7 guard did not catch the unguarded doctor-check owner-read"
    assert offenders[0][0] == rogue_check
    assert offenders[0][1] == [(2, "check_rogue")]


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


# ── 6. SPDX license-header presence (publish-prep, source-license-headers) ───
#
# Every covered first-party source file SHALL carry the SPDX license-identifier
# token within its first few lines. The one-time sweep (publish-prep task 2.2)
# is the migration; THIS gate is the durable drift-prevention contract: a newly
# added source file without the header fails `make coverage`.
#
# The covered set is built from the CURRENT tree, not a frozen list:
#   - Python: every `*.py` under `src/` and `tests/` (the `#` token).
#   - Templates: EVERY file under `src/templates/` that is not Python/pyc — so a
#     new non-source template file is FORCED into the allowlist (drift-proof),
#     never silently exempt. The comment token differs by type (`//` Go,
#     `{# … #}` Jinja-rendered, `#` shell/Dockerfile/config), but the normative
#     invariant the gate enforces is only the SPDX TOKEN substring; the comment
#     framing is the sweep's concern, not the gate's.
# Generated / comment-less / served-verbatim files are exempt via
# `_SPDX_ALLOWLIST` (one-line reason each, mirroring `_LAYOUT_ALLOWLIST`); a
# dead allowlist entry fails the gate so exemptions cannot rot.

# The normative invariant: this exact substring must appear in a covered file's
# first few lines, whatever the comment token framing it.
_SPDX_TOKEN = "SPDX-License-Identifier: AGPL-3.0-or-later"
# How many leading lines may carry the header (shebang + header → line 2).
_SPDX_HEADER_LINES = 5

_TEMPLATES_ROOT = _SRC_ROOT / "templates"

# Covered-set template files with no header, each with a one-line reason. Keyed
# by repo-relative POSIX path (mirrors `_LAYOUT_ALLOWLIST`).
_SPDX_ALLOWLIST: dict[str, str] = {
    "src/templates/config/core/.claude.json": (
        "JSON config seed — JSON has no comment syntax, a header would be invalid."
    ),
    "src/templates/dispatch/fixtures/target_argv_cases.json": (
        "JSON test fixture — JSON has no comment syntax."
    ),
    "src/templates/config/proxy/ERR_SANDBOX_403": (
        "Static HTTP 403 error body served verbatim to clients; a header would "
        "leak into the response page."
    ),
    "src/templates/dispatch/go.mod": "Managed by the Go toolchain.",
    "src/templates/dispatch/go.sum": "Generated checksum file.",
    "src/templates/dispatch/vendor/modules.txt": "Vendored module manifest (generated).",
}


def _spdx_covered_template_files() -> Iterator[Path]:
    """Every file under `src/templates/` that is NOT Python/pyc and not in a
    `__pycache__` dir — the covered template set, derived from the live tree.

    Built from the tree (not a frozen list) so a newly added non-source template
    file is forced through the gate: it must either carry the header or earn an
    explicit `_SPDX_ALLOWLIST` entry — it cannot be silently skipped."""
    for p in _TEMPLATES_ROOT.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix in (".py", ".pyc") or "__pycache__" in p.parts:
            continue
        yield p


def _spdx_violations(files: Iterator[Path], allowlist: dict[str, str]) -> list[str]:
    """Scan ``files``; return the repo-relative (or absolute, if outside the
    repo) path of every file that lacks the SPDX token within its first
    ``_SPDX_HEADER_LINES`` lines and is NOT in ``allowlist``.

    Reusable detector seam (anti-hack rule 5): the file iterable and allowlist
    are PARAMETERS, not module state, so the deliberate-violation regression
    (:func:`test_spdx_deliberate_violation_is_detected`) drives the SAME
    predicate against arbitrary tmp_path files without duplicating the logic."""
    offenders: list[str] = []
    for src in files:
        key = (
            src.relative_to(_REPO_ROOT).as_posix()
            if src.is_relative_to(_REPO_ROOT)
            else src.as_posix()
        )
        if key in allowlist:
            continue
        head = "\n".join(src.read_text().splitlines()[:_SPDX_HEADER_LINES])
        if _SPDX_TOKEN not in head:
            offenders.append(key)
    return offenders


def _spdx_covered_files() -> list[Path]:
    """The full covered set from the live tree: Python under src/ + tests/, plus
    every non-Python file under src/templates/."""
    files = list(_python_files(_SRC_ROOT)) + list(_python_files(_TESTS_ROOT))
    files.extend(_spdx_covered_template_files())
    return files


def test_spdx_headers_present() -> None:
    """Every covered first-party source file carries the SPDX header
    (publish-prep "SPDX License Header Presence").

    The covered set is built from the CURRENT tree, so a newly added source file
    without the header (and not allowlisted) fails this gate — the durable
    drift-prevention contract behind the one-time sweep.
    """
    offenders = _spdx_violations(iter(_spdx_covered_files()), _SPDX_ALLOWLIST)
    if offenders:
        details = "\n".join(f"  {o}" for o in sorted(offenders))
        pytest.fail(
            f"{len(offenders)} covered file(s) lack the SPDX header.\n{details}\n\n"
            f"Fix: add `{_SPDX_TOKEN}` within the first {_SPDX_HEADER_LINES} "
            "lines, using the comment token for the file type (`#` Python/shell/"
            "Dockerfile/config, `//` Go, `{# … #}` Jinja-rendered template — the "
            "Jinja comment is stripped at render so it never reaches output). If "
            "the file genuinely cannot carry a header (no comment syntax / served "
            "verbatim / generated), add it to _SPDX_ALLOWLIST with a one-line reason."
        )


def test_spdx_allowlist_has_no_stale_entries() -> None:
    """Every `_SPDX_ALLOWLIST` key must correspond to a file on disk.

    Mirrors `_LAYOUT_ALLOWLIST`'s self-validation: a dead exemption (the file was
    renamed or deleted) fails the gate, so the allowlist cannot silently rot."""
    stale = [rel for rel in _SPDX_ALLOWLIST if not (_REPO_ROOT / rel).exists()]
    if stale:
        details = "\n".join(f"  {s}" for s in sorted(stale))
        pytest.fail(
            f"{len(stale)} stale _SPDX_ALLOWLIST entr(y/ies) — path no longer exists.\n"
            f"{details}\n\nFix: remove the dead entry (the file was renamed or deleted)."
        )


def test_spdx_deliberate_violation_is_detected(tmp_path: Path) -> None:
    """A header-LESS file is flagged by the shared detector; a header-FULL file is
    not — proving the gate catches the bug class (a new file missing the header),
    not merely the current absence of the symptom.

    Drives the SAME :func:`_spdx_violations` predicate the gate uses (no
    duplicated logic), satisfying the 2.4 "header-less new file fails the gate /
    then revert" requirement durably as a tmp_path regression."""
    headerless = tmp_path / "rogue_no_header.py"
    headerless.write_text("import os\n\n\ndef f() -> None:\n    return None\n")
    headerful = tmp_path / "rogue_with_header.py"
    headerful.write_text(
        f"# Copyright (c) 2026 zerotrust-ai. {_SPDX_TOKEN}\nimport os\n"
    )

    offenders = _spdx_violations(iter([headerless, headerful]), {})

    assert headerless.as_posix() in offenders, (
        "deliberate header-less file was not flagged by _spdx_violations"
    )
    assert headerful.as_posix() not in offenders, (
        "a file carrying the SPDX token was wrongly flagged"
    )


_PYRIGHT_TEST_RELAXATIONS = frozenset(
    {
        "reportPrivateUsage",
        "reportUnknownLambdaType",
        "reportUnknownArgumentType",
        "reportUnknownMemberType",
        "reportUnusedFunction",
    }
)


def test_pyrightconfig_relaxations_whitelisted() -> None:
    """pyright is strict on ``src/`` and relaxes ONLY a fixed whitelist of rules
    for ``tests/``. The config is inverted on purpose — the whitelist is relaxed
    at the top level (governing ``tests/``, where pyright resolves imports
    correctly) and re-enabled for the production trees ``src/`` and ``scripts/``
    via per-root execution environments, because pyright cannot resolve a
    ``src/``-layout's first-party imports under a subdirectory root.

    This guards the pyright-strict gate against silent widening: any NEW
    top-level relaxation, any relaxation set to something other than ``none``, or
    any whitelisted rule the ``src/`` environment fails to re-enable to ``error``
    fails here. It is the pyright analogue of
    :func:`test_no_suppression_directives` — the gate is signal; keep it.
    """
    cfg_path = _REPO_ROOT / "pyrightconfig.json"
    # pyright parses pyrightconfig.json as JSONC; strip ``//`` line comments.
    raw = re.sub(r"(?m)^\s*//.*$", "", cfg_path.read_text())
    cfg = json.loads(raw)

    assert cfg.get("typeCheckingMode") == "strict", "pyright must run in strict mode"

    top_overrides = {k: v for k, v in cfg.items() if k.startswith("report")}
    assert set(top_overrides) == set(_PYRIGHT_TEST_RELAXATIONS), (
        "top-level pyright rule overrides drifted from the whitelist: "
        f"{set(top_overrides) ^ set(_PYRIGHT_TEST_RELAXATIONS)}"
    )
    assert all(v == "none" for v in top_overrides.values()), (
        f"top-level pyright overrides must all be 'none': {top_overrides}"
    )

    # Pin the analyzed scope so a future edit can't silently SHRINK it
    # (axis-e widening): no dropped include entry, no exclude/ignore escape hatch.
    assert cfg.get("include") == ["src", "tests", "scripts"], (
        f"pyright 'include' (the analyzed scope) drifted: {cfg.get('include')}"
    )
    assert cfg.get("extraPaths") == ["src"], f"pyright 'extraPaths' drifted: {cfg.get('extraPaths')}"
    for shrink_key in ("exclude", "ignore"):
        assert shrink_key not in cfg, (
            f"pyrightconfig must not set top-level '{shrink_key}' — it can silently "
            "shrink the analyzed scope below the pinned include set"
        )

    # Every production tree (src/, scripts/) gets its OWN env re-enabling the
    # full whitelist to 'error', so all production code stays fully strict.
    _strict_roots = {"src", "scripts"}
    envs = cfg.get("executionEnvironments", [])
    assert {e.get("root") for e in envs} == _strict_roots and len(envs) == len(_strict_roots), (
        f"executionEnvironments must be exactly the strict production roots {_strict_roots}, "
        f"got {[e.get('root') for e in envs]}"
    )
    for env in envs:
        overrides = {k: v for k, v in env.items() if k.startswith("report")}
        assert set(overrides) == set(_PYRIGHT_TEST_RELAXATIONS), (
            f"the {env.get('root')}/ env must re-enable exactly the whitelist (so it stays "
            f"fully strict): {set(overrides) ^ set(_PYRIGHT_TEST_RELAXATIONS)}"
        )
        assert all(v == "error" for v in overrides.values()), (
            f"the {env.get('root')}/ env must set every whitelisted rule to 'error': {overrides}"
        )
