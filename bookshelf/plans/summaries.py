"""Backfill summaries for shelved books that never had one looked up."""

from __future__ import annotations

from pathlib import Path

from ..catalog import Catalog
from .base import Context, Outcome, Plan


class SummaryPlan(Plan):
    empty_message = "All summaries already filled."

    def __init__(self, missing: list[tuple[int, str]], ctx: Context):
        self.missing = missing
        self.ctx = ctx

    def is_empty(self) -> bool:
        return not self.missing

    def payload(self) -> dict:
        """There is nothing to review — only a count worth showing first."""
        return {"missing": len(self.missing)}

    def apply(self, catalog: Catalog) -> Outcome:
        self.ctx.out(f"Fetching summaries for {len(self.missing)} book(s)…\n")
        titles = [Path(name).stem for _, name in self.missing]
        found = self.ctx.lookup.summaries(titles, progress=_progress(self.ctx))

        # Store every row the model answered for, even "" (meaning none
        # available) so it is not retried. Rows it skipped stay NULL.
        updates = [
            (found[Path(name).stem], row_id)
            for row_id, name in self.missing
            if Path(name).stem in found
        ]
        catalog.set_summaries(updates)
        filled = sum(1 for summary, _ in updates if summary)
        return Outcome(filled, 0,
                       f"Done. {filled}/{len(self.missing)} summaries added.")


def _progress(ctx: Context):
    def report(index: int, total: int, size: int) -> None:
        ctx.out(f"  Batch {index}/{total} ({size} books)…")
    return report


def build(ctx: Context) -> SummaryPlan:
    return SummaryPlan(ctx.catalog.missing_summaries(), ctx)
