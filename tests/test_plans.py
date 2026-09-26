"""Plan building for each mode. The lookup client is stubbed — no network."""

import tempfile
import unittest
from pathlib import Path

from bookshelf.catalog import Catalog
from bookshelf.config import Config
from bookshelf.models import Proposal
from bookshelf.plans import Context, PlanError
from bookshelf.plans import dedupe as dedupe_plan
from bookshelf.plans import rename as rename_plan
from bookshelf.plans import shelve as shelve_plan
from bookshelf.plans import summaries as summaries_plan


class StubLookup:
    """Returns canned rows and records what it was asked."""

    def __init__(self, titles=None, categorized=None, summaries=None):
        self._titles = titles if titles is not None else []
        self._categorized = categorized if categorized is not None else []
        self._summaries = summaries or {}
        self.calls = []

    def titles(self, stems, publisher=""):
        self.calls.append(("titles", list(stems), publisher))
        return self._titles

    def categorized_titles(self, stems, categories, samples=None):
        self.calls.append(("categorized", list(stems), list(categories)))
        return self._categorized

    def summaries(self, titles, batch_size=20, progress=None):
        self.calls.append(("summaries", list(titles)))
        return self._summaries


class PlanTestCase(unittest.TestCase):
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
        self.printed = []

    def context(self, lookup=None):
        return Context(config=self.cfg, catalog=self.cat,
                       lookup=lookup or StubLookup(),
                       out=lambda *a: self.printed.append(" ".join(str(x) for x in a)))

    def touch(self, rel, content=b"%PDF"):
        path = self.data / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path


class RenamePlanTest(PlanTestCase):
    def test_no_pdfs_yields_an_empty_plan(self):
        (self.data / "Networking").mkdir()
        plan = rename_plan.build(self.context(), self.data / "Networking", False, "")
        self.assertTrue(plan.is_empty())
        self.assertEqual(plan.empty_message, "No PDF files found.")

    def test_pending_proposals_resurface_without_a_lookup(self):
        self.touch("Networking/raw.pdf")
        directory = self.data / "Networking"
        self.cat.stage(self.cat.key(directory), [Proposal("raw.pdf", "Real.pdf", "s")])

        lookup = StubLookup()
        plan = rename_plan.build(self.context(lookup), directory, False, "")

        self.assertEqual(lookup.calls, [])
        self.assertEqual(plan.groups[0][1][0].filename, "Real.pdf")

    def test_already_correct_names_are_auto_tracked_not_proposed(self):
        self.touch("Networking/Real Title.pdf")
        directory = self.data / "Networking"
        lookup = StubLookup(titles=[{"original": "Real Title", "title": "Real Title",
                                     "summary": "s"}])
        plan = rename_plan.build(self.context(lookup), directory, False, "")

        self.assertTrue(plan.is_empty())
        self.assertEqual(self.cat.applied_names(self.cat.key(directory)), {"Real Title.pdf"})
        self.assertIn("Auto-tracked 1 already correctly-named file(s).", self.printed)

    def test_failed_lookup_raises_plan_error(self):
        self.touch("Networking/raw.pdf")
        with self.assertRaises(PlanError):
            rename_plan.build(self.context(StubLookup(titles=[])),
                              self.data / "Networking", False, "")

    def test_apply_renames_the_file_and_marks_it_applied(self):
        self.touch("Networking/raw.pdf")
        directory = self.data / "Networking"
        plan = rename_plan.RenamePlan([(directory, [Proposal("raw.pdf", "Real.pdf")])],
                                      self.data)
        outcome = plan.apply(self.cat)

        self.assertEqual((outcome.ok, outcome.errors), (1, 0))
        self.assertTrue((directory / "Real.pdf").is_file())
        self.assertFalse((directory / "raw.pdf").exists())

    def test_apply_skips_when_the_destination_exists(self):
        self.touch("Networking/raw.pdf")
        self.touch("Networking/Real.pdf")
        directory = self.data / "Networking"
        plan = rename_plan.RenamePlan([(directory, [Proposal("raw.pdf", "Real.pdf")])],
                                      self.data)
        outcome = plan.apply(self.cat)

        self.assertEqual((outcome.ok, outcome.errors), (0, 1))
        self.assertTrue((directory / "raw.pdf").is_file())

    def test_payload_labels_each_group_relative_to_the_scanned_root(self):
        plan = rename_plan.RenamePlan(
            [(self.data / "A", [Proposal("a.pdf", "A.pdf")]),
             (self.data / "B", [Proposal("b.pdf", "B.pdf")])], self.data)
        groups = plan.payload()["groups"]
        self.assertEqual([g["directory"] for g in groups], ["A", "B"])
        self.assertEqual(groups[0]["rows"][0]["original"], "a.pdf")
        self.assertEqual(groups[0]["rows"][0]["proposed"], "A.pdf")

    def test_payload_falls_back_to_an_absolute_path_outside_the_root(self):
        outside = Path(self.tmp.name).resolve() / "elsewhere"
        plan = rename_plan.RenamePlan([(outside, [Proposal("a.pdf", "A.pdf")])],
                                      self.data)
        self.assertEqual(plan.payload()["groups"][0]["directory"], str(outside))

    def test_from_pending_rebuilds_staged_proposals_with_their_row_ids(self):
        directory = self.data / "Networking"
        directory.mkdir(parents=True, exist_ok=True)
        self.cat.stage(self.cat.key(directory), [Proposal("raw.pdf", "Real.pdf", "s")])

        plan = rename_plan.from_pending(self.context(), directory)

        row = plan.payload()["groups"][0]["rows"][0]
        self.assertEqual(row["proposed"], "Real.pdf")
        self.assertEqual(row["summary"], "s")
        self.assertIsNotNone(row["id"])

    def test_from_pending_sweeps_in_subdirectories(self):
        """A recursive scan stages a row under every directory it walked."""
        for name in ("Networking", "Networking/Deep"):
            (self.data / name).mkdir(parents=True, exist_ok=True)
            self.cat.stage(self.cat.key(self.data / name),
                           [Proposal("raw.pdf", f"{Path(name).name}.pdf")])

        plan = rename_plan.from_pending(self.context(), self.data / "Networking")

        self.assertEqual([g["directory"] for g in plan.payload()["groups"]],
                         [".", "Deep"])

    def test_from_pending_leaves_the_landing_pad_to_shelve(self):
        self.cfg.landing_dir.mkdir(parents=True, exist_ok=True)
        self.cat.stage(self.cat.key(self.cfg.landing_dir),
                       [Proposal("raw.pdf", "Cat/Real.pdf")])

        plan = rename_plan.from_pending(self.context(), self.data)

        self.assertTrue(plan.is_empty())


