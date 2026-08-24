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
from typing import Callable, Iterable, Sequence

from .config import Config, ConfigError

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

Use the example books as anchors to pick the most appropriate existing category for each new book. If none fit well, propose a short new category name.

Filenames:
{_numbered(stems)}

Return ONLY a valid JSON array — no markdown fences, no explanation. Schema:
[{{"original": "<stem>", "title": "<Exact Published Title>", "category": "<Category Name>", "summary": "<One sentence, max 200 words>"}}]
"""


# ── response parsing ───────────────────────────────────────────────────────

def extract_json_array(text: str) -> list[dict]:
    """Pull the first JSON array out of a model reply.

    Tolerates the prose and markdown fences models add despite being asked not
    to, by taking everything between the outermost brackets. Returns [] when
    there is nothing parseable — callers treat that as "lookup failed".
    """
    text = (text or "").strip()
    start = text.find("[")
    end = text.rfind("]") + 1
    if start == -1 or end <= start:
        return []
    try:
        parsed = json.loads(text[start:end])
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


# ── client ─────────────────────────────────────────────────────────────────

class LookupClient:
    """One OpenAI client and one request path for every kind of lookup.

    The underlying client is built lazily, so constructing a LookupClient
    costs nothing and needs no API key until a lookup actually runs. Pass
    `client` to inject a stub in tests.
    """

    def __init__(self, config: Config, client=None):
        self.config = config
        self._client = client

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
        response = self.client.responses.create(
            model=self.config.model,
            tools=[WEB_SEARCH_TOOL],
            input=prompt,
        )
        return extract_json_array(response.output_text)

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
        """Like titles(), but also assigns each book a category.

        Returns [{"original": ..., "title": ..., "category": ..., "summary": ...}].
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
