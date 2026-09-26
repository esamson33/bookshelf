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

from dataclasses import dataclass, replace
from pathlib import Path

PDF_SUFFIX = ".pdf"


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
    ) -> "Proposal":
        """Build from a looked-up title (no extension)."""
        return cls(
            original_name=original_name,
            filename=f"{title}{PDF_SUFFIX}",
            summary=summary or "",
            category=category,
        )

    @classmethod
    def from_db(
        cls, original_name: str, stored_name: str, summary: str = "",
        row_id: int | None = None,
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
        }


@dataclass(frozen=True)
class Duplicate:
    """One filename found in more than one category directory.

    `keep` is the copy with the earliest applied_at (untracked copies sort
    last); `remove` are the rest. `remove_db_ids` covers only those copies
    that actually have a catalog row — a duplicate can exist on disk with no
    row at all.
    """

    filename: str
    keep: Path
    remove: tuple[Path, ...]
    remove_db_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class StaleRow:
    """An applied catalog row whose file is no longer on disk."""

    row_id: int
    directory: str
    filename: str

    @property
    def path(self) -> Path:
        return Path(self.directory) / self.filename
