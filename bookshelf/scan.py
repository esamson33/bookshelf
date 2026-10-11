"""Filesystem scanning and reconciliation against the catalog.

`reconcile` replaces the applied/pending/new partition that the original
script wrote twice — once for the landing pad and once for the rename path —
in two shapes that had drifted apart.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .catalog import Catalog
from .models import PDF_SUFFIX, Proposal


def is_pdf(name: str) -> bool:
    return name.lower().endswith(PDF_SUFFIX)


def pdf_names(directory: Path) -> list[str]:
    """Sorted PDF filenames directly inside `directory`."""
    if not directory.is_dir():
        return []
    return sorted(f.name for f in directory.iterdir() if f.is_file() and is_pdf(f.name))


def collect_pdfs(root: Path, recursive: bool) -> list[tuple[Path, list[str]]]:
    """(directory, [pdf_filenames]) pairs, for the root alone or the whole tree."""
    if not recursive:
        pdfs = pdf_names(root)
        return [(root, pdfs)] if pdfs else []

    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        pdfs = sorted(f for f in filenames if is_pdf(f))
        if pdfs:
            results.append((Path(dirpath), pdfs))
    return results


def categories(data_dir: Path) -> list[str]:
    """Sorted category directory names, excluding hidden and helper dirs.

    A category left with no PDFs (its last book deduplicated or archived away)
    is not offered: its directory stays on disk, but it is left out of the
    category list and the lookup prompt until a book is filed into it again.
    """
    if not data_dir.is_dir():
        return []
    return sorted(
        d.name for d in data_dir.iterdir()
        if d.is_dir() and not d.name.startswith((".", "_")) and pdf_names(d)
    )


def sample_titles(data_dir: Path, names: list[str], n: int = 5) -> dict[str, list[str]]:
    """{category: [title_stem, ...]} — anchors that help the model classify."""
    samples: dict[str, list[str]] = {}
    for category in names:
        stems = [Path(f).stem for f in pdf_names(data_dir / category)]
        samples[category] = stems[:n]
    return samples


@dataclass
class Reconciled:
    """How one directory's files line up against what the catalog knows."""

    directory: Path
    applied: int = 0
    pending: list[Proposal] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    @property
    def stems(self) -> list[str]:
        """Filename stems for the unknown files, ready for a lookup."""
        return [Path(f).stem for f in self.unknown]

    def __bool__(self) -> bool:
        return bool(self.pending or self.unknown)


def reconcile(catalog: Catalog, directory: Path, filenames: list[str]) -> Reconciled:
    """Split `filenames` into already-applied, already-proposed, and unknown.

    Files with a stored pending proposal come back as Proposals so they can be
    re-shown without another API call.
    """
    key = catalog.key(directory)
    applied = catalog.applied_names(key)
    pending = catalog.pending(key)

    return Reconciled(
        directory=directory,
        applied=sum(1 for f in filenames if f in applied),
        pending=[
            Proposal.from_db(f, pending[f][0], pending[f][1], candidates=pending[f][2])
            for f in filenames if f in pending
        ],
        unknown=[f for f in filenames if f not in applied and f not in pending],
    )
