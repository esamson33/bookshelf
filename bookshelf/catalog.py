"""SQLite catalog of the shelf — the single owner of the schema.

Previously the schema had two authors: rename_books.init_db() created the
table, while bookshelf_server.ensure_schema() bolted on `accessed_at`. A DB
created by the CLI was therefore missing a column the server needed until the
server happened to run against it. All DDL now lives here, versioned via
PRAGMA user_version, and both entry points go through this class.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Iterable, Sequence

from .config import ARCHIVE_NAME, Config
from .models import Proposal

SCHEMA_VERSION = 4

#: Timestamp expression for applied_at / accessed_at writes.
#
# Note the existing inconsistency this preserves for now: `created_at` in the
# live DB defaults to UTC (`datetime('now')`) while these two are written in
# local time, so the same event can differ by the UTC offset. Standardising is
# a separate step — it has to land together with display-side localisation in
# the web UI, or timestamps there would shift.
NOW_SQL = "datetime('now', 'localtime')"

TABLE = "books"
#: Pre-v4 name. A catalog is a list of books; only some rows describe a rename.
LEGACY_TABLE = "renames"

#: Helper directories (__landing, __archive) are staging areas, not
#: categories. Expressed in SQL so the filter runs before ORDER BY / LIMIT.
NOT_HELPER_SQL = "substr(directory, 1, 1) NOT IN ('_', '.')"

#: Prefix of every archived row's directory key. Matched with substr() rather
#: than LIKE, whose `_` wildcard would make "__archive/%" match far too much.
ARCHIVE_PREFIX = ARCHIVE_NAME + "/"


def is_helper_dir(directory: str) -> bool:
    """Python counterpart of NOT_HELPER_SQL, for values already fetched."""
    top = (directory or "").split("/", 1)[0]
    return top.startswith(("_", "."))


class Catalog:
    """Every query against the catalog DB.

    Guarded by an internal lock so the threaded web server can share one
    connection; the CLI is single-threaded and pays only an uncontended
    acquire.
    """

    def __init__(self, config: Config, *, threaded: bool = False):
        self.config = config
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(config.db_path), check_same_thread=not threaded
        )
        self._conn.row_factory = sqlite3.Row

    # ── lifecycle ────────────────────────────────────────────────────────

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def migrate(self) -> int:
        """Bring the DB up to SCHEMA_VERSION. Returns the version found.

        Every step is driven by the DB's actual shape rather than by the
        version stamp alone, because pre-refactor DBs carry no stamp at all
        while already having most of the schema. Running twice is a no-op.
        """
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            table = self._existing_table()

            if table is None:
                # Fresh DB — create the current shape outright.
                self._conn.execute(f"""
                    CREATE TABLE {TABLE} (
                        id            INTEGER PRIMARY KEY AUTOINCREMENT,
                        directory     TEXT NOT NULL,
                        original_name TEXT NOT NULL,
                        filename      TEXT,
                        status        TEXT NOT NULL DEFAULT 'pending',
                        created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                        applied_at    TEXT,
                        summary       TEXT,
                        accessed_at   TEXT,
                        UNIQUE(directory, original_name)
                    )
                """)
            else:
                columns = self._columns(table)
                if "summary" not in columns:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN summary TEXT")
                if "accessed_at" not in columns:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN accessed_at TEXT")

                # v4: the table is a catalog of books, and for a shelved book
                # the column is simply its filename — "proposed" only holds
                # while a row is still pending.
                if table == LEGACY_TABLE:
                    self._conn.execute(
                        f"ALTER TABLE {LEGACY_TABLE} RENAME TO {TABLE}")
                if "proposed_name" in self._columns(TABLE):
                    self._conn.execute(
                        f"ALTER TABLE {TABLE} RENAME COLUMN proposed_name TO filename")

            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._conn.commit()
            return version

    def _existing_table(self) -> str | None:
        """Whichever of the two table names this DB currently uses."""
        row = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN (?, ?)",
            (TABLE, LEGACY_TABLE),
        ).fetchone()
        return row[0] if row else None

    def _columns(self, table: str = TABLE) -> set[str]:
        return {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}

    # ── directory keys ───────────────────────────────────────────────────

    def key(self, path) -> str:
        """Catalog key for a directory: its path relative to the shelf root.

        The shelf root itself maps to '.'. Paths outside the shelf fall back
        to their absolute form (shouldn't normally happen).
        """
        p = Path(path).resolve()
        try:
            return p.relative_to(self.config.data_dir).as_posix()
        except ValueError:
            return p.as_posix()

    def path(self, key: str) -> Path:
        """Resolve a catalog key back to an absolute path under the shelf.

        Joining an absolute key onto the root yields the key unchanged, so
        legacy absolute keys resolve correctly too.
        """
        return (self.config.data_dir / key).resolve()

    # ── reads ────────────────────────────────────────────────────────────

    def applied_names(self, key: str) -> set[str]:
        """Filenames already shelved in `key` — skip these entirely."""
        with self._lock:
            cur = self._conn.execute(
                f"SELECT original_name FROM {TABLE} "
                "WHERE directory = ? AND status = 'applied'",
                (key,),
            )
            return {r[0] for r in cur}

    def pending(self, key: str) -> dict[str, tuple[str, str]]:
        """{original_name: (filename, summary)} awaiting confirmation."""
        with self._lock:
            cur = self._conn.execute(
                f"SELECT original_name, filename, summary FROM {TABLE} "
                "WHERE directory = ? AND status = 'pending'",
                (key,),
            )
            return {r[0]: (r[1], r[2] or "") for r in cur}

    def pending_rows(self, key: str) -> list[dict]:
        """Pending proposals for `key`, each with the row id the UI edits by.

        pending() answers the scan path, which matches on original_name and
        has no use for the id. The web UI edits and drops individual rows, so
        it needs one.
        """
        with self._lock:
            cur = self._conn.execute(
                f"SELECT id, original_name, filename, summary FROM {TABLE} "
                "WHERE directory = ? AND status = 'pending' "
                "ORDER BY original_name COLLATE NOCASE",
                (key,),
            )
            return [
                {"id": r[0], "original_name": r[1],
                 "filename": r[2], "summary": r[3] or ""}
                for r in cur
            ]

    def pending_by_directory(self) -> list[dict]:
        """{directory, count} for every directory holding staged work.

        A scan can be reviewed later, so the UI has to be able to find the
        directories with proposals still waiting.
        """
        with self._lock:
            cur = self._conn.execute(
                f"SELECT directory, COUNT(*) FROM {TABLE} WHERE status = 'pending' "
                "GROUP BY directory ORDER BY directory"
            )
            return [{"directory": r[0], "count": r[1]} for r in cur]

    def applied_index(self) -> dict[tuple[str, str], tuple[int, str]]:
        """{(directory_key, filename): (id, applied_at)} for shelved rows.

        Archived rows are left out: the dedupe walk never enters __archive/,
        so including them would report every archived book as stale.
        """
        with self._lock:
            cur = self._conn.execute(
                f"SELECT id, directory, filename, applied_at FROM {TABLE} "
                f"WHERE status = 'applied' AND {NOT_HELPER_SQL}"
            )
            return {(r[1], r[2]): (r[0], r[3] or "") for r in cur}

    def missing_summaries(self) -> list[tuple[int, str]]:
        """(id, filename) for shelved rows never attempted (NULL only).

        An archived book is out of the library, so it is not worth a lookup.
        """
        with self._lock:
            cur = self._conn.execute(
                f"SELECT id, filename FROM {TABLE} "
                f"WHERE status = 'applied' AND summary IS NULL AND {NOT_HELPER_SQL}"
            )
            return [(r[0], r[1]) for r in cur]

    def applied_count(self) -> int:
        with self._lock:
            return self._conn.execute(
                f"SELECT COUNT(*) FROM {TABLE} WHERE status = 'applied'"
            ).fetchone()[0]

    # ── writes ───────────────────────────────────────────────────────────

    def stage(
        self, key: str, proposals: Sequence["Proposal"], status: str = "pending"
    ) -> None:
        """Upsert proposals for `key`, stamping applied_at when applied.

        `db_value` reproduces the stored `filename` format, including the
        "Category/Title.pdf" form used while a book sits in the landing pad.
        """
        with self._lock:
            for p in proposals:
                self._conn.execute(
                    f"""
                    INSERT INTO {TABLE}
                        (directory, original_name, filename, summary, status, applied_at)
                    VALUES (?, ?, ?, ?, ?,
                            CASE ? WHEN 'applied' THEN {NOW_SQL} ELSE NULL END)
                    ON CONFLICT(directory, original_name) DO UPDATE SET
                        filename      = excluded.filename,
                        summary       = excluded.summary,
                        status        = excluded.status,
                        applied_at    = excluded.applied_at
                    """,
                    # summary is stored verbatim: "" means "looked up, none
                    # available" and must stay distinct from NULL, which
                    # missing_summaries() treats as "never attempted".
                    (key, p.original_name, p.db_value, p.summary, status, status),
                )
            self._conn.commit()

    def mark_applied(self, key: str, original_name: str) -> None:
        with self._lock:
            self._conn.execute(
                f"UPDATE {TABLE} SET status = 'applied', applied_at = {NOW_SQL} "
                "WHERE directory = ? AND original_name = ?",
                (key, original_name),
            )

    def rekey(self, src_key: str, src_name: str, dst_key: str, dst_name: str) -> None:
        """Re-point a row at its destination after a move.

        A shelved book is an ordinary row (directory = category, both names =
        the final filename), so nothing is left behind under __landing while
        the summary is preserved.
        """
        with self._lock:
            self._conn.execute(
                f"""UPDATE {TABLE}
                    SET directory = ?, original_name = ?, filename = ?,
                        status = 'applied', applied_at = {NOW_SQL}
                    WHERE directory = ? AND original_name = ?""",
                (dst_key, dst_name, dst_name, src_key, src_name),
            )

    def set_summaries(self, updates: Sequence[tuple[str, int]]) -> None:
        """Persist (summary, id) pairs."""
        with self._lock:
            self._conn.executemany(
                f"UPDATE {TABLE} SET summary = ? WHERE id = ?", updates
            )
            self._conn.commit()

    def set_pending_value(self, row_id: int, db_value: str) -> bool:
        """Retarget one staged proposal. `db_value` is Proposal.db_value, so
        the overloaded "Category/Title.pdf" form is produced in one place.

        Applied rows are deliberately out of reach: renaming a shelved book is
        a different operation from editing a proposal.
        """
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE {TABLE} SET filename = ? WHERE id = ? AND status = 'pending'",
                (db_value, row_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def clear_pending(self, key: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                f"DELETE FROM {TABLE} WHERE directory = ? AND status = 'pending'",
                (key,),
            )
            self._conn.commit()
            return cur.rowcount

    def delete_ids(self, ids: Iterable[int]) -> int:
        ids = list(ids)
        if not ids:
            return 0
        with self._lock:
            cur = self._conn.execute(
                f"DELETE FROM {TABLE} WHERE id IN ({','.join('?' * len(ids))})", ids
            )
            self._conn.commit()
            return cur.rowcount

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    # ── reader (web UI) ──────────────────────────────────────────────────

    def all_books(self) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                f"""SELECT id, directory, filename, summary, accessed_at
                    FROM {TABLE}
                    WHERE status = 'applied' AND {NOT_HELPER_SQL}
                    ORDER BY directory, filename COLLATE NOCASE"""
            )
            return [
                {
                    "id": r["id"],
                    "category": r["directory"],
                    "filename": r["filename"],
                    "summary": r["summary"] or "",
                    "accessed_at": r["accessed_at"],
                }
                for r in cur
            ]

    def recent_books(self, limit: int = 10) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                f"""SELECT id, directory, filename, accessed_at
                    FROM {TABLE}
                    WHERE status = 'applied' AND accessed_at IS NOT NULL
                      AND {NOT_HELPER_SQL}
                    ORDER BY accessed_at DESC
                    LIMIT ?""",
                (limit,),
            )
            return [
                {
                    "id": r["id"],
                    "category": r["directory"],
                    "filename": r["filename"],
                    "accessed_at": r["accessed_at"],
                }
                for r in cur
            ]

    def mark_opened(self, book_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE {TABLE} SET accessed_at = {NOW_SQL} WHERE id = ?",
                (book_id,),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def clear_accessed(self, book_id: int) -> bool:
        """Drop a book off the Recent list by clearing its access stamp."""
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE {TABLE} SET accessed_at = NULL WHERE id = ?",
                (book_id,),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def book_row(self, book_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                f"""SELECT id, directory, original_name, filename, summary,
                           status, applied_at, accessed_at
                    FROM {TABLE} WHERE id = ?""",
                (book_id,),
            ).fetchone()

    def book_path(self, book_id: int) -> Path | None:
        """Absolute path of a book's PDF, or None if missing or out of bounds."""
        row = self.book_row(book_id)
        if not row:
            return None
        root = self.config.data_dir
        candidate = (root / row["directory"] / row["filename"]).resolve()
        # Guard against path traversal — must stay inside the shelf.
        if root not in candidate.parents and candidate != root:
            return None
        return candidate if candidate.is_file() else None

    # ── archive ──────────────────────────────────────────────────────────
    #
    # An archived book is an ordinary applied row whose directory key starts
    # with "__archive/": the helper-dir filter already keeps it out of the
    # library, Recent and search, and the category it came from is the rest
    # of the key. No new status or column, and nothing else has to know.

    def archived_books(self) -> list[dict]:
        """Every archived book, with the category it was archived from."""
        with self._lock:
            cur = self._conn.execute(
                f"""SELECT id, directory, filename, summary
                    FROM {TABLE}
                    WHERE status = 'applied'
                      AND substr(directory, 1, length(?)) = ?
                    ORDER BY directory, filename COLLATE NOCASE""",
                (ARCHIVE_PREFIX, ARCHIVE_PREFIX),
            )
            return [
                {
                    "id": r["id"],
                    "category": r["directory"][len(ARCHIVE_PREFIX):],
                    "filename": r["filename"],
                    "summary": r["summary"] or "",
                }
                for r in cur
            ]

    def move_book(self, book_id: int, dst_key: str, dst_name: str) -> bool:
        """Re-point a shelved book at its new directory and filename.

        The caller has already moved the file; this only records where it
        went. original_name and applied_at are kept, so "filed from" and the
        dedupe tie-break both survive a round trip through the archive.
        Raises ValueError if the destination already holds a row with that
        original name.
        """
        with self._lock:
            try:
                cur = self._conn.execute(
                    f"""UPDATE {TABLE}
                        SET directory = ?, filename = ?
                        WHERE id = ? AND status = 'applied'""",
                    (dst_key, dst_name, book_id),
                )
            except sqlite3.IntegrityError:
                raise ValueError(
                    f"The catalog already has a row for that book under {dst_key}/."
                ) from None
            self._conn.commit()
            return cur.rowcount > 0
