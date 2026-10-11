"""OpenAI lookups: proper titles, categories, and summaries.

The original script carried three near-identical functions, each building a
numbered list, calling responses.create() with the same model and tool, then
scraping a JSON array out of the reply and swallowing the parse error. That
scrape was copy-pasted verbatim three times; it lives once here, in
_ask_json(), which is also the only place retries or response logging would
need to go.

The prompt text is preserved exactly as it was — the model's output depends on
it, so it is deliberately untouched by this refactor.
"""

from __future__ import annotations

import json
import sys
from typing import Callable, Sequence

from .config import Config, ConfigError
from .models import MAX_CANDIDATES

SUMMARY_BATCH_SIZE = 20

WEB_SEARCH_TOOL = {"type": "web_search_preview"}


def _numbered(items: Sequence[str]) -> str:
    return "\n".join(f"{i + 1}. {item}" for i, item in enumerate(items))


# ── prompt builders ────────────────────────────────────────────────────────

def summaries_prompt(titles: Sequence[str]) -> str:
    return f"""\
For each book title below, search the web and write a one-sentence summary (max 200 words).

Titles:
{_numbered(titles)}

Return ONLY a valid JSON array — no markdown fences, no explanation. Schema:
[{{"title": "<Exact Title As Given>", "summary": "<One sentence, max 200 words>"}}]
"""


def titles_prompt(stems: Sequence[str], publisher_hint: str = "") -> str:
    hint = (
        f"These are books published by {publisher_hint}."
        if publisher_hint
        else "Publishers may include Packt, O'Reilly, Manning, No Starch Press, or others."
    )
    return f"""\
I have PDF book filenames (stems only, no extension) that need their proper published titles.
{hint}
Search the web to find the exact, properly-cased title for each one.

Filenames:
{_numbered(stems)}

Return ONLY a valid JSON array — no markdown fences, no explanation. Schema:
[{{"original": "<stem>", "title": "<Exact Published Title>", "summary": "<One sentence, max 200 words>"}}]
"""


def categorized_titles_prompt(
    stems: Sequence[str],
    categories: Sequence[str],
    samples: dict[str, list[str]] | None = None,
) -> str:
    lines = []
    for cat in sorted(categories):
        books = (samples or {}).get(cat, [])
        if books:
            examples = ", ".join(f'"{b}"' for b in books)
            lines.append(f"- {cat} (e.g. {examples})")
        else:
            lines.append(f"- {cat}")
    cat_list = "\n".join(lines)

    return f"""\
I have PDF book filenames (stems only, no extension) that need their proper published titles and subject categories.
Search the web to find the exact, properly-cased title for each one.

Existing categories with representative books already shelved in each:
{cat_list}

Use the example books as anchors to judge how well each existing category fits each new book. For each book, give up to {MAX_CANDIDATES} candidate categories, best first, each with a score from 0 to 100 for how appropriate it is. A candidate may be an existing category or a short new category name; propose a new one whenever it would fit noticeably better than the existing ones, even if an existing category partly fits. Use the existing category's name exactly as written. Category names must not contain "/".

Filenames:
{_numbered(stems)}

Return ONLY a valid JSON array — no markdown fences, no explanation. Schema:
[{{"original": "<stem>", "title": "<Exact Published Title>", "categories": [{{"category": "<Category Name>", "score": <0-100>}}], "summary": "<One sentence, max 200 words>"}}]
"""


# ── response parsing ───────────────────────────────────────────────────────

def extract_json_array(text: str) -> list[dict]:
    """Pull the JSON array of rows out of a model reply.

    Tolerates the prose and markdown fences models add despite being asked not
    to. Decoding starts at whichever of "[" or "{" comes first, so a reply
    that is a single bare object — which models sometimes send for a one-item
    request — is taken as a one-row array, rather than its first nested array
    (a row's "categories") being mistaken for the rows. Returns [] when there
    is nothing parseable — callers treat that as "lookup failed".

    An array the model never closed (seen from gpt-4.1-mini: `[{...} ` with
    no final "]") is salvaged row by row, keeping every complete object.
    """
    text = (text or "").strip()
    starts = [i for i in (text.find("["), text.find("{")) if i != -1]
    if not starts:
        return []
    start = min(starts)
    try:
        parsed, _ = _DECODER.raw_decode(text, start)
    except json.JSONDecodeError:
        parsed = _salvage_rows(text, start) if text[start] == "[" else []
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


