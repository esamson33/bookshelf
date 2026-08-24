"""Catalog schema migration, key mapping, and queries."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from bookshelf.catalog import Catalog, SCHEMA_VERSION, is_helper_dir
from bookshelf.config import Config
from bookshelf.models import Proposal

LEGACY_DDL = """
CREATE TABLE renames (
    id INTEGER PRIMARY KEY AUTOINCREMENT, directory TEXT NOT NULL,
    original_name TEXT NOT NULL, proposed_name TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')), applied_at TEXT,
    UNIQUE(directory, original_name))
"""


class CatalogTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.cfg = Config(
            app_dir=self.root,
            data_dir=self.data,
            db_path=self.root / "cat.db",
            config_file=self.root / "conf.toml",
        )

    def open(self) -> Catalog:
        cat = Catalog(self.cfg)
        self.addCleanup(cat.close)
        return cat


class MigrationTest(CatalogTestCase):
    def test_creates_schema_from_empty(self):
        cat = self.open()
        self.assertEqual(cat.migrate(), 0)
        self.assertEqual(cat._columns() >= {"summary", "accessed_at"}, True)
        self.assertEqual(cat.applied_count(), 0)

    def test_stamps_version(self):
        cat = self.open()
        cat.migrate()
        version = cat._conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, SCHEMA_VERSION)

    def test_is_idempotent(self):
        cat = self.open()
        cat.migrate()
        cat.stage("Networking", [Proposal("a.pdf", "A.pdf")])
        self.assertEqual(cat.migrate(), SCHEMA_VERSION)
        self.assertEqual(len(cat.pending("Networking")), 1)

    def test_upgrades_legacy_db_without_losing_rows(self):
        """A pre-refactor DB: right table, no summary/accessed_at, no version."""
        conn = sqlite3.connect(self.cfg.db_path)
        conn.execute(LEGACY_DDL)
        conn.execute(
            "INSERT INTO renames (directory, original_name, proposed_name, status, applied_at) "
            "VALUES ('Networking', 'x.pdf', 'X.pdf', 'applied', '2026-01-01 00:00:00')"
        )
        conn.commit()
        conn.close()

        cat = self.open()
        self.assertEqual(cat.migrate(), 0)
        self.assertIn("summary", cat._columns())
        self.assertIn("accessed_at", cat._columns())
        self.assertEqual(cat.applied_count(), 1)

    def test_already_current_shape_without_stamp_is_a_no_op(self):
        """The live DB's exact situation: all columns present, user_version 0."""
        conn = sqlite3.connect(self.cfg.db_path)
        conn.execute(LEGACY_DDL)
        conn.execute("ALTER TABLE renames ADD COLUMN summary TEXT")
        conn.execute("ALTER TABLE renames ADD COLUMN accessed_at TEXT")
        conn.execute(
            "INSERT INTO renames (directory, original_name, proposed_name, status, summary) "
            "VALUES ('Networking', 'x.pdf', 'X.pdf', 'applied', 'kept')"
        )
        conn.commit()
        conn.close()

        cat = self.open()
        cat.migrate()
        self.assertEqual(cat.all_books()[0]["summary"], "kept")


class KeyTest(CatalogTestCase):
    def test_shelf_root_maps_to_dot(self):
        self.assertEqual(self.open().key(self.data), ".")

    def test_roundtrip(self):
        cat = self.open()
        self.assertEqual(cat.key(cat.path("Networking")), "Networking")

    def test_nested_key_uses_posix_separators(self):
        cat = self.open()
        self.assertEqual(cat.key(self.data / "A" / "B"), "A/B")

    def test_path_outside_shelf_falls_back_to_absolute(self):
        cat = self.open()
        outside = (self.root / "elsewhere").resolve()
        self.assertEqual(cat.key(outside), outside.as_posix())

    def test_absolute_legacy_key_resolves_to_itself(self):
        cat = self.open()
        legacy = (self.root / "elsewhere").resolve().as_posix()
        self.assertEqual(cat.path(legacy), Path(legacy))


