"""Remove duplicate PDFs across categories and drop catalog rows with no file."""

from __future__ import annotations

import hashlib
import os
import re
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
                 root: Path, index: dict[tuple[str, str], tuple[int, str]]):
        self.duplicates = duplicates
        self.stale = stale
        self.root = root
        #: {(absolute directory, filename): (row id, applied_at)} for every
        #: shelved row — what a copy's "tracked" flag and filing date come from.
        self.index = index

    @property
    def tracked(self) -> set[tuple[str, str]]:
        return set(self.index)

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

    def _entry(self, path: Path):
        return self.index.get((str(path.parent), path.name))

    def _copy(self, path: Path) -> dict:
        entry = self._entry(path)
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        return {
            "path": self._rel(path),
            "name": path.name,
            "category": path.parent.name,
            "size": size,
            # The catalog row, so the page can open this copy to compare.
            "id": entry[0] if entry else None,
            # When the copy was filed; None for a file the catalog never saw.
            "added": entry[1] or None if entry else None,
            "tracked": entry is not None,
        }

    def payload(self) -> dict:
        return {
            "duplicates": [
                {
                    "filename": d.filename,
                    "match": d.match,
                    "review": d.needs_review,
                    # Every copy, earliest filed first, for the page to offer
                    # as the one to keep; `keep` is the default choice.
                    "copies": [self._copy(p)
                               for p in sorted((d.keep, *d.remove), key=self._order)],
                    "keep": self._rel(d.keep),
                    "remove": [
                        {
                            "path": self._rel(path),
                            # A duplicate can sit on disk with no catalog row
                            # at all; only the tracked ones cost a DB delete.
                            "tracked": self._entry(path) is not None,
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

    def _order(self, p: Path) -> tuple[str, str]:
        return _sort_key(self.index, p)

    def keeping(self, d: Duplicate, rel_path: str) -> Duplicate:
        """`d` with the copy at `rel_path` kept and every other copy removed.

        Raises ValueError when `rel_path` is not one of the group's copies —
        a stale page must not be able to name a file outside the group.
        """
        copies = (d.keep, *d.remove)
        matches = [p for p in copies if self._rel(p) == rel_path]
        if not matches:
            raise ValueError(f"'{rel_path}' is not a copy of {d.filename}.")
        keep = matches[0]
        remove = tuple(p for p in copies if p != keep)
        return Duplicate(
            filename=d.filename, keep=keep, remove=remove, match=d.match,
            remove_db_ids=_row_ids(self.index, remove),
        )

    def select(self, filenames=None, include_stale: bool = True,
               keep: dict[str, str] | None = None) -> "DedupePlan":
        """A copy narrowed to what the user actually ticked.

        The CLI's prompt was all-or-nothing; a page can offer each duplicate
        group and the stale rows separately, so apply must be able to act on a
        subset without rebuilding the scan. `keep` maps a group's filename to
        the relative path of the copy the user chose to keep; a group it does
        not mention keeps the earliest-filed copy.
        """
        # "Every group" never includes one matched on title alone: those hold
        # different files, so they are only touched when named explicitly.
        chosen = [d for d in self.duplicates if not d.needs_review] if filenames is None else [
            d for d in self.duplicates if d.filename in set(filenames)
        ]
        keep = keep or {}
        chosen = [self.keeping(d, keep[d.filename]) if d.filename in keep else d
                  for d in chosen]
        return DedupePlan(chosen, self.stale if include_stale else [],
                          self.root, self.index)

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


def _sort_key(index, p: Path) -> tuple[str, str]:
    """Earliest filed first; copies the catalog never saw sort last."""
    entry = index.get((str(p.parent), p.name))
    return ((entry[1] or NEVER_APPLIED) if entry else NEVER_APPLIED, str(p))


def _row_ids(index, paths) -> tuple[int, ...]:
    return tuple(index[(str(p.parent), p.name)][0]
                 for p in paths if (str(p.parent), p.name) in index)


def _digest(path: Path) -> str | None:
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


_ARTICLE = re.compile(r"^(the|an|a) ")


def _title_key(text: str) -> str:
    """A title reduced for comparison: case, punctuation and a leading article
    ignored, so "An Introduction to X" and "Introduction to x" agree."""
    words = re.sub(r"[^0-9a-z]+", " ", text.lower()).strip()
    return _ARTICLE.sub("", words)


def _title_keys(path: Path) -> tuple[str, str]:
    """(whole title, main title) — the main title drops a ": subtitle"."""
    stem = path.stem
    return _title_key(stem), _title_key(stem.split(":", 1)[0])


def _groups(paths: list[Path]) -> list[tuple[list[Path], str]]:
    """Partition PDFs into copies of the same book, with why they matched.

    Two files are the same book when they share a filename, when their
    contents are identical, or when their titles agree — the same title, or
    one is the other plus a ": subtitle" ("Computer Graphics from Scratch"
    and "Computer Graphics from Scratch: A Programmer's Introduction…").
    Two titles that merely share a main title ("Python: A" and "Python: B")
    do not match. Contents are only hashed for files whose size matches
    another file's, so a scan of a large shelf reads almost nothing.
    """
    parent = {p: p for p in paths}

    def find(p):
        while parent[p] != p:
            parent[p] = parent[parent[p]]
            p = parent[p]
        return p

    def union(a, b):
        parent[find(a)] = find(b)

    by_name: dict[str, list[Path]] = {}
    by_size: dict[int, list[Path]] = {}
    for p in paths:
        by_name.setdefault(p.name, []).append(p)
        try:
            by_size.setdefault(p.stat().st_size, []).append(p)
        except OSError:
            pass

    for same in by_name.values():
        for other in same[1:]:
            union(same[0], other)

    content_linked: set[Path] = set()
    for same_size in by_size.values():
        if len(same_size) < 2:
            continue
        by_digest: dict[str, list[Path]] = {}
        for p in same_size:
            digest = _digest(p)
            if digest:
                by_digest.setdefault(digest, []).append(p)
        for same in by_digest.values():
            # Copies that also share a name are already grouped by name; only
            # a differently named twin makes this a content match.
            if len({p.name for p in same}) > 1:
                content_linked.update(same)
            for other in same[1:]:
                union(same[0], other)

    # Title matches link only what a name or content match did not already:
    # they are a weaker signal, and the group's label should say so.
    title_linked: set[Path] = set()
    by_title: dict[str, list[Path]] = {}
    for p in paths:
        by_title.setdefault(_title_keys(p)[0], []).append(p)
    for p in paths:
        whole, main = _title_keys(p)
        if not main:
            continue
        for other in by_title.get(whole, []) + (by_title.get(main, []) if main != whole else []):
            if other is p or other.name == p.name or find(other) == find(p):
                continue
            title_linked.update((p, other))
            union(p, other)

    groups: dict[Path, list[Path]] = {}
    for p in paths:
        groups.setdefault(find(p), []).append(p)

    result = []
    for members in groups.values():
        if len(members) < 2:
            continue
        reasons = []
        if len({p.name for p in members}) < len(members):
            reasons.append("name")
        if any(p in content_linked for p in members):
            reasons.append("content")
        if any(p in title_linked for p in members):
            reasons.append("title")
        result.append((members, " and ".join(reasons) or "name"))
    return result


def build(ctx: Context) -> DedupePlan:
    root = ctx.config.data_dir

    pdfs: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "_")))
        for fname in sorted(filenames):
            if is_pdf(fname):
                pdfs.append(Path(dirpath) / fname)

    # Index applied rows by absolute directory, matching the walked paths.
    index = {
        (str(ctx.catalog.path(key)), name): entry
        for (key, name), entry in ctx.catalog.applied_index().items()
    }

    duplicates = []
    for members, match in _groups(pdfs):
        keep, *remove = sorted(members, key=lambda p: _sort_key(index, p))
        duplicates.append(Duplicate(
            filename=keep.name,
            keep=keep,
            remove=tuple(remove),
            remove_db_ids=_row_ids(index, remove),
            match=match,
        ))
    duplicates.sort(key=lambda d: d.filename.lower())

    on_disk = {str(p) for p in pdfs}
    stale = [
        StaleRow(row_id, directory, filename)
        for (directory, filename), (row_id, _) in index.items()
        if str(Path(directory) / filename) not in on_disk
    ]

    return DedupePlan(duplicates, stale, root, index)
