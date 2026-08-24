"""Proposal: the record that replaced untyped dicts and the
"Category/Title.pdf" overload of the stored name column."""

import unittest

from bookshelf.models import Proposal


class DbValueTest(unittest.TestCase):
    def test_shelved_book_stores_bare_filename(self):
        p = Proposal("raw.pdf", "Real Title.pdf")
        self.assertEqual(p.db_value, "Real Title.pdf")

    def test_landing_book_stores_category_prefix(self):
        p = Proposal("raw.pdf", "Real Title.pdf", category="Networking")
        self.assertEqual(p.db_value, "Networking/Real Title.pdf")

    def test_title_strips_the_extension(self):
        self.assertEqual(Proposal("a.pdf", "Real Title.pdf").title, "Real Title")


class RoundTripTest(unittest.TestCase):
    def assert_round_trips(self, stored_name):
        p = Proposal.from_db("raw.pdf", stored_name, "s")
        self.assertEqual(p.db_value, stored_name)

    def test_bare_filename(self):
        self.assert_round_trips("Real Title.pdf")

    def test_category_prefixed(self):
        self.assert_round_trips("Machine Learning and AI/Deep Learning.pdf")

    def test_title_containing_dots(self):
        self.assert_round_trips("Python 3.12 In Practice.pdf")

    def test_name_without_an_extension(self):
        # from_title would append .pdf; from_db must not invent one.
        self.assert_round_trips("No Extension")

    def test_category_with_spaces_and_punctuation(self):
        self.assert_round_trips("Non-Technical and Career/So Good They Can't Ignore You.pdf")

    def test_from_db_splits_category_from_filename(self):
        p = Proposal.from_db("raw.pdf", "Networking/TCP-IP.pdf", "sum")
        self.assertEqual((p.category, p.filename, p.summary),
                         ("Networking", "TCP-IP.pdf", "sum"))

    def test_from_db_without_category(self):
        p = Proposal.from_db("raw.pdf", "TCP-IP.pdf")
        self.assertIsNone(p.category)
        self.assertEqual(p.filename, "TCP-IP.pdf")

    def test_from_db_normalises_null_summary_to_empty(self):
        self.assertEqual(Proposal.from_db("a.pdf", "A.pdf", None).summary, "")


class FromTitleTest(unittest.TestCase):
    def test_appends_the_pdf_extension(self):
        p = Proposal.from_title("raw.pdf", "Real Title")
        self.assertEqual(p.filename, "Real Title.pdf")

    def test_carries_category_and_summary(self):
        p = Proposal.from_title("raw.pdf", "T", "sum", "Networking")
        self.assertEqual(p.db_value, "Networking/T.pdf")
        self.assertEqual(p.summary, "sum")

    def test_none_summary_becomes_empty_string(self):
        self.assertEqual(Proposal.from_title("a.pdf", "T", None).summary, "")


class IsNoopTest(unittest.TestCase):
    def test_true_when_already_correctly_named(self):
        self.assertTrue(Proposal("Real.pdf", "Real.pdf").is_noop)

    def test_false_when_the_name_changes(self):
        self.assertFalse(Proposal("raw.pdf", "Real.pdf").is_noop)

    def test_false_for_a_move_even_if_the_name_matches(self):
        # Same filename but destined for a category — it still has to move.
        self.assertFalse(Proposal("Real.pdf", "Real.pdf", category="Networking").is_noop)


class WithCategoryTest(unittest.TestCase):
    def test_returns_a_new_proposal_leaving_the_original_alone(self):
        original = Proposal("raw.pdf", "T.pdf", category="Old")
        updated = original.with_category("New")
        self.assertEqual(original.category, "Old")
        self.assertEqual(updated.category, "New")
        self.assertEqual(updated.db_value, "New/T.pdf")

    def test_can_add_a_category_to_a_bare_proposal(self):
        self.assertEqual(Proposal("raw.pdf", "T.pdf").with_category("C").db_value, "C/T.pdf")


if __name__ == "__main__":
    unittest.main()