class QueryTest(CatalogTestCase):
    def setUp(self):
        super().setUp()
        self.cat = self.open()
        self.cat.migrate()

    def test_stage_then_read_back_pending(self):
        self.cat.stage("Networking", [
            Proposal("raw.pdf", "Real.pdf", "s"),
        ])
        self.assertEqual(self.cat.pending("Networking"), {"raw.pdf": ("Real.pdf", "s")})
        self.assertEqual(self.cat.applied_names("Networking"), set())

    def test_stage_applied_stamps_applied_at(self):
        self.cat.stage("Networking", [Proposal("a.pdf", "A.pdf")],
                       status="applied")
        self.assertEqual(self.cat.applied_names("Networking"), {"a.pdf"})
        (_, applied_at), = self.cat.applied_index().values()
        self.assertTrue(applied_at)

    def test_stage_upserts_on_conflict(self):
        self.cat.stage("Networking", [Proposal("a.pdf", "First.pdf", "one")])
        self.cat.stage("Networking", [Proposal("a.pdf", "Second.pdf", "two")])
        self.assertEqual(self.cat.pending("Networking"), {"a.pdf": ("Second.pdf", "two")})

    def test_mark_applied_moves_row_out_of_pending(self):
        self.cat.stage("Networking", [Proposal("a.pdf", "A.pdf")])
        self.cat.mark_applied("Networking", "a.pdf")
        self.cat.commit()
        self.assertEqual(self.cat.pending("Networking"), {})
        self.assertEqual(self.cat.applied_names("Networking"), {"a.pdf"})

    def test_rekey_moves_landing_row_to_its_category(self):
        self.cat.stage("__landing", [
            Proposal("raw.pdf", "Networking/Real.pdf", "keep me"),
        ])
        self.cat.rekey("__landing", "raw.pdf", "Networking", "Real.pdf")
        self.cat.commit()
        self.assertEqual(self.cat.pending("__landing"), {})
        self.assertEqual(self.cat.applied_names("Networking"), {"Real.pdf"})
        # the summary survives the move
        self.assertEqual(self.cat.applied_index()[("Networking", "Real.pdf")][0] > 0, True)

    def test_clear_pending_leaves_applied_rows(self):
        self.cat.stage("Networking", [Proposal("p.pdf", "P.pdf")])
        self.cat.stage("Networking", [Proposal("a.pdf", "A.pdf")],
                       status="applied")
        self.assertEqual(self.cat.clear_pending("Networking"), 1)
        self.assertEqual(self.cat.applied_names("Networking"), {"a.pdf"})

    def test_delete_ids_handles_empty_input(self):
        self.assertEqual(self.cat.delete_ids([]), 0)

    def test_missing_summaries_only_covers_nulls(self):
        self.cat.stage("Networking", [
            Proposal("b.pdf", "B.pdf", ""),
            Proposal("c.pdf", "C.pdf", "have one"),
        ], status="applied")
        # A NULL summary only arises on rows predating the column, so insert
        # one directly — Proposal always writes a string.
        self.cat._conn.execute(
            "INSERT INTO renames (directory, original_name, proposed_name, status, summary) "
            "VALUES ('Networking', 'a.pdf', 'A.pdf', 'applied', NULL)")
        self.cat.commit()
        # "" means "asked, none available" — not retried.
        self.assertEqual([n for _, n in self.cat.missing_summaries()], ["A.pdf"])

    def test_staged_proposals_never_leave_a_null_summary(self):
        self.cat.stage("Networking", [Proposal("a.pdf", "A.pdf")], status="applied")
        self.assertEqual(self.cat.missing_summaries(), [])

    def test_set_summaries_persists(self):
        self.cat._conn.execute(
            "INSERT INTO renames (directory, original_name, proposed_name, status, summary) "
            "VALUES ('Networking', 'a.pdf', 'A.pdf', 'applied', NULL)")
        self.cat.commit()
        row_id, _ = self.cat.missing_summaries()[0]
        self.cat.set_summaries([("filled in", row_id)])
        self.assertEqual(self.cat.missing_summaries(), [])
        self.assertEqual(self.cat.all_books()[0]["summary"], "filled in")


class ReaderTest(CatalogTestCase):
    def setUp(self):
        super().setUp()
        self.cat = self.open()
        self.cat.migrate()
        (self.data / "Networking").mkdir()
        (self.data / "Networking" / "Real.pdf").write_bytes(b"%PDF")
        self.cat.stage("Networking", [Proposal("Real.pdf", "Real.pdf")],
                       status="applied")
        self.cat.stage("__landing", [Proposal("raw.pdf", "raw.pdf")],
                       status="applied")

    def test_helper_dirs_excluded_from_listings(self):
        self.assertEqual([b["category"] for b in self.cat.all_books()], ["Networking"])

    def test_is_helper_dir_matches_sql_filter(self):
        self.assertTrue(is_helper_dir("__landing"))
        self.assertTrue(is_helper_dir(".hidden/sub"))
        self.assertFalse(is_helper_dir("Networking"))
        self.assertFalse(is_helper_dir(""))

    def test_recent_needs_an_access_stamp(self):
        self.assertEqual(self.cat.recent_books(), [])
        book_id = self.cat.all_books()[0]["id"]
        self.assertTrue(self.cat.mark_opened(book_id))
        self.assertEqual(len(self.cat.recent_books()), 1)

    def test_mark_opened_reports_unknown_id(self):
        self.assertFalse(self.cat.mark_opened(999999))

    def test_book_path_resolves_existing_file(self):
        book_id = self.cat.all_books()[0]["id"]
        self.assertEqual(self.cat.book_path(book_id).name, "Real.pdf")

    def test_book_path_is_none_when_file_is_missing(self):
        (self.data / "Networking" / "Real.pdf").unlink()
        self.assertIsNone(self.cat.book_path(self.cat.all_books()[0]["id"]))

    def test_book_path_blocks_traversal_outside_the_shelf(self):
        escape = (self.root / "secret.pdf")
        escape.write_bytes(b"%PDF")
        self.cat.stage("../", [Proposal("secret.pdf", "secret.pdf")],
                       status="applied")
        row_id = self.cat.applied_index()[("../", "secret.pdf")][0]
        self.assertIsNone(self.cat.book_path(row_id))


if __name__ == "__main__":
    unittest.main()
