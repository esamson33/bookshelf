"""The proposal record shared by every mode.

The original script passed untyped dicts around and overloaded
the stored name column: for a shelved book it held "Title.pdf", but for one still
in the landing pad it held "Category/Title.pdf". That fiction was re-parsed at
three separate call sites (splitting with Path().parent/.name, with
.partition("/"), and re-joining with an f-string), and a missing key raised
KeyError deep inside an apply loop.

Proposal keeps the category as its own field. `db_value` reproduces the stored
format exactly, so the on-disk schema is unchanged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

PDF_SUFFIX = ".pdf"

#: How many category suggestions a looked-up book keeps.
MAX_CANDIDATES = 3


@dataclass(frozen=True)
class Candidate:
    """One category the model suggested for a book, and how well it fits.

    `score` is the model's own 0–100 rating of the fit — useful for ranking
    the suggestions against each other, not a calibrated probability. It is
    None when the reply gave no usable number.
    """

    category: str
    score: int | None = None

    def as_dict(self) -> dict:
        return {"category": self.category, "score": self.score}


def parse_candidates(raw) -> tuple[Candidate, ...]:
    """Normalize suggestions from a model reply or a stored JSON column.

    Accepts a list of {"category", "score"} dicts (or the JSON text of one).
    Drops entries with no usable name or a '/' in it (a category is a single
    directory), merges case-insensitive repeats, clamps scores to 0–100, and
    keeps the best MAX_CANDIDATES, highest score first.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return ()
    if not isinstance(raw, list):
        return ()

    seen: set[str] = set()
    found: list[Candidate] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("category") or "").strip()
        if not name or "/" in name or name.lower() in seen:
            continue
        seen.add(name.lower())
        found.append(Candidate(name, _score(item.get("score"))))

    # Stable sort: equal (or missing) scores keep the model's own order.
    found.sort(key=lambda c: -1 if c.score is None else c.score, reverse=True)
    return tuple(found[:MAX_CANDIDATES])


def _score(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return max(0, min(100, round(float(value))))
    except (TypeError, ValueError):
        return None


def candidates_json(candidates: tuple[Candidate, ...]) -> str | None:
    """The `candidates` column value: JSON, or NULL when there are none."""
    return json.dumps([c.as_dict() for c in candidates]) if candidates else None


@dataclass(frozen=True)
class Proposal:
    """One proposed rename or move.

    `filename` is the destination name including its extension — stored
    verbatim rather than rebuilt from a title, so a name like "Book v2.pdf"
    round-trips through the DB unchanged. `category` is set only for books
    being filed out of the landing pad.
    """

    original_name: str
    filename: str
    summary: str = ""
    category: str | None = None
    #: The catalog row this was read back from, when it was. A proposal that
    #: has only just been looked up has no row yet; the web UI edits and drops
    #: proposals by id, so one rebuilt from the DB carries it.
    row_id: int | None = None
    #: The model's category suggestions, best first, for the review to offer
    #: as alternatives. Empty when the category was forced or for renames.
    candidates: tuple[Candidate, ...] = ()

    def __post_init__(self):
        # summary is always a string: "" means "looked up, none available".
        # A NULL summary means "never attempted" and belongs only to rows
        # predating the column, which missing_summaries() picks up for
        # backfill — a Proposal never writes one.
        if self.summary is None:
            object.__setattr__(self, "summary", "")

    @property
    def title(self) -> str:
        """Display title — the filename without its extension."""
        return Path(self.filename).stem

    @property
    def db_value(self) -> str:
        """The `filename` column value for this proposal."""
        return f"{self.category}/{self.filename}" if self.category else self.filename

    @property
    def is_noop(self) -> bool:
        """True when the file is already named correctly."""
        return self.category is None and self.filename == self.original_name

    def with_category(self, category: str) -> "Proposal":
        return replace(self, category=category)

    @classmethod
    def from_title(
        cls,
        original_name: str,
        title: str,
        summary: str = "",
        category: str | None = None,
        candidates: tuple[Candidate, ...] = (),
    ) -> "Proposal":
        """Build from a looked-up title (no extension)."""
        return cls(
            original_name=original_name,
            filename=f"{title}{PDF_SUFFIX}",
            summary=summary or "",
            category=category,
            candidates=candidates,
        )

    @classmethod
    def from_db(
        cls, original_name: str, stored_name: str, summary: str = "",
        row_id: int | None = None, candidates: str | None = None,
    ) -> "Proposal":
        """Rebuild from a stored row, splitting off a category if present.

        Filenames cannot contain '/', so the first separator — and there is at
        most one — always divides category from filename.
        """
        category, sep, filename = (stored_name or "").partition("/")
        if not sep:
            category, filename = None, category
        return cls(
            original_name=original_name,
            filename=filename,
            summary=summary or "",
            category=category,
            row_id=row_id,
            candidates=parse_candidates(candidates) if candidates else (),
        )

    def with_title(self, title: str) -> "Proposal":
        """Retarget the destination name, keeping the extension convention."""
        return replace(self, filename=f"{title}{PDF_SUFFIX}")

    def as_row(self) -> dict:
        """The JSON shape the web UI renders and edits."""
        return {
            "id": self.row_id,
            "original": self.original_name,
            "title": self.title,
            "proposed": self.filename,
            "category": self.category,
            "summary": self.summary,
            "candidates": [c.as_dict() for c in self.candidates],
        }


@dataclass(frozen=True)
class Duplicate:
    """One book found more than once on the shelf.

    Copies are grouped when they share a filename or have identical contents
    — the same PDF filed twice under different looked-up titles is still one
    book. `filename` identifies the group (the default kept copy's name; no
    two groups can share one). `keep` is the copy with the earliest
    applied_at (untracked copies sort last); `remove` are the rest.
    `remove_db_ids` covers only those copies that actually have a catalog row
    — a duplicate can exist on disk with no row at all. `match` says why the
    copies were grouped — "name", "content", "title", or several joined with
    " and ". A group linked by title alone holds different files (another
    edition, another scan), so it needs a person's review before anything
    in it is deleted.
    """

    filename: str
    keep: Path
    remove: tuple[Path, ...]
    remove_db_ids: tuple[int, ...] = ()
    match: str = "name"

    @property
    def needs_review(self) -> bool:
        """True when the copies were matched on title, not as the same file."""
        return "title" in self.match.split(" and ")


@dataclass(frozen=True)
class StaleRow:
    """An applied catalog row whose file is no longer on disk."""

    row_id: int
    directory: str
    filename: str

    @property
    def path(self) -> Path:
        return Path(self.directory) / self.filename
