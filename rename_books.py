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
import json
import sqlite3
import argparse
from pathlib import Path
from datetime import datetime

# ── optional pretty table ──────────────────────────────────────────────────
try:
    from tabulate import tabulate
    HAS_TABULATE = True
except ImportError:
    HAS_TABULATE = False

# ── OpenAI ─────────────────────────────────────────────────────────────────
try:
    from openai import OpenAI
except ImportError:
    print("Error: 'openai' package not found. Run: pip install openai")
    sys.exit(1)

import tomllib

# ── config ─────────────────────────────────────────────────────────────────
# SCRIPT_DIR holds the app files (this script, the DB, the config); DATA_DIR is
# the shelf root where the book categories and __landing/ live. DB `directory`
# keys are stored relative to DATA_DIR.
SCRIPT_DIR   = Path(__file__).parent.resolve()
DATA_DIR     = SCRIPT_DIR / "data"
CONFIG_FILE  = SCRIPT_DIR / ".rename_books.toml"
DB_PATH      = SCRIPT_DIR / ".rename_books.db"
LANDING_DIR  = DATA_DIR / "__landing"
MODEL        = "gpt-4.1-mini"


def rel_key(path) -> str:
    """DB key for a directory: its path relative to DATA_DIR (POSIX).

    The shelf root itself maps to '.'. Paths outside the shelf fall back to
    their absolute form (shouldn't normally happen).
    """
    p = Path(path).resolve()
    try:
        return p.relative_to(DATA_DIR).as_posix()
    except ValueError:
        return p.as_posix()


def abs_dir(key: str) -> Path:
    """Resolve a DB directory key back to an absolute path under DATA_DIR.

    Joining an absolute key onto DATA_DIR yields the key unchanged, so this
    also handles any legacy absolute keys gracefully.
    """
    return (DATA_DIR / key).resolve()


def load_api_key() -> str:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "rb") as f:
            config = tomllib.load(f)
        key = config.get("openai", {}).get("api_key", "")
        if key and not key.startswith("sk-..."):
            return key
    # fall back to environment variable
    key = os.environ.get("OPENAI_API_KEY", "")
    if key:
        return key
    print(f"Error: No API key found. Set it in {CONFIG_FILE} or via OPENAI_API_KEY env var.")
    sys.exit(1)