_DECODER = json.JSONDecoder()


def _salvage_rows(text: str, start: int) -> list:
    """The complete objects of a broken array beginning at text[start] == "[".

    Reads one element at a time and stops at the first that does not decode —
    so a missing "]", or a reply cut off mid-row, still yields the rows
    before it.
    """
    rows, i = [], start + 1
    while True:
        while i < len(text) and text[i] in " \t\r\n,":
            i += 1
        if i >= len(text) or text[i] == "]":
            return rows
        try:
            item, i = _DECODER.raw_decode(text, i)
        except json.JSONDecodeError:
            return rows
        rows.append(item)


# ── client ─────────────────────────────────────────────────────────────────

class LookupClient:
    """One OpenAI client and one request path for every kind of lookup.

    The underlying client is built lazily, so constructing a LookupClient
    costs nothing and needs no API key until a lookup actually runs. Pass
    `client` to inject a stub in tests.

    `on_call(state, error)` is told about every request: "busy" when it is
    sent, then "ok" or "error" when it settles — the server turns these into
    the UI's status bar.
    """

    def __init__(
        self,
        config: Config,
        client=None,
        on_call: Callable[[str, str | None], None] | None = None,
    ):
        self.config = config
        self._client = client
        self.on_call = on_call

    def _notify(self, state: str, error: str | None = None) -> None:
        # The indicator is cosmetic; it must never break a lookup.
        if self.on_call is None:
            return
        try:
            self.on_call(state, error)
        except Exception:                             # noqa: BLE001
            pass

    @property
    def client(self):
        if self._client is None:
            # Imported late so modes that never call the API (dedupe) work
            # without the package installed.
            try:
                from openai import OpenAI
            except ImportError:
                raise ConfigError(
                    "'openai' package not found. Run: pip install openai"
                ) from None

            self._client = OpenAI(api_key=self.config.api_key())
        return self._client

    def _ask_json(self, prompt: str) -> list[dict]:
        """Send one prompt and return the JSON array it replies with.

        Errors raised by the API itself (auth, network, rate limits) are left
        to propagate — only an unparseable reply degrades to [].
        """
        self._notify("busy")
        try:
            response = self.client.responses.create(
                model=self.config.model,
                tools=[WEB_SEARCH_TOOL],
                input=prompt,
            )
        except Exception as e:
            self._notify("error", f"{type(e).__name__}: {e}")
            raise
        rows = extract_json_array(response.output_text)
        if rows:
            self._notify("ok")
        else:
            self._notify("error", "Reply contained no JSON array")
            # Lands in the server's error log: without the reply itself, a
            # malformed answer is indistinguishable from a bad key.
            print(f"bookshelf: unreadable model reply: {response.output_text!r:.4000}",
                  file=sys.stderr, flush=True)
        return rows

    # ── lookups ──────────────────────────────────────────────────────────

    def titles(self, stems: Sequence[str], publisher_hint: str = "") -> list[dict]:
        """Resolve filename stems to proper titles.

        Returns [{"original": stem, "title": ..., "summary": ...}].
        """
        return self._ask_json(titles_prompt(stems, publisher_hint))

    def categorized_titles(
        self,
        stems: Sequence[str],
        categories: Sequence[str],
        samples: dict[str, list[str]] | None = None,
    ) -> list[dict]:
        """Like titles(), but also suggests up to MAX_CANDIDATES categories.

        Returns [{"original": ..., "title": ...,
                  "categories": [{"category": ..., "score": 0-100}, ...],
                  "summary": ...}].
        """
        return self._ask_json(categorized_titles_prompt(stems, categories, samples))

    def summaries(
        self,
        titles: Sequence[str],
        batch_size: int = SUMMARY_BATCH_SIZE,
        progress: Callable[[int, int, int], None] | None = None,
    ) -> dict[str, str]:
        """Fetch summaries for proper titles, batched to stay within limits.

        Returns {title: summary}. `progress` is called with
        (batch_number, batch_count, size) before each request.
        """
        batches = _chunk(titles, batch_size)
        results: dict[str, str] = {}
        for index, batch in enumerate(batches, 1):
            if progress:
                progress(index, len(batches), len(batch))
            for row in self._ask_json(summaries_prompt(batch)):
                title, summary = row.get("title"), row.get("summary")
                if title is not None and summary is not None:
                    results[title] = summary
        return results


def _chunk(items: Sequence[str], size: int) -> list[Sequence[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]
