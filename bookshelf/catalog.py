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

from .config import Config
from .models import Proposal

SCHEMA_VERSION = 3

#: Timestamp expression for applied_at / accessed_at writes.
#
# Note the existing inconsistency this preserves for now: `created_at` in the
# live DB defaults to UTC (`datetime('now')`) while these two are written in
# local time, so the same event can differ by the UTC offset. Standardising is
# a separate step — it has to land together with display-side localisation in
# the web UI, or timestamps there would shift.
NOW_SQL = "datetime('now', 'localtime')"

TABLE = "renames"

#: Helper directories (e.g. __landing) are staging areas, not categories.
#: Expressed in SQL so the filter runs before ORDER BY / LIMIT.
NOT_HELPER_SQL = "substr(directory, 1, 1) NOT IN ('_', '.')"


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
        """Bring the DB up to SCHEMA_VERSION. Idempotent.

        Each step is also guarded by an existence check, so a DB that already
        has the right shape but no version stamp (as pre-refactor DBs do)
        passes straight through and is simply stamped.
        """
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]

            if version < 1:
                self._conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS {TABLE} (
                        id            INTEGER PRIMARY KEY AUTOINCREMENT,
                        directory     TEXT NOT NULL,
                        original_name TEXT NOT NULL,
                        proposed_name TEXT,
                        status        TEXT NOT NULL DEFAULT 'pending',
                        created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                        applied_at    TEXT,
                        UNIQUE(directory, original_name)
                    )
                """)

            existing = self._columns()
            if version < 2 and "summary" not in existing:
                self._conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN summary TEXT")
            if version < 3 and "accessed_at" not in existing:
                self._conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN accessed_at TEXT")

            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._conn.commit()
            return version

    def _columns(self) -> set[str]:
        return {r[1] for r in self._conn.execute(f"PRAGMA table_info({TABLE})")}

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
        """{original_name: (proposed_name, summary)} awaiting confirmation."""
        with self._lock:
            cur = self._conn.execute(
                f"SELECT original_name, proposed_name, summary FROM {TABLE} "
                "WHERE directory = ? AND status = 'pending'",
                (key,),
            )
            return {r[0]: (r[1], r[2] or "") for r in cur}

    def applied_index(self) -> dict[tuple[str, str], tuple[int, str]]:
        """{(directory_key, proposed_name): (id, applied_at)} for shelved rows."""
        with self._lock:
            cur = self._conn.execute(
                f"SELECT id, directory, proposed_name, applied_at FROM {TABLE} "
                "WHERE status = 'applied'"
            )
            return {(r[1], r[2]): (r[0], r[3] or "") for r in cur}

    def missing_summaries(self) -> list[tuple[int, str]]:
        """(id, proposed_name) for shelved rows never attempted (NULL only)."""
        with self._lock:
            cur = self._conn.execute(
                f"SELECT id, proposed_name FROM {TABLE} "
                "WHERE status = 'applied' AND summary IS NULL"
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

        `db_value` reproduces the legacy `proposed_name` format, including the
        "Category/Title.pdf" form used while a book sits in the landing pad.
        """
        with self._lock:
            for p in proposals:
                self._conn.execute(
                    f"""
                    INSERT INTO {TABLE}
                        (directory, original_name, proposed_name, summary, status, applied_at)
                    VALUES (?, ?, ?, ?, ?,
                            CASE ? WHEN 'applied' THEN {NOW_SQL} ELSE NULL END)
                    ON CONFLICT(directory, original_name) DO UPDATE SET
                        proposed_name = excluded.proposed_name,
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
                    SET directory = ?, original_name = ?, proposed_name = ?,
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
                f"""SELECT id, directory, proposed_name, summary, accessed_at
                    FROM {TABLE}
                    WHERE status = 'applied' AND {NOT_HELPER_SQL}
                    ORDER BY directory, proposed_name COLLATE NOCASE"""
            )
            return [
                {
                    "id": r["id"],
                    "category": r["directory"],
                    "filename": r["proposed_name"],
                    "summary": r["summary"] or "",
                    "accessed_at": r["accessed_at"],
                }
                for r in cur
            ]

    def recent_books(self, limit: int = 10) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                f"""SELECT id, directory, proposed_name, accessed_at
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
                    "filename": r["proposed_name"],
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

    def book_row(self, book_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                f"""SELECT id, directory, original_name, proposed_name, summary,
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
        candidate = (root / row["directory"] / row["proposed_name"]).resolve()
        # Guard against path traversal — must stay inside the shelf.
        if root not in candidate.parents and candidate != root:
            return None
        return candidate if candidate.is_file() else None