# ── database ───────────────────────────────────────────────────────────────

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS renames (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            directory     TEXT NOT NULL,
            original_name TEXT NOT NULL,
            proposed_name TEXT,
            summary       TEXT,
            status        TEXT NOT NULL DEFAULT 'pending',
            created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            applied_at    TEXT,
            UNIQUE(directory, original_name)
        )
    """)
    conn.commit()
    return conn


def already_tracked_names(conn: sqlite3.Connection, directory: str) -> set[str]:
    """Return filenames already applied (skip these entirely)."""
    cur = conn.execute(
        "SELECT original_name FROM renames WHERE directory = ? AND status = 'applied'",
        (directory,),
    )
    return {row[0] for row in cur}


def pending_proposals(conn: sqlite3.Connection, directory: str) -> dict[str, tuple[str, str]]:
    """Return {original_name: (proposed_name, summary)} for files pending confirmation."""
    cur = conn.execute(
        "SELECT original_name, proposed_name, summary FROM renames WHERE directory = ? AND status = 'pending'",
        (directory,),
    )
    return {row[0]: (row[1], row[2] or "") for row in cur}


def books_missing_summary(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """Return (id, proposed_name) for applied rows not yet attempted (NULL only)."""
    cur = conn.execute(
        "SELECT id, proposed_name FROM renames WHERE status = 'applied' AND summary IS NULL"
    )
    return cur.fetchall()


def save_summaries(conn: sqlite3.Connection, updates: list[tuple[str, int]]):
    """Persist (summary, id) pairs into the DB."""
    conn.executemany(
        "UPDATE renames SET summary = ? WHERE id = ?",
        updates,
    )
    conn.commit()


def save_proposals(conn: sqlite3.Connection, directory: str, proposals: list[dict], status: str = "pending"):
    for p in proposals:
        conn.execute(
            """
            INSERT INTO renames (directory, original_name, proposed_name, summary, status, applied_at)
            VALUES (?, ?, ?, ?, ?, CASE ? WHEN 'applied' THEN datetime('now', 'localtime') ELSE NULL END)
            ON CONFLICT(directory, original_name) DO UPDATE SET
                proposed_name = excluded.proposed_name,
                summary       = excluded.summary,
                status        = excluded.status,
                applied_at    = excluded.applied_at
            """,
            (directory, p["original_name"], p["proposed_name"], p.get("summary"), status, status),
        )
    conn.commit()


def apply_renames(
    conn: sqlite3.Connection, directory: str, proposals: list[dict]
) -> tuple[int, int]:
    dir_path = Path(directory)
    key      = rel_key(dir_path)
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
            conn.execute(
                """UPDATE renames
                   SET status = 'applied', applied_at = datetime('now', 'localtime')
                   WHERE directory = ? AND original_name = ?""",
                (key, p["original_name"]),
            )
            success += 1
        except OSError as e:
            print(f"  Error renaming '{src.name}': {e}")
            errors += 1
    conn.commit()
    return success, errors


def apply_moves(
    conn: sqlite3.Connection, landing_dir: Path, root: Path, proposals: list[dict]
) -> tuple[int, int]:
    """Move files from landing_dir into root/<category>/<title>.pdf."""
    key     = rel_key(landing_dir)
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
            conn.execute(
                """UPDATE renames
                   SET directory = ?, original_name = ?, proposed_name = ?,
                       status = 'applied', applied_at = datetime('now', 'localtime')
                   WHERE directory = ? AND original_name = ?""",
                (rel_key(dst_dir), dst.name, dst.name, key, p["original_name"]),
            )
            success += 1
        except OSError as e:
            print(f"  Error moving '{src.name}': {e}")
            errors += 1
    conn.commit()
    return success, errors


def find_duplicates(root: Path, conn: sqlite3.Connection) -> tuple[list[dict], list[tuple[int, str, str]]]:
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

    # Index applied DB rows: {(directory, proposed_name): (id, applied_at)}
    cur = conn.execute(
        "SELECT id, directory, proposed_name, applied_at FROM renames WHERE status = 'applied'"
    )
    db_index: dict[tuple[str, str], tuple[int, str]] = {
        (str(abs_dir(row[1])), row[2]): (row[0], row[3] or "") for row in cur
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
    conn: sqlite3.Connection, groups: list[dict], stale: list[tuple[int, str, str]]
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
        if g["remove_db_ids"]:
            conn.execute(
                f"DELETE FROM renames WHERE id IN ({','.join('?' * len(g['remove_db_ids']))})",
                g["remove_db_ids"],
            )
    if stale:
        stale_ids = [s[0] for s in stale]
        conn.execute(
            f"DELETE FROM renames WHERE id IN ({','.join('?' * len(stale_ids))})",
            stale_ids,
        )
    conn.commit()
    return deleted, errors


# ── API helpers ────────────────────────────────────────────────────────────

SUMMARY_BATCH_SIZE = 20


def _lookup_summaries_batch(client: OpenAI, titles: list[str]) -> dict[str, str]:
    """Fetch summaries for one batch of titles. Returns {title: summary}."""
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(titles))

    prompt = f"""\
For each book title below, search the web and write a one-sentence summary (max 200 words).

Titles:
{numbered}

