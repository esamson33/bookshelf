"""Remove duplicate PDFs across categories and drop catalog rows with no file."""

from __future__ import annotations

import os
from pathlib import Path

from ..catalog import Catalog
from ..models import Duplicate, StaleRow
from ..scan import is_pdf
from .base import Context, Outcome, Plan

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

    def payload(self) -> dict:
        return {
            "duplicates": [
                {
                    "filename": d.filename,
                    "keep": self._rel(d.keep),
                    "remove": [
                        {
                            "path": self._rel(path),
                            # A duplicate can sit on disk with no catalog row
                            # at all; only the tracked ones cost a DB delete.
                            "tracked": (str(path.parent), d.filename) in self.tracked,
                        }
                        for path in d.remove
                    ],
                }
                for d in self.duplicates
            ],
            "stale": [
                {"id": s.row_id, "path": self._rel(s.path)} for s in self.stale
            ],
            "file_count": self.file_count,
            "record_count": self.record_count,
        }

    def select(self, filenames=None, include_stale: bool = True) -> "DedupePlan":
        """A copy narrowed to what the user actually ticked.

        The CLI's prompt was all-or-nothing; a page can offer each duplicate
        group and the stale rows separately, so apply must be able to act on a
        subset without rebuilding the scan.
        """
        chosen = self.duplicates if filenames is None else [
            d for d in self.duplicates if d.filename in set(filenames)
        ]
        return DedupePlan(chosen, self.stale if include_stale else [],
                          self.root, self.tracked)

    def apply(self, catalog: Catalog) -> Outcome:
        deleted = errors = 0
        for d in self.duplicates:
            for path in d.remove:
                try:
                    path.unlink()
                    deleted += 1
                except OSError as e:
                    self.out(f"  Error deleting '{path}': {e}")
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
        StaleRow(row_id, directory, filename)
        for (directory, filename), (row_id, _) in index.items()
        if str(Path(directory) / filename) not in on_disk
    ]

    return DedupePlan(duplicates, stale, root, tracked=set(index))
