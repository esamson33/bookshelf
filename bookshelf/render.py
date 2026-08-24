"""Terminal rendering: one aligned-table implementation for every preview.

The original script carried three hand-rolled renderers, each with its own
column arithmetic and its own `tabulate`-or-fallback branch. `tabulate` was
never installed, so the fallback was the only live path; this module keeps a
single renderer and drops the optional dependency.
"""

from __future__ import annotations

from typing import Iterable, Sequence

RULE = "─"


def truncate(s: str, n: int = 200) -> str:
    """Clip to n characters, marking the cut with an ellipsis."""
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def table(
    rows: Sequence[Sequence[object]],
    headers: Sequence[str],
    *,
    detail_last: bool = False,
    gap: int = 2,
) -> str:
    """Render `rows` as an aligned table.

    detail_last: render the final column on its own indented continuation
    line beneath each row instead of in a column of its own. Used for long
    free-text descriptions that would otherwise blow the line width.
    """
    if not rows:
        return ""

    headers = list(headers)
    detail_idx = len(headers) - 1 if detail_last else None
    cols = [i for i in range(len(headers)) if i != detail_idx]

    cells = [[_str(c) for c in row] for row in rows]
    widths = {
        i: max(len(headers[i]), max(len(r[i]) for r in cells)) + gap for i in cols
    }

    def line(values: Sequence[str]) -> str:
        parts = [values[i].ljust(widths[i]) for i in cols]
        return "".join(parts).rstrip()

    out = [line(headers)]
    out.append(RULE * max(len(out[0]), sum(widths.values())))

    indent = " " * min(widths[cols[0]], 5) if cols else ""
    for row in cells:
        out.append(line(row))
        if detail_idx is not None and row[detail_idx]:
            out.append(f"{indent}{row[detail_idx]}")

    return "\n".join(out)


def _str(value: object) -> str:
    return "" if value is None else str(value)


def bullet_list(items: Iterable[str], indent: str = "  ") -> str:
    """Simple indented listing, for previews that aren't tabular."""
    return "\n".join(f"{indent}{item}" for item in items)