class ShelvePlanTest(PlanTestCase):
    def setUp(self):
        super().setUp()
        self.landing = self.cfg.landing_dir
        self.landing.mkdir(parents=True)

    def test_empty_landing_yields_an_empty_plan(self):
        plan = shelve_plan.build(self.context(), "", False, "")
        self.assertTrue(plan.is_empty())
        self.assertIn("No PDF files in", plan.empty_message)

    def test_forced_category_skips_the_categorising_lookup(self):
        (self.landing / "raw.pdf").write_bytes(b"%PDF")
        lookup = StubLookup(titles=[{"original": "raw", "title": "Real", "summary": "s"}])
        plan = shelve_plan.build(self.context(lookup), "Forced", False, "")

        self.assertEqual(lookup.calls[0][0], "titles")
        self.assertEqual(plan.proposals[0].category, "Forced")
        self.assertEqual(plan.proposals[0].db_value, "Forced/Real.pdf")

    def test_without_a_forced_category_the_model_chooses(self):
        (self.landing / "raw.pdf").write_bytes(b"%PDF")
        lookup = StubLookup(categorized=[
            {"original": "raw", "title": "Real", "category": "Networking", "summary": "s"}])
        plan = shelve_plan.build(self.context(lookup), "", False, "")

        self.assertEqual(lookup.calls[0][0], "categorized")
        self.assertEqual(plan.proposals[0].category, "Networking")

    def test_missing_category_in_the_reply_falls_back_to_uncategorized(self):
        (self.landing / "raw.pdf").write_bytes(b"%PDF")
        lookup = StubLookup(categorized=[{"original": "raw", "title": "Real"}])
        plan = shelve_plan.build(self.context(lookup), "", False, "")
        self.assertEqual(plan.proposals[0].category, "Uncategorized")

    def test_forced_category_overrides_a_stored_pending_proposal(self):
        (self.landing / "raw.pdf").write_bytes(b"%PDF")
        self.cat.stage(self.cat.key(self.landing),
                       [Proposal("raw.pdf", "Real.pdf", "s", "Old Category")])
        plan = shelve_plan.build(self.context(), "Forced", False, "")
        self.assertEqual(plan.proposals[0].category, "Forced")

    def test_fresh_clears_pending_and_looks_up_again(self):
        (self.landing / "raw.pdf").write_bytes(b"%PDF")
        self.cat.stage(self.cat.key(self.landing),
                       [Proposal("raw.pdf", "Stale.pdf", "s", "Old")])
        lookup = StubLookup(categorized=[
            {"original": "raw", "title": "Fresh", "category": "New", "summary": ""}])
        plan = shelve_plan.build(self.context(lookup), "", True, "")

        self.assertEqual(plan.proposals[0].filename, "Fresh.pdf")
        self.assertIn("Cleared 1 previously proposed move(s); starting fresh.", self.printed)

    def test_payload_carries_the_category_and_title_separately(self):
        plan = shelve_plan.ShelvePlan([Proposal("raw.pdf", "R.pdf", "why", "Old")],
                                      self.landing, self.data)
        row = plan.payload()["rows"][0]
        self.assertEqual(row["original"], "raw.pdf")
        self.assertEqual(row["category"], "Old")
        self.assertEqual(row["title"], "R")
        self.assertEqual(row["summary"], "why")

    def test_from_pending_reads_back_a_staged_batch(self):
        (self.landing / "raw.pdf").write_bytes(b"%PDF")
        self.cat.stage(self.cat.key(self.landing),
                       [Proposal("raw.pdf", "R.pdf", "why", "Old")])

        rows = shelve_plan.from_pending(self.context()).payload()["rows"]

        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["category"], rows[0]["proposed"]), ("Old", "R.pdf"))
        self.assertIsNotNone(rows[0]["id"])

    def test_from_pending_drops_a_proposal_whose_file_vanished(self):
        """Otherwise the row would only fail at apply() time."""
        self.cat.stage(self.cat.key(self.landing),
                       [Proposal("gone.pdf", "G.pdf", "", "Old")])
        self.assertTrue(shelve_plan.from_pending(self.context()).is_empty())

    def test_from_pending_reflects_an_edit_made_between_review_and_apply(self):
        (self.landing / "raw.pdf").write_bytes(b"%PDF")
        self.cat.stage(self.cat.key(self.landing),
                       [Proposal("raw.pdf", "R.pdf", "", "Old")])
        row_id = self.cat.pending_rows(self.cat.key(self.landing))[0]["id"]

        self.cat.set_pending_value(row_id, "New/Better.pdf")

        proposal = shelve_plan.from_pending(self.context()).proposals[0]
        self.assertEqual((proposal.category, proposal.filename), ("New", "Better.pdf"))

    def test_apply_moves_the_file_and_rekeys_the_row(self):
        (self.landing / "raw.pdf").write_bytes(b"%PDF")
        proposal = Proposal("raw.pdf", "Real.pdf", "keep me", "Networking")
        self.cat.stage(self.cat.key(self.landing), [proposal])
        plan = shelve_plan.ShelvePlan([proposal], self.landing, self.data)

        outcome = plan.apply(self.cat)

        self.assertEqual((outcome.ok, outcome.errors), (1, 0))
        self.assertTrue((self.data / "Networking" / "Real.pdf").is_file())
        self.assertEqual(self.cat.pending(self.cat.key(self.landing)), {})
        self.assertEqual(self.cat.all_books()[0]["summary"], "keep me")


