"""The one confirmation loop, shared by every plan."""

from __future__ import annotations

from typing import Callable

from .catalog import Catalog
from .plans.base import Plan, Preview
from .render import table


def render(preview: Preview, out: Callable[..., None] = print) -> None:
    for section in preview.sections:
        if section.heading:
            out(section.heading)
        if section.rows:
            out(table(section.rows, section.headers, detail_last=section.detail_last))
        for line in section.lines:
            out(line)
    if preview.footer:
        out(preview.footer)


def run_plan(
    plan: Plan,
    catalog: Catalog,
    *,
    assume_yes: bool = False,
    ask: Callable[[str], str] = input,
    out: Callable[..., None] = print,
) -> int:
    """Preview, confirm, apply. Returns a process exit code.

    Typing a row number re-opens the preview after plan.edit() handles it,
    which is how the shelve mode retargets a book's category.
    """
    if plan.is_empty():
        out(plan.empty_message)
        return 0

    if plan.confirm_required:
        while True:
            out()
            render(plan.preview(), out)
            out()

            plan.stage(catalog)

            answer = "y" if assume_yes else ask(plan.confirm_prompt()).strip()
            lowered = answer.lower()
            if lowered == "y":
                break
            if not plan.supports_edit or lowered in ("n", ""):
                # Plans without an edit affordance are strictly y-or-abort.
                out(plan.abort_message)
                return 0
            if answer.isdigit():
                if not plan.edit(int(answer) - 1):
                    out(f"  Invalid row number — enter 1–{plan.row_count}.")
                continue
            out("  Enter y, n, or a row number.")

    outcome = plan.apply(catalog)
    if outcome.summary:
        # The interactive modes separate the result from the prompt with a
        # blank line; the non-confirming ones print straight through.
        if plan.confirm_required:
            out()
        out(outcome.summary)
    # The original reported per-file errors in the summary line but always
    # exited 0. Preserved deliberately: existing aliases and any scripted use
    # would otherwise start seeing failures for partial runs.
    return 0
