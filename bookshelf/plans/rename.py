"""Rename books in place to their proper published titles."""

from __future__ import annotations

from pathlib import Path

from ..catalog import Catalog
from ..models import Proposal
from ..render import truncate
from ..scan import collect_pdfs, reconcile
from .base import Context, Outcome, Plan, PlanError, Preview, Section


class RenamePlan(Plan):
    abort_message = "Aborted. Proposals are saved — re-run to apply them."

    def __init__(self, groups: list[tuple[Path, list[Proposal]]], root: Path,
                 empty_message: str = "No renames needed."):
        self.groups = groups
        self.root = root
        self.empty_message = empty_message

    @property
    def row_count(self) -> int:
        return sum(len(p) for _, p in self.groups)

    def is_empty(self) -> bool:
        return not self.groups

    def confirm_prompt(self) -> str:
        return "Apply these renames? [y/N] "

    def preview(self) -> Preview:
        # A directory header only earns its line when more than one is shown.
        multi = len(self.groups) > 1
        sections = []
        counter = 1
        for directory, proposals in self.groups:
            heading = ""
            if multi:
                try:
                    label = directory.relative_to(self.root)
                except ValueError:
                    label = directory
                heading = f"\n{label}/"
            sections.append(Section(
                heading=heading,
                headers=["#", "Original", "Renamed to", "Description"],
                rows=[
                    (counter + i, p.original_name, p.filename, truncate(p.summary))
                    for i, p in enumerate(proposals)
                ],
                detail_last=True,
            ))
            counter += len(proposals)
        return Preview(sections=sections)

    def stage(self, catalog: Catalog) -> None:
        for directory, proposals in self.groups:
            catalog.stage(catalog.key(directory), proposals, status="pending")

    def apply(self, catalog: Catalog) -> Outcome:
        ok = errors = 0
        for directory, proposals in self.groups:
            key = catalog.key(directory)
            for p in proposals:
                src = directory / p.original_name
                dst = directory / p.filename
                if dst.exists():
                    print(f"  Skipping — destination already exists: {dst.name}")
                    errors += 1
                    continue
                try:
                    src.rename(dst)
                    catalog.mark_applied(key, p.original_name)
                    ok += 1
                except OSError as e:
                    print(f"  Error renaming '{src.name}': {e}")
                    errors += 1
        catalog.commit()
        return Outcome(ok, errors, f"Done. {ok} renamed, {errors} error(s).")


def build(ctx: Context, root: Path, recursive: bool, publisher: str) -> RenamePlan:
    """Scan `root`, resurface pending proposals, look up whatever is new."""
    dir_groups = collect_pdfs(root, recursive)
    if not dir_groups:
        return RenamePlan([], root, empty_message="No PDF files found.")

    new_groups: list[tuple[Path, list[str]]] = []
    pending_groups: list[tuple[Path, list[Proposal]]] = []
    total_skipped = 0

    for directory, all_pdfs in dir_groups:
        found = reconcile(ctx.catalog, directory, all_pdfs)
        total_skipped += found.applied
        if found.pending:
            pending_groups.append((directory, found.pending))
        if found.unknown:
            new_groups.append((directory, found.unknown))

    # Pending proposals need no API call — show them first.
    groups: list[tuple[Path, list[Proposal]]] = list(pending_groups)
    total_pending = sum(len(p) for _, p in pending_groups)
    if total_pending:
        ctx.out(f"Re-showing {total_pending} previously proposed rename(s).")

    total_new = sum(len(f) for _, f in new_groups)
    if total_new:
        if total_skipped:
            ctx.out(f"Skipping {total_skipped} already-applied file(s).")
        ctx.out(f"Looking up titles for {total_new} new file(s)…\n")

        stems = [Path(f).stem for _, files in new_groups for f in files]
        results = ctx.lookup.titles(stems, publisher)
        if not results:
            raise PlanError(
                "Could not retrieve titles from OpenAI. "
                "Check your OPENAI_API_KEY and try again."
            )

        titles = {r["original"]: r["title"] for r in results}
        summaries = {r["original"]: r.get("summary", "") for r in results}

        already_correct: list[tuple[Path, list[Proposal]]] = []
        for directory, files in new_groups:
            correct, to_rename = [], []
            for fname in files:
                stem = Path(fname).stem
                proposal = Proposal.from_title(
                    original_name=fname,
                    title=titles.get(stem, stem),
                    summary=summaries.get(stem, ""),
                )
                (correct if proposal.is_noop else to_rename).append(proposal)
            if correct:
                already_correct.append((directory, correct))
            if to_rename:
                groups.append((directory, to_rename))

        # Files already named correctly are recorded without a confirmation.
        total_correct = sum(len(p) for _, p in already_correct)
        for directory, proposals in already_correct:
            ctx.catalog.stage(ctx.catalog.key(directory), proposals, status="applied")
        if total_correct:
            ctx.out(f"Auto-tracked {total_correct} already correctly-named file(s).")
    elif not pending_groups and total_skipped:
        return RenamePlan(
            [], root,
            empty_message=f"All {total_skipped} file(s) already applied. Nothing to do.",
        )

    return RenamePlan(groups, root)