class DedupePlanTest(PlanTestCase):
    def test_no_duplicates_yields_an_empty_plan(self):
        self.touch("Networking/only.pdf")
        self.cat.stage("Networking", [Proposal("only.pdf", "only.pdf")], status="applied")
        self.assertTrue(dedupe_plan.build(self.context()).is_empty())

    def test_keeps_the_earliest_applied_copy(self):
        self.touch("Networking/dup.pdf")
        self.touch("Systems/dup.pdf")
        self.cat._conn.execute(
            "INSERT INTO books (directory, original_name, filename, status, applied_at)"
            " VALUES ('Networking','dup.pdf','dup.pdf','applied','2026-01-01 00:00:00')")
        self.cat._conn.execute(
            "INSERT INTO books (directory, original_name, filename, status, applied_at)"
            " VALUES ('Systems','dup.pdf','dup.pdf','applied','2026-06-01 00:00:00')")
        self.cat.commit()

        plan = dedupe_plan.build(self.context())
        self.assertEqual(plan.duplicates[0].keep.parent.name, "Networking")
        self.assertEqual(plan.duplicates[0].remove[0].parent.name, "Systems")

    def test_untracked_copies_are_never_the_one_kept(self):
        self.touch("Networking/dup.pdf")
        self.touch("Systems/dup.pdf")
        self.cat._conn.execute(
            "INSERT INTO books (directory, original_name, filename, status, applied_at)"
            " VALUES ('Systems','dup.pdf','dup.pdf','applied','2026-06-01 00:00:00')")
        self.cat.commit()

        plan = dedupe_plan.build(self.context())
        self.assertEqual(plan.duplicates[0].keep.parent.name, "Systems")

    def test_untracked_duplicate_contributes_no_db_id(self):
        self.touch("Networking/dup.pdf")
        self.touch("Systems/dup.pdf")
        plan = dedupe_plan.build(self.context())
        self.assertEqual(plan.duplicates[0].remove_db_ids, ())
        self.assertEqual(plan.record_count, 0)

    def test_payload_marks_only_genuinely_tracked_removals(self):
        """The original labelled every removal '(+ DB record)' unconditionally."""
        self.touch("Networking/dup.pdf")
        self.touch("Systems/dup.pdf")
        plan = dedupe_plan.build(self.context())
        removals = plan.payload()["duplicates"][0]["remove"]
        self.assertEqual([r["tracked"] for r in removals], [False])

    def test_payload_reports_what_would_be_deleted(self):
        self.touch("Networking/dup.pdf")
        self.touch("Systems/dup.pdf")
        self.cat.stage("Networking", [Proposal("gone.pdf", "gone.pdf")], status="applied")
        payload = dedupe_plan.build(self.context()).payload()
        self.assertEqual(payload["file_count"], 1)
        self.assertEqual([s["path"] for s in payload["stale"]], ["Networking/gone.pdf"])

    def test_stale_rows_are_found(self):
        self.cat.stage("Networking", [Proposal("gone.pdf", "gone.pdf")], status="applied")
        plan = dedupe_plan.build(self.context())
        self.assertEqual([s.filename for s in plan.stale], ["gone.pdf"])

    def test_helper_directories_are_not_scanned(self):
        self.touch("__landing/dup.pdf")
        self.touch("Networking/dup.pdf")
        self.assertEqual(dedupe_plan.build(self.context()).duplicates, [])

    def test_apply_deletes_files_and_rows(self):
        self.touch("Networking/dup.pdf")
        self.touch("Systems/dup.pdf")
        self.cat._conn.execute(
            "INSERT INTO books (directory, original_name, filename, status, applied_at)"
            " VALUES ('Systems','dup.pdf','dup.pdf','applied','2026-06-01 00:00:00')")
        self.cat.commit()
        plan = dedupe_plan.build(self.context())
        removed = plan.duplicates[0].remove[0]

        outcome = plan.apply(self.cat)

        self.assertEqual(outcome.ok, 1)
        self.assertFalse(removed.exists())