Return ONLY a valid JSON array — no markdown fences, no explanation. Schema:
[{{"title": "<Exact Title As Given>", "summary": "<One sentence, max 200 words>"}}]
"""

    response = client.responses.create(
        model=MODEL,
        tools=[{"type": "web_search_preview"}],
        input=prompt,
    )

    text  = response.output_text.strip()
    start = text.find("[")
    end   = text.rfind("]") + 1
    if start != -1 and end > start:
        try:
            results = json.loads(text[start:end])
            return {r["title"]: r["summary"] for r in results}
        except (json.JSONDecodeError, KeyError):
            pass

    return {}


def lookup_summaries(titles: list[str]) -> dict[str, str]:
    """
    Given a list of proper book titles (no extension), return {title: summary}.
    Batches requests to stay within token limits.
    """
    client  = OpenAI(api_key=load_api_key())
    results = {}
    batches = [titles[i:i + SUMMARY_BATCH_SIZE] for i in range(0, len(titles), SUMMARY_BATCH_SIZE)]

    for idx, batch in enumerate(batches, 1):
        print(f"  Batch {idx}/{len(batches)} ({len(batch)} books)…")
        results.update(_lookup_summaries_batch(client, batch))

    return results


def lookup_titles(stems: list[str], publisher_hint: str) -> list[dict]:
    """
    Ask GPT-4o (with web search) to resolve each stem to a proper book title.
    Returns a list of {"original": stem, "title": "Proper Title"}.
    """
    client = OpenAI(api_key=load_api_key())

    hint = (
        f"These are books published by {publisher_hint}."
        if publisher_hint
        else "Publishers may include Packt, O'Reilly, Manning, No Starch Press, or others."
    )

    numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(stems))

    prompt = f"""\
I have PDF book filenames (stems only, no extension) that need their proper published titles.
{hint}
Search the web to find the exact, properly-cased title for each one.

Filenames:
{numbered}

Return ONLY a valid JSON array — no markdown fences, no explanation. Schema:
[{{"original": "<stem>", "title": "<Exact Published Title>", "summary": "<One sentence, max 200 words>"}}]
"""

    response = client.responses.create(
        model=MODEL,
        tools=[{"type": "web_search_preview"}],
        input=prompt,
    )

    text = response.output_text.strip()
    start = text.find("[")
    end   = text.rfind("]") + 1
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            pass

    return []


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


def lookup_categorized_titles(
    stems: list[str],
    existing_categories: list[str],
    category_samples: dict[str, list[str]] | None = None,
) -> list[dict]:
    """
    Like lookup_titles but also assigns each book to a category.
    Returns a list of {"original": stem, "title": ..., "category": ..., "summary": ...}.
    """
    client = OpenAI(api_key=load_api_key())

    cat_lines = []
    for cat in sorted(existing_categories):
        books = (category_samples or {}).get(cat, [])
        if books:
            examples = ", ".join(f'"{b}"' for b in books)
            cat_lines.append(f'- {cat} (e.g. {examples})')
        else:
            cat_lines.append(f"- {cat}")
    cat_list = "\n".join(cat_lines)

    numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(stems))

    prompt = f"""\
I have PDF book filenames (stems only, no extension) that need their proper published titles and subject categories.
Search the web to find the exact, properly-cased title for each one.

Existing categories with representative books already shelved in each:
{cat_list}

Use the example books as anchors to pick the most appropriate existing category for each new book. If none fit well, propose a short new category name.

Filenames:
{numbered}

