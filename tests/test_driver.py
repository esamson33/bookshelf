"""The shared preview/confirm/apply loop."""

import unittest

from bookshelf.driver import render, run_plan
from bookshelf.plans import Outcome, Plan, Preview, Section


class FakePlan(Plan):
    def __init__(self, empty=False, **kw):
        self._empty = empty
        self.applied = 0
        self.staged = 0
        self.edits = []
        for key, value in kw.items():
            setattr(self, key, value)

    row_count = 2

    def is_empty(self):
        return self._empty

    def preview(self):
        return Preview(sections=[Section(headers=["#", "A"], rows=[(1, "x"), (2, "y")])])

    def stage(self, catalog):
        self.staged += 1

    def apply(self, catalog):
        self.applied += 1
        return Outcome(1, 0, "Done. 1 thing.")


class EditablePlan(FakePlan):
    supports_edit = True

    def edit(self, index):
        if not 0 <= index < self.row_count:
            return False
        self.edits.append(index)
        return True


def drive(plan, answers, **kw):
    out = []
    replies = list(answers)
    code = run_plan(plan, catalog=None, ask=lambda _: replies.pop(0),
                    out=lambda *a: out.append(" ".join(str(x) for x in a)), **kw)
    return code, "\n".join(out)


class EmptyPlanTest(unittest.TestCase):
    def test_prints_empty_message_and_does_not_apply(self):
        plan = FakePlan(empty=True, empty_message="Nothing doing.")
        code, out = drive(plan, [])
        self.assertEqual(code, 0)
        self.assertIn("Nothing doing.", out)
        self.assertEqual(plan.applied, 0)
        self.assertEqual(plan.staged, 0)


class ConfirmTest(unittest.TestCase):
    def test_yes_applies(self):
        plan = FakePlan()
        code, out = drive(plan, ["y"])
        self.assertEqual((code, plan.applied), (0, 1))
        self.assertIn("Done. 1 thing.", out)

    def test_uppercase_yes_applies(self):
        plan = FakePlan()
        drive(plan, ["Y"])
        self.assertEqual(plan.applied, 1)

    def test_no_aborts_without_applying(self):
        plan = FakePlan(abort_message="Aborted.")
        code, out = drive(plan, ["n"])
        self.assertEqual((code, plan.applied), (0, 0))
        self.assertIn("Aborted.", out)

    def test_empty_answer_aborts(self):
        plan = FakePlan()
        drive(plan, [""])
        self.assertEqual(plan.applied, 0)

    def test_proposals_are_staged_before_the_prompt(self):
        """A declined run still leaves the proposals resumable."""
        plan = FakePlan()
        drive(plan, ["n"])
        self.assertEqual(plan.staged, 1)

    def test_assume_yes_never_prompts(self):
        plan = FakePlan()
        code = run_plan(plan, catalog=None, assume_yes=True,
                        ask=lambda _: (_ for _ in ()).throw(AssertionError("prompted")),
                        out=lambda *a: None)
        self.assertEqual((code, plan.applied), (0, 1))


class NonEditablePromptTest(unittest.TestCase):
    """Plans without an edit affordance are strictly y-or-abort."""

    def test_a_digit_aborts_rather_than_reprompting(self):
        plan = FakePlan()
        code, out = drive(plan, ["3"])
        self.assertEqual(plan.applied, 0)
        self.assertNotIn("Invalid row number", out)

    def test_junk_aborts(self):
        plan = FakePlan()
        drive(plan, ["maybe"])
        self.assertEqual(plan.applied, 0)


class EditablePromptTest(unittest.TestCase):
    def test_row_number_is_routed_to_edit_then_reprompts(self):
        plan = EditablePlan()
        code, _ = drive(plan, ["2", "y"])
        self.assertEqual(plan.edits, [1])
        self.assertEqual(plan.applied, 1)

    def test_out_of_range_row_reports_the_valid_range(self):
        plan = EditablePlan()
        _, out = drive(plan, ["9", "y"])
        self.assertIn("Invalid row number — enter 1–2.", out)
        self.assertEqual(plan.edits, [])

    def test_junk_asks_again_instead_of_aborting(self):
        plan = EditablePlan()
        _, out = drive(plan, ["huh", "y"])
        self.assertIn("Enter y, n, or a row number.", out)
        self.assertEqual(plan.applied, 1)

    def test_n_still_aborts(self):
        plan = EditablePlan()
        drive(plan, ["n"])
        self.assertEqual(plan.applied, 0)

    def test_preview_is_reshown_after_an_edit(self):
        plan = EditablePlan()
        _, out = drive(plan, ["1", "y"])
        self.assertEqual(out.count("#  A"), 2)


class NoConfirmPlanTest(unittest.TestCase):
    def test_applies_without_preview_or_prompt(self):
        plan = FakePlan(confirm_required=False)
        code, out = drive(plan, [])
        self.assertEqual((code, plan.applied), (0, 1))
        self.assertNotIn("#  A", out)


class ExitCodeTest(unittest.TestCase):
    def test_errors_still_exit_zero_as_before(self):
        class Failing(FakePlan):
            def apply(self, catalog):
                return Outcome(0, 3, "Done. 0 renamed, 3 error(s).")

        code, out = drive(Failing(), ["y"])
        self.assertEqual(code, 0)
        self.assertIn("3 error(s)", out)


class RenderTest(unittest.TestCase):
    def test_renders_heading_table_lines_and_footer(self):
        out = []
        render(Preview(
            sections=[
                Section(heading="H", headers=["#", "A"], rows=[(1, "x")]),
                Section(lines=["  a line"]),
            ],
            footer="the footer",
        ), out=lambda *a: out.append(" ".join(str(x) for x in a)))
        joined = "\n".join(out)
        for expected in ("H", "#  A", "  a line", "the footer"):
            self.assertIn(expected, joined)


if __name__ == "__main__":
    unittest.main()