class SummaryPlanTest(PlanTestCase):
    def test_nothing_missing_yields_an_empty_plan(self):
        self.cat.stage("Networking", [Proposal("a.pdf", "A.pdf", "have one")],
                       status="applied")
        self.assertTrue(summaries_plan.build(self.context()).is_empty())

    def test_payload_reports_only_a_count(self):
        self.cat._conn.execute(
            "INSERT INTO books (directory, original_name, filename, status)"
            " VALUES ('Networking','a.pdf','A.pdf','applied')")
        self.cat.commit()
        self.assertEqual(summaries_plan.build(self.context()).payload(), {"missing": 1})

    def test_fills_in_what_the_model_returns(self):
        self.cat._conn.execute(
            "INSERT INTO books (directory, original_name, filename, status, summary)"
            " VALUES ('Networking','A.pdf','A.pdf','applied',NULL)")
        self.cat.commit()
        lookup = StubLookup(summaries={"A": "the summary"})
        plan = summaries_plan.build(self.context(lookup))

        outcome = plan.apply(self.cat)

        self.assertEqual(outcome.ok, 1)
        self.assertEqual(self.cat.all_books()[0]["summary"], "the summary")
        self.assertEqual(self.cat.missing_summaries(), [])

    def test_rows_the_model_skipped_stay_null_for_a_later_retry(self):
        self.cat._conn.execute(
            "INSERT INTO books (directory, original_name, filename, status, summary)"
            " VALUES ('Networking','A.pdf','A.pdf','applied',NULL)")
        self.cat.commit()
        plan = summaries_plan.build(self.context(StubLookup(summaries={})))

        outcome = plan.apply(self.cat)

        self.assertEqual(outcome.ok, 0)
        self.assertEqual(len(self.cat.missing_summaries()), 1)

    def test_empty_summary_is_stored_so_it_is_not_retried(self):
        self.cat._conn.execute(
            "INSERT INTO books (directory, original_name, filename, status, summary)"
            " VALUES ('Networking','A.pdf','A.pdf','applied',NULL)")
        self.cat.commit()
        plan = summaries_plan.build(self.context(StubLookup(summaries={"A": ""})))

        outcome = plan.apply(self.cat)

        self.assertEqual(outcome.ok, 0)          # nothing usable filled in
        self.assertEqual(self.cat.missing_summaries(), [])   # but not retried


if __name__ == "__main__":
    unittest.main()
