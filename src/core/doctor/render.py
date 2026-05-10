"""Rich output renderer for doctor check results.

Groups results by category and prints with progressive disclosure: pass
results show one line, warn/fail results show detail + remediation,
skip results show a single dim line. The summary footer is colored by
the worst severity present.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console

    from core.doctor.types import CheckResult


def render_results(
    results: list[CheckResult],
    *,
    console: Console | None = None,
) -> None:
    """Render check results using Rich with progressive disclosure."""
    from rich.console import Console as RichConsole
    from rich.text import Text

    if console is None:
        console = RichConsole()

    # Group by category
    grouped: dict[str, list[CheckResult]] = defaultdict(list)
    for r in results:
        cat = r.category or "General"
        grouped[cat].append(r)

    pass_count = sum(1 for r in results if r.status == "pass")
    fail_count = sum(1 for r in results if r.status == "fail")
    skip_count = sum(1 for r in results if r.status == "skip")
    warn_count = sum(1 for r in results if r.status == "warn")

    for category, checks in grouped.items():
        console.print(f"\n[bold]{category}[/bold]")
        for r in checks:
            if r.status == "pass":
                line = Text(f"  ✓ {r.name}", style="green")
                if r.detail:
                    line.append(f"  {r.detail}", style="dim")
                console.print(line)
            elif r.status == "fail":
                console.print(Text(f"  ✗ {r.name}", style="red bold"))
                console.print(f"    {r.detail}")
                if r.remediation:
                    console.print(f"    Fix: {r.remediation}", style="yellow")
                if r.doc_ref:
                    console.print(f"    Docs: {r.doc_ref}", style="dim")
            elif r.status == "warn":
                console.print(Text(f"  ⚠ {r.name}", style="yellow"))
                console.print(f"    {r.detail}")
                if r.remediation:
                    console.print(f"    Fix: {r.remediation}", style="yellow")
            elif r.status == "skip":
                console.print(Text(f"  ⊘ {r.name} — {r.detail}", style="dim"))

    # Summary line
    console.print()
    summary = f"{pass_count}/{len(results)} passed"
    if warn_count:
        summary += f" · {warn_count} warnings"
    if fail_count:
        summary += f" · {fail_count} failed"
    if skip_count:
        summary += f" · {skip_count} skipped"

    if fail_count > 0:
        style = "red bold"
    elif warn_count > 0:
        style = "yellow bold"
    else:
        style = "green bold"
    console.print(summary, style=style)


__all__ = ["render_results"]
