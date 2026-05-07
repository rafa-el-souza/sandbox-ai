"""Tests for Rich markup safety — AST regression scan over `console.print` literals.

Per the `orchestrator-cli` capability's "Rich Markup Safety in Console Output"
requirement: any `console.print(...)` call whose message contains literal
``[<token>]`` characters MUST either pass ``markup=False`` or wrap the
fragment in ``rich.markup.escape(...)`` if ``<token>`` is not a Rich style
token. Rich silently consumes unrecognized ``[token]`` sequences as style
tags, producing messages with missing words and double-spacing — a defect
class that historically corrupted operator-facing diagnostics (e.g.,
``[host]``, ``[workspaces]``).

**Scope (AST-only, intentional limitation):** this test inspects literal
bracketed tokens in source — ``Constant`` strings and ``JoinedStr`` /
f-string component literals passed directly to ``console.print``.
Dynamically-constructed strings (``console.print(get_msg())``,
``console.print(some_var)``) are NOT inspected because the AST has no
literal to grep. This is acceptable because the bug class we are fixing
is literal ``[token]`` in source; runtime-built strings would require
runtime instrumentation to catch and are out of scope.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

# ── Style allowlist ─────────────────────────────────────────────────────────
#
# Seeded by surveying the post-fix tree:
#     rg -o 'style="[^"]+"' src/ | sed 's/.*style="\\([^"]*\\)".*/\\1/' | sort -u
# Survey performed on 2026-05-07. Re-run when adding new console.print styles
# and add any genuine style tokens that appear (single colors, style keywords,
# or whitespace-separated combinations). Do NOT widen the allowlist with
# non-style tokens to silence the test — escape the offender or pass
# ``markup=False`` instead.
RICH_STYLE_TOKENS: frozenset[str] = frozenset(
    {
        # Colors
        "red", "green", "yellow", "blue", "cyan", "magenta", "white", "black",
        "bright_red", "bright_green", "bright_yellow", "bright_blue",
        "bright_cyan", "bright_magenta", "bright_white", "bright_black",
        # Style keywords
        "bold", "dim", "italic", "underline", "reverse", "blink",
        # Combinations observed via the survey above.
        "red bold", "green bold", "yellow bold",
        # Closing forms (Rich treats `[/]` as "close any open style").
        "/", "/red", "/green", "/yellow", "/blue", "/cyan", "/magenta",
        "/white", "/black", "/bold", "/dim", "/italic", "/underline",
        "/reverse", "/blink", "/red bold", "/green bold", "/yellow bold",
    }
)

# Matches `[<token>]` or `[/<token>]` where <token> is one identifier or
# whitespace-separated identifiers (Rich's style composition syntax).
_BRACKET_TOKEN_RE = re.compile(
    r"\[([a-zA-Z_][\w]*( [a-zA-Z_][\w]*)*|/[a-zA-Z_][\w]*( [a-zA-Z_][\w]*)*|/)\]"
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCAN_ROOTS = (_REPO_ROOT / "src" / "cli", _REPO_ROOT / "src" / "core")


def _is_console_print(call: ast.Call) -> bool:
    """Match ``console.print(...)`` (or any ``<name>.print(...)`` with name 'console')."""
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr != "print":
        return False
    if not isinstance(func.value, ast.Name):
        return False
    return func.value.id == "console"


def _has_markup_false(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "markup" and isinstance(kw.value, ast.Constant) and kw.value.value is False:
            return True
    return False


def _literal_fragments(node: ast.AST) -> list[str]:
    """Extract literal string fragments from a positional argument.

    Handles ``Constant`` strings and ``JoinedStr`` (f-string) component
    literals. Non-literal interpolations (``FormattedValue``) are ignored —
    they're runtime-built and out of scope per the module docstring.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        out: list[str] = []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                out.append(part.value)
        return out
    return []


def _iter_python_files(root: Path) -> Iterator[Path]:
    yield from root.rglob("*.py")


def _collect_offenders() -> list[tuple[Path, int, str]]:
    """Walk source roots, find every console.print Call, return offenders.

    Each offender is ``(file, lineno, token)`` for one bracketed token
    that is neither in the allowlist nor accompanied by ``markup=False``.
    """
    offenders: list[tuple[Path, int, str]] = []
    for root in _SCAN_ROOTS:
        for src in _iter_python_files(root):
            tree = ast.parse(src.read_text(), filename=str(src))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not _is_console_print(node):
                    continue
                if _has_markup_false(node):
                    continue
                # Inspect every positional argument's literal fragments.
                for arg in node.args:
                    for fragment in _literal_fragments(arg):
                        for match in _BRACKET_TOKEN_RE.finditer(fragment):
                            token = match.group(1)
                            if token not in RICH_STYLE_TOKENS:
                                offenders.append((src, node.lineno, token))
    return offenders


def test_no_unallowed_bracket_tokens_in_console_print_literals() -> None:
    """Every literal `[<token>]` in a `console.print` call MUST be a known style or use markup=False."""
    offenders = _collect_offenders()
    if offenders:
        details = "\n".join(
            f"  {file.relative_to(_REPO_ROOT)}:{lineno}: unallowed token '[{token}]' in console.print literal"
            for file, lineno, token in offenders
        )
        pytest.fail(
            f"Rich markup safety: {len(offenders)} offender(s) found.\n{details}\n\n"
            "Fix: pass `markup=False` to the call, OR wrap the fragment via "
            "`rich.markup.escape(...)`. Extend RICH_STYLE_TOKENS only for "
            "genuine style tokens, never to silence non-style data."
        )
