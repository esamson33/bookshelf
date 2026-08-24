#!/usr/bin/env python3
"""
rename_books.py — Rename PDF books to their proper titles using OpenAI.

Usage:
    python rename_books.py <directory> [--publisher "Manning"] [--recursive]
    python rename_books.py --new [--category "Machine Learning and AI"] [--fresh]
    python rename_books.py --dedupe
    python rename_books.py --backfill-summaries

Options:
    --publisher, -p        Hint for the publisher (e.g. Manning, Packt, O'Reilly).
                           Helps find titles faster. Optional.
    --recursive, -r        Also process PDFs in subdirectories.
    --new                  Categorize and move PDFs from __landing/ into the
                           proper subject directory.
    --category, -c         With --new: file every landing book into this exact
                           category instead of letting the model choose.
    --fresh                With --new: discard previously proposed (pending)
                           moves and look everything up again from scratch.
    --dedupe               Find and remove duplicate PDFs across category
                           directories and clean stale DB records.
    --backfill-summaries   Fetch summaries for all applied DB records missing one.

The default (rename) mode will:
  1. Scan the directory (and subdirs if --recursive) for PDFs not yet in the DB.
  2. Look up their proper titles via OpenAI (with web search).
  3. Show a preview table for confirmation.
  4. Apply renames only after you confirm.
  5. Record every rename in the SQLite DB alongside this script (.rename_books.db).

Re-running on the same directory skips already-renamed files.

The --new, --dedupe, and --backfill-summaries modes don't require a directory
argument.

Layout: the app files (this script, .rename_books.db, .rename_books.toml, the
web UI, vendor/) live at the top level; the book categories and __landing/ live
under data/. DB `directory` keys are stored relative to data/.
"""

import os
import sys
import argparse
from pathlib import Path

from bookshelf.catalog import Catalog
from bookshelf.config import Config, ConfigError
from bookshelf.lookup import LookupClient
from bookshelf.render import table, truncate

# ── config ─────────────────────────────────────────────────────────────────
# Paths now come from bookshelf.config.Config rather than module constants, so
# they can be pointed elsewhere by tests and by --db / --root. CATALOG and
# LOOKUP are set up in main().
CONFIG      = Config.default()
DATA_DIR    = CONFIG.data_dir
LANDING_DIR = CONFIG.landing_dir

CATALOG: Catalog = None      # type: ignore
LOOKUP:  LookupClient = None  # type: ignore


def batch_progress(index: int, total: int, size: int) -> None:
    print(f"  Batch {index}/{total} ({size} books)…")


def apply_renames(directory: str, proposals: list[dict]) -> tuple[int, int]:
    dir_path = Path(directory)
    key      = CATALOG.key(dir_path)
    success = errors = 0
    for p in proposals:
        src = dir_path / p["original_name"]
        dst = dir_path / p["proposed_name"]
        if dst.exists():
            print(f"  Skipping — destination already exists: {dst.name}")
            errors += 1
            continue
        try:
            src.rename(dst)
            CATALOG.mark_applied(key, p["original_name"])
            success += 1
        except OSError as e:
            print(f"  Error renaming '{src.name}': {e}")
            errors += 1
    CATALOG.commit()
    return success, errors


def apply_moves(landing_dir: Path, root: Path, proposals: list[dict]) -> tuple[int, int]:
    """Move files from landing_dir into root/<category>/<title>.pdf."""
    key     = CATALOG.key(landing_dir)
    success = errors = 0
    for p in proposals:
        src     = landing_dir / p["original_name"]
        # proposed_name encodes "Category/Title.pdf"
        rel     = Path(p["proposed_name"])
        dst_dir = root / rel.parent
        dst     = dst_dir / rel.name

        dst_dir.mkdir(exist_ok=True)

        if dst.exists():
            print(f"  Skipping — destination already exists: {rel}")
            errors += 1
            continue
        try:
            src.rename(dst)
            # Re-key the record to its destination so it becomes an ordinary
            # shelved row (directory = category, proposed_name = title). This
            # leaves no leftover '__landing' rows while preserving the summary.
            CATALOG.rekey(key, p["original_name"], CATALOG.key(dst_dir), dst.name)
            success += 1
        except OSError as e:
            print(f"  Error moving '{src.name}': {e}")
            errors += 1
    CATALOG.commit()
    return success, errors


