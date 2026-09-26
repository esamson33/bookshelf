"""The shape every mode shares: describe what would change, then change it.

All four modes were the same pipeline written four times inside a 337-line
main(), separated by sys.exit() as control flow. A Plan describes what would
change and knows how to apply it.

The preview used to be a terminal table and the confirmation a blocking
input(); both now belong to the browser, so what a Plan hands back is JSON
(`payload`) and what it writes progress through is `out`. Editing a proposal
went with them: staged proposals live in the catalog as `pending` rows, so
retargeting one is a row update, not a method on an object that only exists
for the length of a request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..catalog import Catalog
from ..config import Config
from ..lookup import LookupClient


class PlanError(Exception):
    """A hard failure while building a plan; the caller reports it."""


@dataclass
class Context:
    """What a plan builder needs, threaded explicitly instead of via globals."""

    config: Config
    catalog: Catalog
    lookup: LookupClient
    out: Callable[..., None] = print


@dataclass
class Outcome:
    ok: int = 0
    errors: int = 0
    summary: str = ""

    def as_dict(self) -> dict:
        return {"ok": self.ok, "errors": self.errors, "summary": self.summary}


class Plan:
    """Base class with the defaults most plans keep.

    Subclasses override payload()/apply() and, where the mode stages work for
    later, stage().
    """

    #: Shown instead of the payload when there is nothing to do.
    empty_message: str = "Nothing to do."

    #: Where per-file progress and errors go. Assigned a job's `report` when
    #: the plan runs in the background, so failures reach the page instead of
    #: the server console.
    out: Callable[..., None] = print

    def is_empty(self) -> bool:
        raise NotImplementedError

    def payload(self) -> dict:
        """What the UI renders: rows to review, plus whatever framing they need."""
        return {}

    def stage(self, catalog: Catalog) -> None:
        """Persist pending proposals so the review survives the request."""

    def apply(self, catalog: Catalog) -> Outcome:
        raise NotImplementedError
