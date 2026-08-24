"""Remove duplicate PDFs across categories and drop catalog rows with no file."""

from __future__ import annotations

import os
from pathlib import Path

from ..catalog import Catalog
from ..models import Duplicate, StaleRow
from ..scan import is_pdf
from .base import Context, Outcome, Plan, Preview, Section

#: Sorts after any real timestamp, so untracked copies are never the one kept.
NEVER_APPLIED = "9999-99-99"


class DedupePlan(Plan):
    empty_message = "No duplicates or stale records found."

    def __init__(self, duplicates: list[Duplicate], stale: list[StaleRow],
                 root: Path, tracked: set[tuple[str, str]]):
        self.duplicates = duplicates
        self.stale = stale
        self.root = root
        self.tracked = tracked

    def is_empty(self) -> bool:
        return not self.duplicates and not self.stale

    @property
    def file_count(self) -> int:
        return sum(len(d.remove) for d in self.duplicates)

    @property
    def record_count(self) -> int:
        return sum(len(d.remove_db_ids) for d in self.duplicates) + len(self.stale)

    def _rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    def preview(self) -> Preview:
        sections = []
        if self.duplicates:
            lines = []
            for d in self.duplicates:
                lines.append(f"  {d.filename}")
                lines.append(f"    KEEP   {self._rel(d.keep)}")
                for path in d.remove:
                    note = "  (+ DB record)" if (str(path.parent), d.filename) in self.tracked else ""
                    lines.append(f"    REMOVE {self._rel(path)}{note}")
            sections.append(Section(
                heading=f"Filesystem duplicates ({self.file_count} file(s) to remove):\n",
                lines=lines + [""],
            ))
        if self.stale:
            sections.append(Section(
                heading=f"Stale DB records ({len(self.stale)} row(s) with no file on disk):\n",
                lines=[f"  {self._rel(s.path)}" for s in self.stale] + [""],
            ))
        return Preview(
            sections=sections,
            footer=f"Will delete {self.file_count} file(s) and {self.record_count} DB record(s).",
        )

    def apply(self, catalog: Catalog) -> Outcome:
        deleted = errors = 0
        for d in self.duplicates:
            for path in d.remove:
                try:
                    path.unlink()
                    deleted += 1
                except OSError as e:
                    print(f"  Error deleting '{path}': {e}")
                    errors += 1
            catalog.delete_ids(d.remove_db_ids)
        catalog.delete_ids(s.row_id for s in self.stale)
        return Outcome(deleted, errors,
                       f"Done. {deleted} file(s) deleted, {errors} error(s).")


def build(ctx: Context) -> DedupePlan:
    root = ctx.config.data_dir

    by_name: dict[str, list[Path]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "_")))
        for fname in sorted(filenames):
            if is_pdf(fname):
                by_name.setdefault(fname, []).append(Path(dirpath) / fname)

    # Index applied rows by absolute directory, matching the walked paths.
    index = {
        (str(ctx.catalog.path(key)), name): entry
        for (key, name), entry in ctx.catalog.applied_index().items()
    }

    duplicates = []
    for fname, paths in sorted(by_name.items()):
        if len(paths) < 2:
            continue

        def sort_key(p: Path, _fname=fname) -> tuple[str, str]:
            entry = index.get((str(p.parent), _fname))
            return (entry[1] if entry else NEVER_APPLIED, str(p))

        keep, *remove = sorted(paths, key=sort_key)
        duplicates.append(Duplicate(
            filename=fname,
            keep=keep,
            remove=tuple(remove),
            remove_db_ids=tuple(
                index[(str(p.parent), fname)][0]
                for p in remove
                if (str(p.parent), fname) in index
            ),
        ))

    on_disk = {str(p) for paths in by_name.values() for p in paths}
    stale = [
        StaleRow(row_id, directory, proposed_name)
        for (directory, proposed_name), (row_id, _) in index.items()
        if str(Path(directory) / proposed_name) not in on_disk
    ]

    return DedupePlan(duplicates, stale, root, tracked=set(index))