def find_duplicates(root: Path) -> tuple[list[dict], list[tuple[int, str, str]]]:
    """
    Returns:
      - duplicate groups: [{filename, keep: Path, remove: [Path], remove_db_ids: [int]}]
      - stale DB rows:    [(id, directory, proposed_name)] where the file no longer exists
    """
    # Scan all non-hidden, non-underscore subdirs for PDFs
    by_name: dict[str, list[Path]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "_")))
        for fname in sorted(filenames):
            if fname.lower().endswith(".pdf"):
                by_name.setdefault(fname, []).append(Path(dirpath) / fname)

    # Index applied rows by absolute directory, to match the paths walked above.
    db_index: dict[tuple[str, str], tuple[int, str]] = {
        (str(CATALOG.path(key)), name): entry
        for (key, name), entry in CATALOG.applied_index().items()
    }

    # Filesystem duplicates
    groups = []
    for fname, paths in sorted(by_name.items()):
        if len(paths) < 2:
            continue

        def sort_key(p: Path) -> tuple[str, str]:
            entry = db_index.get((str(p.parent), fname))
            return (entry[1] if entry else "9999-99-99", str(p))

        paths_sorted = sorted(paths, key=sort_key)
        keep, *remove = paths_sorted
        groups.append({
            "filename": fname,
            "keep":     keep,
            "remove":   remove,
            "remove_db_ids": [
                db_index[(str(p.parent), fname)][0]
                for p in remove
                if (str(p.parent), fname) in db_index
            ],
        })

    # Stale DB rows — applied but file missing from disk
    on_disk = {str(p) for paths in by_name.values() for p in paths}
    stale = [
        (row_id, directory, proposed_name)
        for (directory, proposed_name), (row_id, _) in db_index.items()
        if str(Path(directory) / proposed_name) not in on_disk
    ]

    return groups, stale


def apply_dedupe(
    groups: list[dict], stale: list[tuple[int, str, str]]
) -> tuple[int, int]:
    deleted = errors = 0
    for g in groups:
        for path in g["remove"]:
            try:
                path.unlink()
                deleted += 1
            except OSError as e:
                print(f"  Error deleting '{path}': {e}")
                errors += 1
        CATALOG.delete_ids(g["remove_db_ids"])
    CATALOG.delete_ids(s[0] for s in stale)
    return deleted, errors


# ── shelf scanning ─────────────────────────────────────────────────────────

def sample_category_books(root: Path, categories: list[str], n: int = 5) -> dict[str, list[str]]:
    """Return {category: [title_stem, ...]} by reading PDFs already in each category dir."""
    samples: dict[str, list[str]] = {}
    for cat in categories:
        cat_dir = root / cat
        if cat_dir.is_dir():
            books = sorted(
                Path(f).stem for f in os.listdir(cat_dir)
                if f.lower().endswith(".pdf")
            )
            samples[cat] = books[:n]
        else:
            samples[cat] = []
    return samples


# ── display ────────────────────────────────────────────────────────────────

def show_preview(groups: list[tuple[Path, list[dict]]], root: Path):
    """
    groups: list of (directory, proposals) pairs.
    Shows a directory header before each group when there is more than one.
    """
    multi = len(groups) > 1
    counter = 1
    for directory, proposals in groups:
        if multi:
            try:
                label = directory.relative_to(root)
            except ValueError:
                label = directory
            print(f"\n{label}/")
        rows = [
            (counter + i, p["original_name"], p["proposed_name"], truncate(p.get("summary") or ""))
            for i, p in enumerate(proposals)
        ]
        counter += len(proposals)
        print(table(rows, ["#", "Original", "Renamed to", "Description"], detail_last=True))


