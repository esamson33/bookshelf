"""The shape every mode shares: preview, confirm, apply.

All four modes were the same pipeline written four times inside a 337-line
main(), separated by sys.exit() as control flow. A Plan describes what would
change and knows how to apply it; bookshelf.driver runs the confirmation loop
once, for all of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Sequence

from ..catalog import Catalog
from ..config import Config
from ..lookup import LookupClient


class PlanError(Exception):
    """A hard failure while building a plan; the CLI reports it and exits 1."""


@dataclass
class Context:
    """What a plan builder needs, threaded explicitly instead of via globals."""

    config: Config
    catalog: Catalog
    lookup: LookupClient
    out: Callable[..., None] = print


@dataclass
class Section:
    """One block of a preview: a table, or free-form lines, or both."""

    heading: str = ""
    headers: Sequence[str] = ()
    rows: Sequence[Sequence[object]] = ()
    lines: Sequence[str] = ()
    detail_last: bool = False


@dataclass
class Preview:
    sections: list[Section] = field(default_factory=list)
    footer: str = ""


@dataclass
class Outcome:
    ok: int = 0
    errors: int = 0
    summary: str = ""


class Plan:
    """Base class with the defaults most plans keep.

    Subclasses override preview()/apply() and, where the mode supports it,
    edit() and stage().
    """

    #: Shown instead of the preview when there is nothing to do.
    empty_message: str = "Nothing to do."
    #: Shown when the user declines.
    abort_message: str = "Aborted."
    #: False for modes that just run (summaries), skipping preview and prompt.
    confirm_required: bool = True
    #: True only where a row number at the prompt means something. Where it is
    #: False the prompt is strictly y-or-abort, matching the original: any
    #: other answer aborts rather than re-prompting.
    supports_edit: bool = False

    def is_empty(self) -> bool:
        raise NotImplementedError

    def preview(self) -> Preview:
        return Preview()

    def confirm_prompt(self) -> str:
        return "Proceed? [y/N] "

    #: Number of rows the preview shows; used for the invalid-row message.
    row_count: int = 0

    def edit(self, index: int) -> bool:
        """Handle a row number typed at the prompt. False = not supported."""
        return False

    def stage(self, catalog: Catalog) -> None:
        """Persist pending proposals so a declined run can be resumed."""

    def apply(self, catalog: Catalog) -> Outcome:
        raise NotImplementedError