Return ONLY a valid JSON array — no markdown fences, no explanation. Schema:
[{{"original": "<stem>", "title": "<Exact Published Title>", "category": "<Category Name>", "summary": "<One sentence, max 200 words>"}}]
"""

    response = client.responses.create(
        model=MODEL,
        tools=[{"type": "web_search_preview"}],
        input=prompt,
    )

    text  = response.output_text.strip()
    start = text.find("[")
    end   = text.rfind("]") + 1
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            pass

    return []


def _truncate(s: str, n: int = 200) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


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
            (counter + i, p["original_name"], p["proposed_name"], _truncate(p.get("summary") or ""))
            for i, p in enumerate(proposals)
        ]
        counter += len(proposals)
        if HAS_TABULATE:
            print(tabulate(rows, headers=["#", "Original", "Renamed to", "Description"], tablefmt="simple"))
        else:
            col = max(len(p["original_name"]) for p in proposals) + 2
            print(f"{'#':<4} {'Original':<{col}} Renamed to")
            print("─" * (col + 60))
            for num, orig, new, desc in rows:
                print(f"{num:<4} {orig:<{col}} {new}")
                if desc:
                    print(f"     {desc}")


def show_landing_preview(proposals: list[dict]):
    rows = [
        (i + 1, p["original_name"], p["category"], p["display_name"], _truncate(p.get("summary") or ""))
        for i, p in enumerate(proposals)
    ]
    if HAS_TABULATE:
        print(tabulate(rows, headers=["#", "Original", "Category", "Renamed to", "Description"], tablefmt="simple"))
    else:
        col_orig = max(len(p["original_name"]) for p in proposals) + 2
        col_cat  = max(len(p["category"])      for p in proposals) + 2
        col_name = max(len(p["display_name"])  for p in proposals) + 2
        print(f"{'#':<4} {'Original':<{col_orig}} {'Category':<{col_cat}} {'Renamed to':<{col_name}} Description")
        print("─" * (col_orig + col_cat + col_name + 70))
        for num, orig, cat, name, desc in rows:
            print(f"{num:<4} {orig:<{col_orig}} {cat:<{col_cat}} {name:<{col_name}} {desc}")


def show_dedupe_preview(groups: list[dict], stale: list[tuple[int, str, str]], root: Path):
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
                db_note = "  (+ DB record)" if (str(p.parent), g["filename"]) else ""
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
    args = parser.parse_args()

    if args.category and not args.new:
        parser.error("--category / -c only applies together with --new")
    if args.fresh and not args.new:
        parser.error("--fresh only applies together with --new")

    conn = init_db()

    # ── backfill-summaries mode ───────────────────────────────────────────
    if args.backfill_summaries:
        missing = books_missing_summary(conn)
        if not missing:
            print("All summaries already filled.")
            sys.exit(0)
        print(f"Fetching summaries for {len(missing)} book(s)…\n")
        titles    = [Path(proposed_name).stem for _, proposed_name in missing]
        title_map = lookup_summaries(titles)
        # Save all rows the API responded to (even "" = unavailable).
        # Rows not in title_map stay NULL and will be retried next run.
        updates = [
            (title_map[Path(proposed_name).stem], row_id)
            for row_id, proposed_name in missing
            if Path(proposed_name).stem in title_map
        ]
        save_summaries(conn, updates)
        filled = sum(1 for summary, _ in updates if summary)
        print(f"Done. {filled}/{len(missing)} summaries added.")
        sys.exit(0)

    # ── dedupe mode ──────────────────────────────────────────────────────
    if args.dedupe:
        groups, stale = find_duplicates(DATA_DIR, conn)
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

        deleted, errors = apply_dedupe(conn, groups, stale)
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
            cur = conn.execute(
                "DELETE FROM renames WHERE directory = ? AND status = 'pending'",
                (rel_key(LANDING_DIR),),
            )
            conn.commit()
            if cur.rowcount:
                print(f"Cleared {cur.rowcount} previously proposed move(s); starting fresh.")

        applied       = already_tracked_names(conn, rel_key(LANDING_DIR))
        pending       = pending_proposals(conn, rel_key(LANDING_DIR))
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
                results = lookup_titles(stems, args.publisher)
            else:
                print(f"Looking up titles and categories for {len(new_files)} new file(s)…\n")
                category_samples = sample_category_books(DATA_DIR, existing_categories)
                results          = lookup_categorized_titles(stems, existing_categories, category_samples)

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

            save_proposals(conn, rel_key(LANDING_DIR), move_proposals, status="pending")

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

        ok, err = apply_moves(conn, LANDING_DIR, DATA_DIR, move_proposals)
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
        applied  = already_tracked_names(conn, rel_key(directory))
        pending  = pending_proposals(conn, rel_key(directory))
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
        results   = lookup_titles(all_stems, args.publisher)

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
            save_proposals(conn, rel_key(directory), proposals, status="applied")
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
        save_proposals(conn, rel_key(directory), proposals, status="pending")

    # ── confirm ───────────────────────────────────────────────────────────
    answer = input("Apply these renames? [y/N] ").strip().lower()
    if answer != "y":
        print("Aborted. Proposals are saved — re-run to apply them.")
        sys.exit(0)

    # ── apply ─────────────────────────────────────────────────────────────
    total_ok = total_err = 0
    for directory, proposals in rename_groups:
        ok, err = apply_renames(conn, str(directory), proposals)
        total_ok  += ok
        total_err += err

    print(f"\nDone. {total_ok} renamed, {total_err} error(s).")


if __name__ == "__main__":
    main()
