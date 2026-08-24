"""The single table renderer that replaced three hand-rolled ones."""

import unittest

from bookshelf.render import bullet_list, table, truncate


class TruncateTest(unittest.TestCase):
    def test_short_string_is_untouched(self):
        self.assertEqual(truncate("abc", 10), "abc")

    def test_exact_length_is_untouched(self):
        self.assertEqual(truncate("abcde", 5), "abcde")

    def test_long_string_is_clipped_with_ellipsis(self):
        self.assertEqual(truncate("abcdef", 5), "abcd…")
        self.assertEqual(len(truncate("x" * 500)), 200)

    def test_none_becomes_empty(self):
        self.assertEqual(truncate(None), "")


class TableTest(unittest.TestCase):
    ROWS = [(1, "raw_name.pdf", "Proper Title.pdf", "A summary."),
            (2, "b.pdf", "Another.pdf", "")]
    HEADERS = ["#", "Original", "Renamed to", "Description"]

    def test_empty_rows_render_as_empty_string(self):
        self.assertEqual(table([], self.HEADERS), "")

    def test_columns_align_to_widest_cell(self):
        out = table(self.ROWS, self.HEADERS).splitlines()
        starts = [line.index("Proper Title.pdf") if "Proper Title.pdf" in line
                  else line.index("Another.pdf") for line in out[2:]]
        self.assertEqual(len(set(starts)), 1, "value column should start at one offset")

    def test_header_and_rule_precede_the_rows(self):
        out = table(self.ROWS, self.HEADERS).splitlines()
        self.assertTrue(out[0].startswith("#"))
        self.assertEqual(set(out[1]), {"─"})
        self.assertEqual(len(out), 4)

    def test_detail_last_moves_final_column_to_its_own_line(self):
        out = table(self.ROWS, self.HEADERS, detail_last=True).splitlines()
        self.assertNotIn("Description", out[0])
        self.assertIn("A summary.", out[3])
        self.assertNotIn("raw_name.pdf", out[3])

    def test_detail_last_skips_blank_details(self):
        out = table(self.ROWS, self.HEADERS, detail_last=True).splitlines()
        # header, rule, row1, detail1, row2 — and no blank line for row2's detail
        self.assertEqual(len(out), 5)

    def test_none_cells_render_as_empty(self):
        out = table([(1, None, "X.pdf", None)], self.HEADERS)
        self.assertIn("X.pdf", out)

    def test_rows_have_no_trailing_whitespace(self):
        for line in table(self.ROWS, self.HEADERS).splitlines():
            self.assertEqual(line, line.rstrip())


class BulletListTest(unittest.TestCase):
    def test_indents_each_item(self):
        self.assertEqual(bullet_list(["a", "b"]), "  a\n  b")

    def test_empty_is_empty(self):
        self.assertEqual(bullet_list([]), "")


if __name__ == "__main__":
    unittest.main()