def show_landing_preview(proposals: list[dict]):
    rows = [
        (i + 1, p["original_name"], p["category"], p["display_name"], truncate(p.get("summary") or ""))
        for i, p in enumerate(proposals)
    ]
    print(table(rows, ["#", "Original", "Category", "Renamed to", "Description"], detail_last=True))


def show_dedupe_preview(groups: list[dict], stale: list[tuple[int, str, str]], root: Path):
    db_index = CATALOG.applied_index()

    def rel(p: Path) -> str:
        try:
            return str(p.relative_to(root))
        except ValueError:
            return str(p)

    if groups:
        print(f"Filesystem duplicates ({sum(len(g['remove']) for g in groups)} file(s) to remove):\n")
        for g in groups:
            print(f"  {g['filename']}")
            print(f"    KEEP   {rel(g['keep'])}")
            for p in g["remove"]:
                key = (CATALOG.key(p.parent), g["filename"])
                db_note = "  (+ DB record)" if key in db_index else ""
                print(f"    REMOVE {rel(p)}{db_note}")
        print()

    if stale:
        print(f"Stale DB records ({len(stale)} row(s) with no file on disk):\n")
        for _, directory, proposed_name in stale:
            try:
                label = str((Path(directory) / proposed_name).relative_to(root))
            except ValueError:
                label = str(Path(directory) / proposed_name)
            print(f"  {label}")
        print()


# ── main ───────────────────────────────────────────────────────────────────

