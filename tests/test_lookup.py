"""LookupClient: prompt construction, JSON extraction, batching.

No network — a stub client records prompts and replays canned replies.
"""

import tempfile
import unittest
from pathlib import Path

from bookshelf.config import Config, ConfigError
from bookshelf.lookup import (
    LookupClient,
    categorized_titles_prompt,
    extract_json_array,
    summaries_prompt,
    titles_prompt,
)


class StubResponse:
    def __init__(self, text):
        self.output_text = text


class StubResponses:
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text = self._replies.pop(0) if self._replies else "[]"
        return StubResponse(text)


class StubClient:
    def __init__(self, *replies):
        self.responses = StubResponses(replies)


def make_config(tmp: Path) -> Config:
    return Config(app_dir=tmp, data_dir=tmp / "data",
                  db_path=tmp / "cat.db", config_file=tmp / "conf.toml")


class ExtractJsonArrayTest(unittest.TestCase):
    def test_an_array_missing_its_closing_bracket_is_salvaged(self):
        """gpt-4.1-mini really sent this: one complete row, no final "]"."""
        text = ('[{"original": "x", "title": "T", '
                '"categories": [{"category": "A", "score": 100}], "summary": "s"} ')
        rows = extract_json_array(text)
        self.assertEqual([(r["original"], r["title"]) for r in rows], [("x", "T")])

    def test_a_truncated_array_keeps_the_complete_rows(self):
        text = '[{"original": "a"}, {"original": "b"}, {"original": "c", "ti'
        self.assertEqual([r["original"] for r in extract_json_array(text)], ["a", "b"])

    def test_a_bare_object_is_one_row_not_its_nested_array(self):
        """A one-file reply sent as an object must not yield its "categories"."""
        text = ('{"original": "x", "title": "T", '
                '"categories": [{"category": "A", "score": 90}]}')
        rows = extract_json_array(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["original"], "x")

    def test_rows_with_nested_arrays_parse_whole(self):
        text = ('[{"original": "x", "categories": [{"category": "A"}]}, '
                '{"original": "y", "categories": []}]')
        self.assertEqual([r["original"] for r in extract_json_array(text)], ["x", "y"])

    def test_plain_array(self):
        self.assertEqual(extract_json_array('[{"a": 1}]'), [{"a": 1}])

    def test_markdown_fenced_array(self):
        text = '```json\n[{"a": 1}]\n```'
        self.assertEqual(extract_json_array(text), [{"a": 1}])

    def test_array_wrapped_in_prose(self):
        text = 'Sure! Here are the results:\n[{"a": 1}]\nHope that helps.'
        self.assertEqual(extract_json_array(text), [{"a": 1}])

    def test_malformed_json_yields_empty(self):
        self.assertEqual(extract_json_array('[{"a": 1,,}]'), [])

    def test_no_array_yields_empty(self):
        self.assertEqual(extract_json_array("I could not find these books."), [])

    def test_empty_and_none_yield_empty(self):
        self.assertEqual(extract_json_array(""), [])
        self.assertEqual(extract_json_array(None), [])

    def test_scalar_json_yields_empty(self):
        # A bare object is a one-row reply (see above); a bare scalar is not.
        self.assertEqual(extract_json_array('"just a string"'), [])
        self.assertEqual(extract_json_array('42'), [])

    def test_non_dict_entries_are_dropped(self):
        self.assertEqual(extract_json_array('[{"a": 1}, "junk", 7]'), [{"a": 1}])


class PromptTest(unittest.TestCase):
    def test_titles_prompt_numbers_stems_from_one(self):
        out = titles_prompt(["a", "b"])
        self.assertIn("1. a", out)
        self.assertIn("2. b", out)

    def test_publisher_hint_replaces_the_generic_line(self):
        with_hint = titles_prompt(["a"], "Manning")
        without = titles_prompt(["a"])
        self.assertIn("published by Manning", with_hint)
        self.assertNotIn("Packt, O'Reilly", with_hint)
        self.assertIn("Packt, O'Reilly", without)

    def test_categories_are_sorted_with_sample_anchors(self):
        out = categorized_titles_prompt(
            ["x"], ["Zebra", "Alpha"], {"Alpha": ["One", "Two"]}
        )
        self.assertLess(out.index("- Alpha"), out.index("- Zebra"))
        self.assertIn('- Alpha (e.g. "One", "Two")', out)

    def test_category_without_samples_has_no_examples(self):
        out = categorized_titles_prompt(["x"], ["Bare"], {})
        self.assertIn("- Bare\n", out)
        self.assertNotIn("e.g.", out)

    def test_categorized_prompt_asks_for_scored_candidates(self):
        out = categorized_titles_prompt(["x"], ["Networking"], {})
        self.assertIn("up to 3 candidate categories", out)
        self.assertIn('"categories": [{"category": "<Category Name>", "score": <0-100>}]', out)

    def test_summaries_prompt_lists_titles(self):
        self.assertIn("1. Some Book", summaries_prompt(["Some Book"]))


class LookupClientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = make_config(Path(self.tmp.name))

    def test_titles_returns_parsed_rows(self):
        stub = StubClient('[{"original": "raw", "title": "Real", "summary": "s"}]')
        rows = LookupClient(self.cfg, stub).titles(["raw"])
        self.assertEqual(rows, [{"original": "raw", "title": "Real", "summary": "s"}])

    def test_request_carries_model_and_web_search_tool(self):
        stub = StubClient("[]")
        LookupClient(self.cfg, stub).titles(["raw"])
        call = stub.responses.calls[0]
        self.assertEqual(call["model"], self.cfg.model)
        self.assertEqual(call["tools"], [{"type": "web_search_preview"}])

    def test_unparseable_reply_degrades_to_empty(self):
        stub = StubClient("The web search returned nothing useful.")
        self.assertEqual(LookupClient(self.cfg, stub).titles(["raw"]), [])

    def test_api_errors_propagate(self):
        class Boom:
            class responses:
                @staticmethod
                def create(**kw):
                    raise RuntimeError("401 unauthorized")

        with self.assertRaises(RuntimeError):
            LookupClient(self.cfg, Boom()).titles(["raw"])

    def test_summaries_batches_and_merges(self):
        titles = [f"B{i}" for i in range(45)]
        stub = StubClient(
            '[{"title": "B0", "summary": "one"}]',
            '[{"title": "B20", "summary": "two"}]',
            '[{"title": "B40", "summary": "three"}]',
        )
        result = LookupClient(self.cfg, stub).summaries(titles)
        self.assertEqual(len(stub.responses.calls), 3)
        self.assertEqual(result, {"B0": "one", "B20": "two", "B40": "three"})

    def test_summaries_batch_sizes_are_20_20_5(self):
        titles = [f"B{i}" for i in range(45)]
        seen = []
        LookupClient(self.cfg, StubClient()).summaries(
            titles, progress=lambda i, n, size: seen.append((i, n, size))
        )
        self.assertEqual(seen, [(1, 3, 20), (2, 3, 20), (3, 3, 5)])

    def test_summaries_skips_rows_missing_a_field(self):
        stub = StubClient('[{"title": "A", "summary": "ok"}, {"title": "B"}, {"summary": "x"}]')
        self.assertEqual(LookupClient(self.cfg, stub).summaries(["A", "B"]), {"A": "ok"})

    def test_summaries_of_nothing_makes_no_calls(self):
        stub = StubClient()
        self.assertEqual(LookupClient(self.cfg, stub).summaries([]), {})
        self.assertEqual(stub.responses.calls, [])

    def _watched(self, client):
        seen = []
        lookup = LookupClient(self.cfg, client,
                              on_call=lambda state, error: seen.append((state, error)))
        return lookup, seen

    def test_on_call_reports_busy_then_ok(self):
        lookup, seen = self._watched(StubClient('[{"original": "raw", "title": "Real"}]'))
        lookup.titles(["raw"])
        self.assertEqual(seen, [("busy", None), ("ok", None)])

    def test_an_unreadable_reply_is_logged(self):
        import contextlib, io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            LookupClient(self.cfg, StubClient("no json here")).titles(["raw"])
        self.assertIn("unreadable model reply: 'no json here'", err.getvalue())

    def test_on_call_reports_unparseable_reply_as_error(self):
        lookup, seen = self._watched(StubClient("nothing useful"))
        lookup.titles(["raw"])
        self.assertEqual(seen, [("busy", None), ("error", "Reply contained no JSON array")])

    def test_on_call_reports_api_error_and_it_still_propagates(self):
        class Boom:
            class responses:
                @staticmethod
                def create(**kw):
                    raise RuntimeError("401 unauthorized")

        lookup, seen = self._watched(Boom())
        with self.assertRaises(RuntimeError):
            lookup.titles(["raw"])
        self.assertEqual(seen, [("busy", None), ("error", "RuntimeError: 401 unauthorized")])

    def test_on_call_fires_per_summary_batch(self):
        lookup, seen = self._watched(StubClient(
            '[{"title": "B0", "summary": "one"}]',
            '[{"title": "B20", "summary": "two"}]',
        ))
        lookup.summaries([f"B{i}" for i in range(25)])
        self.assertEqual([s for s, _ in seen], ["busy", "ok", "busy", "ok"])

    def test_a_failing_on_call_does_not_break_the_lookup(self):
        def broken(state, error):
            raise ValueError("indicator bug")

        stub = StubClient('[{"original": "raw", "title": "Real"}]')
        rows = LookupClient(self.cfg, stub, on_call=broken).titles(["raw"])
        self.assertEqual(rows, [{"original": "raw", "title": "Real"}])

    def test_client_is_lazy_so_no_key_is_needed_until_a_lookup_runs(self):
        # Constructing must not touch the (absent) config file or env.
        LookupClient(self.cfg)

    def test_missing_key_raises_config_error_on_first_use(self):
        import os

        saved = os.environ.pop("OPENAI_API_KEY", None)
        try:
            with self.assertRaises(ConfigError):
                LookupClient(self.cfg).titles(["raw"])
        finally:
            if saved is not None:
                os.environ["OPENAI_API_KEY"] = saved


if __name__ == "__main__":
    unittest.main()
