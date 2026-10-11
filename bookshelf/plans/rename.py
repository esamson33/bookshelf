"""Rename books in place to their proper published titles."""

from __future__ import annotations

from pathlib import Path

from ..catalog import Catalog
from ..models import Proposal
from ..scan import collect_pdfs, reconcile
from .base import Context, Outcome, Plan, PlanError


class RenamePlan(Plan):
    def __init__(self, groups: list[tuple[Path, list[Proposal]]], root: Path,
                 empty_message: str = "No renames needed."):
        self.groups = groups
        self.root = root
        self.empty_message = empty_message

    def is_empty(self) -> bool:
        return not self.groups

    def _label(self, directory: Path) -> str:
        try:
            return str(directory.relative_to(self.root))
        except ValueError:
            return str(directory)

    def payload(self) -> dict:
        """Grouped by directory, because a recursive scan spans several."""
        return {
            "root": str(self.root),
            "groups": [
                {
                    "directory": self._label(directory),
                    "rows": [p.as_row() for p in proposals],
                }
                for directory, proposals in self.groups
            ],
        }

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
                    self.out(f"  Skipping — destination already exists: {dst.name}")
                    errors += 1
                    continue
                try:
                    src.rename(dst)
                    catalog.mark_applied(key, p.original_name)
                    ok += 1
                except OSError as e:
                    self.out(f"  Error renaming '{src.name}': {e}")
                    errors += 1
        catalog.commit()
        return Outcome(ok, errors, f"Done. {ok} renamed, {errors} error(s).")


def from_pending(ctx: Context, root: Path) -> RenamePlan:
    """Rebuild a plan from proposals already staged under `root`.

    Costs no API call: the review, and the apply that follows it, read the same
    `pending` rows the scan wrote. Subdirectories are swept in too, because a
    recursive scan stages a row under each directory it walked.
    """
    landing = ctx.config.landing_dir
    groups: list[tuple[Path, list[Proposal]]] = []
    for entry in ctx.catalog.pending_by_directory():
        directory = ctx.catalog.path(entry["directory"])
        if directory != root and root not in directory.parents:
            continue
        if directory == landing or landing in directory.parents:
            continue                      # the landing pad belongs to shelve
        proposals = [
            Proposal.from_db(r["original_name"], r["filename"], r["summary"],
                             row_id=r["id"])
            for r in ctx.catalog.pending_rows(entry["directory"])
        ]
        if proposals:
            groups.append((directory, proposals))
    return RenamePlan(groups, root)


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
        ctx.out(f"Looking up titles for {total_new} new file(s)…")

        stems = [Path(f).stem for _, files in new_groups for f in files]
        results = ctx.lookup.titles(stems, publisher)
        # A row without both keys cannot be matched to a file; leaving it out
        # means that file falls back to its own name rather than crashing.
        results = [r for r in results
                   if isinstance(r.get("original"), str) and isinstance(r.get("title"), str)]
        if not results:
            # API failures (bad key, no credit, network) raise before here;
            # reaching this means the model answered with nothing usable.
            raise PlanError(
                "The AI replied, but its answer had no usable titles. "
                "Try the scan again; the reply is in the server's error log."
            )

        # Matched on the trimmed name: the model drops stray spaces, so a file
        # like "Book (EX200) .pdf" would otherwise miss its own answer.
        titles = {r["original"].strip(): r["title"] for r in results}
        summaries = {r["original"].strip(): r.get("summary", "") for r in results}

        already_correct: list[tuple[Path, list[Proposal]]] = []
        for directory, files in new_groups:
            correct, to_rename = [], []
            for fname in files:
                stem = Path(fname).stem
                proposal = Proposal.from_title(
                    original_name=fname,
                    title=titles.get(stem.strip(), stem),
                    summary=summaries.get(stem.strip(), ""),
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