def collect_pdfs(root: Path, recursive: bool) -> list[tuple[Path, list[str]]]:
    """
    Returns a list of (directory, [pdf_filenames]) pairs.
    In non-recursive mode returns only the root; in recursive mode walks all subdirs.
    """
    if not recursive:
        pdfs = sorted(
            f.name for f in root.iterdir()
            if f.is_file() and f.suffix.lower() == ".pdf"
        )
        return [(root, pdfs)] if pdfs else []

    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        pdfs = sorted(f for f in filenames if f.lower().endswith(".pdf"))
        if pdfs:
            results.append((Path(dirpath), pdfs))
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Rename PDF books to their proper titles."
    )
    parser.add_argument(
        "directory", nargs="?",
        help="Directory containing PDF files (not required for --backfill-summaries, --new, or --dedupe)",
    )
    parser.add_argument(
        "--publisher", "-p", default="",
        help="Publisher hint, e.g. 'Manning', 'Packt', \"O'Reilly\"",
    )
    parser.add_argument(
        "--recursive", "-r", action="store_true",
        help="Also process PDFs in subdirectories",
    )
    parser.add_argument(
        "--backfill-summaries", action="store_true",
        help="Fetch summaries for all applied DB records that are missing one",
    )
    parser.add_argument(
        "--new", action="store_true",
        help="Categorize and move PDFs from __landing/ into the proper subject directory",
    )
    parser.add_argument(
        "--category", "-c", default="",
        help="With --new: file every landing book into this category instead of "
             "letting the model choose (proper titles are still looked up)",
    )
    parser.add_argument(
        "--fresh", action="store_true",
        help="With --new: discard previously proposed (pending) moves for "
             "__landing/ and look everything up again from scratch",
    )
    parser.add_argument(
        "--dedupe", action="store_true",
        help="Find and remove duplicate PDFs across category directories and clean stale DB records",
    )
    parser.add_argument(
        "--db", default=None,
        help="Path to the catalog DB (default: .rename_books.db beside this script)",
    )
    parser.add_argument(
        "--root", default=None,
        help="Shelf root holding the category directories (default: data/)",
    )
    parser.add_argument(
        "--config", default=None,
        help="Path to the TOML config holding the API key "
             "(default: .rename_books.toml beside this script)",
    )
    args = parser.parse_args()

    if args.category and not args.new:
        parser.error("--category / -c only applies together with --new")
    if args.fresh and not args.new:
        parser.error("--fresh only applies together with --new")

    global CONFIG, DATA_DIR, LANDING_DIR, CATALOG, LOOKUP
    CONFIG      = Config.from_args(args)
    DATA_DIR    = CONFIG.data_dir
    LANDING_DIR = CONFIG.landing_dir

    CATALOG = Catalog(CONFIG)
    CATALOG.migrate()
    LOOKUP = LookupClient(CONFIG)

    # ── backfill-summaries mode ───────────────────────────────────────────
    if args.backfill_summaries:
        missing = CATALOG.missing_summaries()
        if not missing:
            print("All summaries already filled.")
            sys.exit(0)
        print(f"Fetching summaries for {len(missing)} book(s)…\n")
        titles    = [Path(proposed_name).stem for _, proposed_name in missing]
        title_map = LOOKUP.summaries(titles, progress=batch_progress)
        # Save all rows the API responded to (even "" = unavailable).
        # Rows not in title_map stay NULL and will be retried next run.
        updates = [
            (title_map[Path(proposed_name).stem], row_id)
            for row_id, proposed_name in missing
            if Path(proposed_name).stem in title_map
        ]
        CATALOG.set_summaries(updates)
        filled = sum(1 for summary, _ in updates if summary)
        print(f"Done. {filled}/{len(missing)} summaries added.")
        sys.exit(0)

    # ── dedupe mode ──────────────────────────────────────────────────────
    if args.dedupe:
        groups, stale = find_duplicates(DATA_DIR)
        if not groups and not stale:
            print("No duplicates or stale records found.")
            sys.exit(0)

        show_dedupe_preview(groups, stale, DATA_DIR)

        total_files = sum(len(g["remove"]) for g in groups)
        total_db    = sum(len(g["remove_db_ids"]) for g in groups) + len(stale)
        print(f"Will delete {total_files} file(s) and {total_db} DB record(s).")
        answer = input("Proceed? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            sys.exit(0)

        deleted, errors = apply_dedupe(groups, stale)
        print(f"\nDone. {deleted} file(s) deleted, {errors} error(s).")
        sys.exit(0)

    # ── new-books mode ────────────────────────────────────────────────────
    if args.new:
        LANDING_DIR.mkdir(parents=True, exist_ok=True)

        all_pdfs = sorted(
            f.name for f in LANDING_DIR.iterdir()
            if f.is_file() and f.suffix.lower() == ".pdf"
        )
        if not all_pdfs:
            print(f"No PDF files in {LANDING_DIR.name}/.")
            sys.exit(0)

        existing_categories = sorted(
            d.name for d in DATA_DIR.iterdir()
            if d.is_dir() and not d.name.startswith((".", "_"))
        )

        forced_category = args.category.strip()

        if args.fresh:
            cleared = CATALOG.clear_pending(CATALOG.key(LANDING_DIR))
            if cleared:
                print(f"Cleared {cleared} previously proposed move(s); starting fresh.")

        applied       = CATALOG.applied_names(CATALOG.key(LANDING_DIR))
        pending       = CATALOG.pending(CATALOG.key(LANDING_DIR))
        total_skipped = sum(1 for f in all_pdfs if f in applied)

        pending_here: list[dict] = []
        for f in all_pdfs:
            if f in pending:
                prop_name, summary = pending[f]
                category, _, title_file = prop_name.partition("/")
                # A forced --category overrides the category stored on a pending
                # proposal, so the whole batch is filed consistently.
                if forced_category:
                    category  = forced_category
                    prop_name = f"{forced_category}/{title_file}"
                pending_here.append({
                    "original_name": f,
                    "proposed_name": prop_name,
                    "category":      category,
                    "display_name":  title_file,
                    "summary":       summary,
                })

        new_files = [f for f in all_pdfs if f not in applied and f not in pending]
        move_proposals: list[dict] = list(pending_here)

        if pending_here:
            print(f"Re-showing {len(pending_here)} previously proposed move(s).")

        if new_files:
            if total_skipped:
                print(f"Skipping {total_skipped} already-applied file(s).")
            stems = [Path(f).stem for f in new_files]

            if forced_category:
                print(f"Looking up titles for {len(new_files)} new file(s); "
                      f"filing all into '{forced_category}'…\n")
                results = LOOKUP.titles(stems, args.publisher)
            else:
                print(f"Looking up titles and categories for {len(new_files)} new file(s)…\n")
                category_samples = sample_category_books(DATA_DIR, existing_categories)
                results          = LOOKUP.categorized_titles(
                    stems, existing_categories, category_samples)

            if not results:
                print("Could not retrieve titles. Check your OPENAI_API_KEY and try again.")
                sys.exit(1)

            title_map    = {r["original"]: r["title"]                    for r in results}
            category_map = {r["original"]: r.get("category", "Uncategorized") for r in results}
            summary_map  = {r["original"]: r.get("summary", "")          for r in results}

            for fname in new_files:
                stem     = Path(fname).stem
                title    = title_map.get(stem, stem)
                category = forced_category or category_map.get(stem, "Uncategorized")
                move_proposals.append({
                    "original_name": fname,
                    "proposed_name": f"{category}/{title}.pdf",
                    "category":      category,
                    "display_name":  f"{title}.pdf",
                    "summary":       summary_map.get(stem, ""),
                })
        elif not pending_here:
            print(f"All {total_skipped} file(s) already applied. Nothing to do.")
            sys.exit(0)

        while True:
            print()
            show_landing_preview(move_proposals)
            print()

            CATALOG.stage(CATALOG.key(LANDING_DIR), move_proposals, status="pending")

            answer = input("Apply these moves? [y/N] or enter a row number to edit its category: ").strip()
            if answer.lower() == "y":
                break
            if answer.lower() in ("n", ""):
                print("Aborted. Proposals are saved — re-run to apply them.")
                sys.exit(0)
            if answer.isdigit():
                idx = int(answer) - 1
                if 0 <= idx < len(move_proposals):
                    p = move_proposals[idx]
                    print(f"  Book     : {p['display_name']}")
                    print(f"  Category : {p['category']}")
                    new_cat = input("  New category: ").strip()
                    if new_cat:
                        p["category"]      = new_cat
                        p["proposed_name"] = f"{new_cat}/{p['display_name']}"
                else:
                    print(f"  Invalid row number — enter 1–{len(move_proposals)}.")
            else:
                print("  Enter y, n, or a row number.")

        ok, err = apply_moves(LANDING_DIR, DATA_DIR, move_proposals)
        print(f"\nDone. {ok} moved, {err} error(s).")
        sys.exit(0)

    if not args.directory:
        parser.error("directory is required unless --backfill-summaries, --new, or --dedupe is set")

    root = Path(args.directory).expanduser().resolve()
    if not root.is_dir():
        print(f"Error: '{root}' is not a directory.")
        sys.exit(1)

    if root == LANDING_DIR.resolve():
        parser.error("Use --new to process the __landing/ staging directory.")

    # ── collect PDFs, skip already-applied ───────────────────────────────
    dir_groups = collect_pdfs(root, args.recursive)
    if not dir_groups:
        print("No PDF files found.")
        sys.exit(0)

    new_groups:     list[tuple[Path, list[str]]] = []
    pending_groups: list[tuple[Path, list[dict]]] = []
    total_skipped = 0

    for directory, all_pdfs in dir_groups:
        applied  = CATALOG.applied_names(CATALOG.key(directory))
        pending  = CATALOG.pending(CATALOG.key(directory))
        total_skipped += len([f for f in all_pdfs if f in applied])

        # Files with a saved pending proposal — re-surface without API call
        pending_here = [
            {"original_name": f, "proposed_name": pending[f][0], "summary": pending[f][1]}
            for f in all_pdfs if f in pending
        ]
        if pending_here:
            pending_groups.append((directory, pending_here))

        # Truly new files — not applied and not pending
        new_files = [f for f in all_pdfs if f not in applied and f not in pending]
        if new_files:
            new_groups.append((directory, new_files))

    # ── look up new files via OpenAI ──────────────────────────────────────
    rename_groups: list[tuple[Path, list[dict]]] = []

    # Re-add pending groups first (already have proposals, no API needed)
    rename_groups.extend(pending_groups)
    total_pending = sum(len(p) for _, p in pending_groups)
    if total_pending:
        print(f"Re-showing {total_pending} previously proposed rename(s).")

    total_new = sum(len(f) for _, f in new_groups)
    if total_new:
        if total_skipped:
            print(f"Skipping {total_skipped} already-applied file(s).")
        print(f"Looking up titles for {total_new} new file(s)…\n")

        all_stems = [Path(f).stem for _, files in new_groups for f in files]
        results   = LOOKUP.titles(all_stems, args.publisher)

        if not results:
            print("Could not retrieve titles from OpenAI. Check your OPENAI_API_KEY and try again.")
            sys.exit(1)

        title_map   = {r["original"]: r["title"]           for r in results}
        summary_map = {r["original"]: r.get("summary", "") for r in results}

        # ── split into already-correct vs. needs rename ───────────────────
        already_correct: list[tuple[Path, list[dict]]] = []

        for directory, files in new_groups:
            correct   = []
            to_rename = []
            for fname in files:
                stem     = Path(fname).stem
                title    = title_map.get(stem, stem)
                proposal = {
                    "original_name": fname,
                    "proposed_name": title + ".pdf",
                    "summary":       summary_map.get(stem, ""),
                }
                if title + ".pdf" == fname:
                    correct.append(proposal)
                else:
                    to_rename.append(proposal)
            if correct:
                already_correct.append((directory, correct))
            if to_rename:
                rename_groups.append((directory, to_rename))

        # ── auto-track files that are already correctly named ─────────────
        total_correct = sum(len(p) for _, p in already_correct)
        for directory, proposals in already_correct:
            CATALOG.stage(CATALOG.key(directory), proposals, status="applied")
        if total_correct:
            print(f"Auto-tracked {total_correct} already correctly-named file(s).")
    elif not pending_groups and total_skipped:
        print(f"All {total_skipped} file(s) already applied. Nothing to do.")
        sys.exit(0)

    if not rename_groups:
        print("No renames needed.")
        sys.exit(0)

    # ── preview ───────────────────────────────────────────────────────────
    print()
    show_preview(rename_groups, root)
    print()

    for directory, proposals in rename_groups:
        CATALOG.stage(CATALOG.key(directory), proposals, status="pending")

    # ── confirm ───────────────────────────────────────────────────────────
    answer = input("Apply these renames? [y/N] ").strip().lower()
    if answer != "y":
        print("Aborted. Proposals are saved — re-run to apply them.")
        sys.exit(0)

    # ── apply ─────────────────────────────────────────────────────────────
    total_ok = total_err = 0
    for directory, proposals in rename_groups:
        ok, err = apply_renames(str(directory), proposals)
        total_ok  += ok
        total_err += err

    print(f"\nDone. {total_ok} renamed, {total_err} error(s).")


if __name__ == "__main__":
    try:
        main()
    except ConfigError as e:
        # Missing API key, or the openai package not installed. Both used to
        # be caught eagerly at import; they now surface only when a lookup is
        # actually attempted, so modes that never call the API still run.
        print(f"Error: {e}")
        sys.exit(1)
