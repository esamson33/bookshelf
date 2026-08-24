"""File books out of the landing pad into their subject categories."""

from __future__ import annotations

from pathlib import Path

from ..catalog import Catalog
from ..models import Proposal
from ..render import truncate
from ..scan import categories, pdf_names, reconcile, sample_titles
from .base import Context, Outcome, Plan, PlanError, Preview, Section


class ShelvePlan(Plan):
    abort_message = "Aborted. Proposals are saved — re-run to apply them."
    supports_edit = True

    def __init__(self, proposals: list[Proposal], landing_dir: Path, data_dir: Path,
                 empty_message: str = "Nothing to shelve."):
        self.proposals = proposals
        self.landing_dir = landing_dir
        self.data_dir = data_dir
        self.empty_message = empty_message

    @property
    def row_count(self) -> int:
        return len(self.proposals)

    def is_empty(self) -> bool:
        return not self.proposals

    def confirm_prompt(self) -> str:
        return "Apply these moves? [y/N] or enter a row number to edit its category: "

    def preview(self) -> Preview:
        return Preview(sections=[Section(
            headers=["#", "Original", "Category", "Renamed to", "Description"],
            rows=[
                (i + 1, p.original_name, p.category, p.filename, truncate(p.summary))
                for i, p in enumerate(self.proposals)
            ],
            detail_last=True,
        )])

    def edit(self, index: int) -> bool:
        """Retarget one book's category, then let the driver re-show the table."""
        if not 0 <= index < len(self.proposals):
            return False
        p = self.proposals[index]
        print(f"  Book     : {p.filename}")
        print(f"  Category : {p.category}")
        new_category = input("  New category: ").strip()
        if new_category:
            # Proposal is frozen — replace the entry so db_value stays in step.
            self.proposals[index] = p.with_category(new_category)
        return True

    def stage(self, catalog: Catalog) -> None:
        catalog.stage(catalog.key(self.landing_dir), self.proposals, status="pending")

    def apply(self, catalog: Catalog) -> Outcome:
        key = catalog.key(self.landing_dir)
        ok = errors = 0
        for p in self.proposals:
            src = self.landing_dir / p.original_name
            dst_dir = self.data_dir / p.category
            dst = dst_dir / p.filename
            dst_dir.mkdir(exist_ok=True)

            if dst.exists():
                print(f"  Skipping — destination already exists: {p.db_value}")
                errors += 1
                continue
            try:
                src.rename(dst)
                # Re-key to the destination so it becomes an ordinary shelved
                # row, leaving no __landing leftovers and keeping the summary.
                catalog.rekey(key, p.original_name, catalog.key(dst_dir), dst.name)
                ok += 1
            except OSError as e:
                print(f"  Error moving '{src.name}': {e}")
                errors += 1
        catalog.commit()
        return Outcome(ok, errors, f"Done. {ok} moved, {errors} error(s).")


def build(ctx: Context, category: str, fresh: bool, publisher: str) -> ShelvePlan:
    landing = ctx.config.landing_dir
    data_dir = ctx.config.data_dir
    landing.mkdir(parents=True, exist_ok=True)

    all_pdfs = pdf_names(landing)
    if not all_pdfs:
        return ShelvePlan([], landing, data_dir,
                          empty_message=f"No PDF files in {landing.name}/.")

    existing = categories(data_dir)
    forced = category.strip()

    if fresh:
        cleared = ctx.catalog.clear_pending(ctx.catalog.key(landing))
        if cleared:
            ctx.out(f"Cleared {cleared} previously proposed move(s); starting fresh.")

    found = reconcile(ctx.catalog, landing, all_pdfs)

    # A forced --category overrides whatever a pending proposal stored, so the
    # whole batch is filed consistently.
    pending = [p.with_category(forced) if forced else p for p in found.pending]
    proposals: list[Proposal] = list(pending)

    if pending:
        ctx.out(f"Re-showing {len(pending)} previously proposed move(s).")

    if found.unknown:
        if found.applied:
            ctx.out(f"Skipping {found.applied} already-applied file(s).")

        if forced:
            ctx.out(f"Looking up titles for {len(found.unknown)} new file(s); "
                    f"filing all into '{forced}'…\n")
            results = ctx.lookup.titles(found.stems, publisher)
        else:
            ctx.out(f"Looking up titles and categories for {len(found.unknown)} new file(s)…\n")
            results = ctx.lookup.categorized_titles(
                found.stems, existing, sample_titles(data_dir, existing))

        if not results:
            raise PlanError(
                "Could not retrieve titles. Check your OPENAI_API_KEY and try again."
            )

        titles = {r["original"]: r["title"] for r in results}
        cats = {r["original"]: r.get("category", "Uncategorized") for r in results}
        summaries = {r["original"]: r.get("summary", "") for r in results}

        for fname in found.unknown:
            stem = Path(fname).stem
            proposals.append(Proposal.from_title(
                original_name=fname,
                title=titles.get(stem, stem),
                summary=summaries.get(stem, ""),
                category=forced or cats.get(stem, "Uncategorized"),
            ))
    elif not pending:
        return ShelvePlan(
            [], landing, data_dir,
            empty_message=f"All {found.applied} file(s) already applied. Nothing to do.",
        )

    return ShelvePlan(proposals, landing, data_dir)
