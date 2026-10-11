"""Filesystem scanning and the applied/pending/new partition."""

import tempfile
import unittest
from pathlib import Path

from bookshelf.catalog import Catalog
from bookshelf.config import Config
from bookshelf.models import Proposal
from bookshelf.scan import (
    categories,
    collect_pdfs,
    pdf_names,
    reconcile,
    sample_titles,
)


class ScanTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.data = self.root / "data"
        self.data.mkdir()
        self.cfg = Config(app_dir=self.root, data_dir=self.data,
                          db_path=self.root / "cat.db", config_file=self.root / "c.toml")
        self.cat = Catalog(self.cfg)
        self.addCleanup(self.cat.close)
        self.cat.migrate()

    def touch(self, rel):
        path = self.data / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF")
        return path


class PdfNamesTest(ScanTestCase):
    def test_lists_only_pdfs_sorted(self):
        self.touch("Networking/b.pdf")
        self.touch("Networking/a.pdf")
        (self.data / "Networking" / "notes.txt").write_text("x")
        self.assertEqual(pdf_names(self.data / "Networking"), ["a.pdf", "b.pdf"])

    def test_extension_match_is_case_insensitive(self):
        self.touch("Networking/Shouty.PDF")
        self.assertEqual(pdf_names(self.data / "Networking"), ["Shouty.PDF"])

    def test_missing_directory_is_empty_not_an_error(self):
        self.assertEqual(pdf_names(self.data / "nope"), [])

    def test_subdirectories_are_not_descended_into(self):
        self.touch("Networking/sub/deep.pdf")
        self.assertEqual(pdf_names(self.data / "Networking"), [])


class CollectPdfsTest(ScanTestCase):
    def test_non_recursive_returns_only_the_root(self):
        self.touch("top.pdf")
        self.touch("Networking/nested.pdf")
        self.assertEqual(collect_pdfs(self.data, recursive=False),
                         [(self.data, ["top.pdf"])])

    def test_non_recursive_with_no_pdfs_returns_nothing(self):
        self.touch("Networking/nested.pdf")
        self.assertEqual(collect_pdfs(self.data, recursive=False), [])

    def test_recursive_walks_subdirectories(self):
        self.touch("Networking/a.pdf")
        self.touch("Systems/b.pdf")
        found = dict(collect_pdfs(self.data, recursive=True))
        self.assertEqual(found[self.data / "Networking"], ["a.pdf"])
        self.assertEqual(found[self.data / "Systems"], ["b.pdf"])

    def test_recursive_skips_directories_holding_no_pdfs(self):
        (self.data / "Empty").mkdir()
        self.touch("Networking/a.pdf")
        self.assertEqual([d for d, _ in collect_pdfs(self.data, recursive=True)],
                         [self.data / "Networking"])


class CategoriesTest(ScanTestCase):
    def test_lists_directories_sorted_excluding_helpers(self):
        for name in ("Networking", "Algorithms", "__landing", ".hidden"):
            (self.data / name).mkdir()
            (self.data / name / "a.pdf").write_bytes(b"%PDF")
        (self.data / "loose.pdf").write_bytes(b"%PDF")
        self.assertEqual(categories(self.data), ["Algorithms", "Networking"])

    def test_a_category_with_no_pdfs_is_not_listed(self):
        (self.data / "Empty").mkdir()
        (self.data / "Empty" / "notes.txt").write_text("x")
        (self.data / "Networking").mkdir()
        (self.data / "Networking" / "a.pdf").write_bytes(b"%PDF")
        self.assertEqual(categories(self.data), ["Networking"])

    def test_missing_root_is_empty(self):
        self.assertEqual(categories(self.data / "nope"), [])


class SampleTitlesTest(ScanTestCase):
    def test_returns_stems_capped_at_n(self):
        for i in range(7):
            self.touch(f"Networking/Book {i}.pdf")
        samples = sample_titles(self.data, ["Networking"], n=3)
        self.assertEqual(samples["Networking"], ["Book 0", "Book 1", "Book 2"])

    def test_category_with_no_books_maps_to_empty_list(self):
        (self.data / "Bare").mkdir()
        self.assertEqual(sample_titles(self.data, ["Bare"]), {"Bare": []})

    def test_missing_category_dir_maps_to_empty_list(self):
        self.assertEqual(sample_titles(self.data, ["Gone"]), {"Gone": []})


class ReconcileTest(ScanTestCase):
    def setUp(self):
        super().setUp()
        self.dir = self.data / "Networking"
        self.dir.mkdir()
        self.key = self.cat.key(self.dir)

    def test_partitions_applied_pending_and_unknown(self):
        self.cat.stage(self.key, [Proposal("done.pdf", "done.pdf")], status="applied")
        self.cat.stage(self.key, [Proposal("raw.pdf", "Real.pdf", "sum")])

        found = reconcile(self.cat, self.dir, ["done.pdf", "raw.pdf", "brand_new.pdf"])

        self.assertEqual(found.applied, 1)
        self.assertEqual([p.original_name for p in found.pending], ["raw.pdf"])
        self.assertEqual(found.unknown, ["brand_new.pdf"])

    def test_pending_comes_back_as_proposals_with_summaries(self):
        self.cat.stage(self.key, [Proposal("raw.pdf", "Real.pdf", "the summary")])
        found = reconcile(self.cat, self.dir, ["raw.pdf"])
        self.assertEqual(found.pending[0].filename, "Real.pdf")
        self.assertEqual(found.pending[0].summary, "the summary")

    def test_landing_pending_keeps_its_category(self):
        landing_key = self.cat.key(self.data / "__landing")
        self.cat.stage(landing_key, [
            Proposal("raw.pdf", "Deep Learning.pdf", "s", "Machine Learning and AI"),
        ])
        found = reconcile(self.cat, self.data / "__landing", ["raw.pdf"])
        self.assertEqual(found.pending[0].category, "Machine Learning and AI")
        self.assertEqual(found.pending[0].filename, "Deep Learning.pdf")

    def test_db_rows_for_absent_files_are_ignored(self):
        self.cat.stage(self.key, [Proposal("gone.pdf", "Gone.pdf")], status="applied")
        found = reconcile(self.cat, self.dir, ["here.pdf"])
        self.assertEqual(found.applied, 0)
        self.assertEqual(found.unknown, ["here.pdf"])

    def test_empty_directory_is_falsy(self):
        self.assertFalse(reconcile(self.cat, self.dir, []))

    def test_stems_drop_the_extension_for_lookup(self):
        found = reconcile(self.cat, self.dir, ["packt_raw_2019.pdf"])
        self.assertEqual(found.stems, ["packt_raw_2019"])

    def test_input_order_is_preserved(self):
        found = reconcile(self.cat, self.dir, ["c.pdf", "a.pdf", "b.pdf"])
        self.assertEqual(found.unknown, ["c.pdf", "a.pdf", "b.pdf"])


if __name__ == "__main__":
    unittest.main()
