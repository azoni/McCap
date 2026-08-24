import unicodedata
from typing import Dict, List, Optional, Tuple

from .helpers import humanize, when_str

# Discord rejects an embed field value longer than 1024 characters with a 400,
# which fails the whole interaction. A long table therefore has to be split
# before it is sent. Budget leaves headroom for the code fence.
EMBED_FIELD_LIMIT = 1024
CHUNK_BUDGET = 980
# Discord also caps the whole embed at 6000 characters across every part, so
# splitting a table into more fields only moves the failure. Leave room for the
# title, description and footer.
EMBED_TOTAL_LIMIT = 6000
EMBED_TOTAL_BUDGET = 5200


def dwidth(s: str) -> int:
    """Rendered width of a string in a monospace block.

    ``len()`` counts code points, but East Asian wide and fullwidth characters
    take two cells — so a name like 模因季节 is 4 code points and 8 columns, and
    padding by len() pushed every column after it out of alignment. Several
    tokens in real use have Chinese names, so this is not hypothetical.
    """
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _pad(s: str, w: int, align: str) -> str:
    gap = max(0, w - dwidth(s))
    if align == "r":
        return " " * gap + s
    if align == "c":
        left = gap // 2
        return " " * left + s + " " * (gap - left)
    return s + " " * gap


def _clip(s: str, w: int) -> str:
    """Truncate to a display width, not a code-point count."""
    if dwidth(s) <= w:
        return s
    out, used = "", 0
    for ch in s:
        cw = dwidth(ch)
        if used + cw > w - 1:
            break
        out += ch
        used += cw
    return out + "…"


def _normalise_aligns(ncols: int, aligns: Optional[List[str]]) -> List[str]:
    out = (aligns or ["l"] * ncols)[:ncols]
    out += ["l"] * (ncols - len(out))
    return out


def _widths(headers: List[str], rows: List[List[str]], max_width: int) -> List[int]:
    widths = []
    for i in range(len(headers)):
        longest = max([dwidth(str(headers[i]))] + [dwidth(str(r[i])) for r in rows if i < len(r)] or [0])
        widths.append(min(max(1, longest), max_width))
    return widths


def _cell(v, w: int, a: str) -> str:
    return _pad(_clip(str(v).replace("\n", " "), w), w, a)


def _render(headers: List[str], rows: List[List[str]], aligns: List[str], widths: List[int]) -> str:
    head = "  ".join(_cell(h, w, "l") for h, w in zip(headers, widths))
    sep = "  ".join("─" * w for w in widths)
    body = "\n".join("  ".join(_cell(v, w, a) for v, w, a in zip(r, widths, aligns)) for r in rows) or "—"
    return f"```\n{head}\n{sep}\n{body}\n```"


def fixed_table(
    headers: List[str],
    rows: List[List[str]],
    aligns: Optional[List[str]] = None,
    max_width: int = 18,
) -> str:
    """Render a monospace table, sizing each column to its widest cell.

    Returns one string of any length. Callers putting this into an embed should
    prefer ``add_table_fields``, which splits tables that would be rejected.
    """
    ncols = len(headers)
    a = _normalise_aligns(ncols, aligns)
    return _render(headers, rows, a, _widths(headers, rows, max_width))


def table_chunks(
    headers: List[str],
    rows: List[List[str]],
    aligns: Optional[List[str]] = None,
    max_width: int = 18,
    budget: int = CHUNK_BUDGET,
) -> List[List[List[str]]]:
    """Group rows into batches that each render inside one embed field.

    Returns the row groups rather than rendered strings so the caller can size
    columns once across everything and keep the pieces aligned.
    """
    if not rows:
        return [[]]

    ncols = len(headers)
    a = _normalise_aligns(ncols, aligns)
    widths = _widths(headers, rows, max_width)

    # Fence + header + separator cost, paid once per chunk.
    overhead = len(_render(headers, [], a, widths))
    row_len = [len("  ".join(_cell(v, w, al) for v, w, al in zip(r, widths, a))) + 1 for r in rows]

    groups: List[List[List[str]]] = []
    start = 0
    while start < len(rows):
        used = overhead
        end = start
        while end < len(rows) and (used + row_len[end]) <= budget:
            used += row_len[end]
            end += 1
        if end == start:  # one row alone exceeds the budget
            end = start + 1
        groups.append(rows[start:end])
        start = end
    return groups


def embed_len(embed) -> int:
    """Characters Discord counts toward the 6000-per-embed cap."""
    n = len(getattr(embed, "title", "") or "") + len(getattr(embed, "description", "") or "")
    footer = getattr(embed, "footer", None)
    n += len(getattr(footer, "text", "") or "") if footer is not None else 0
    author = getattr(embed, "author", None)
    n += len(getattr(author, "name", "") or "") if author is not None else 0
    for f in (getattr(embed, "fields", None) or []):
        if isinstance(f, tuple):  # test doubles store (name, value)
            n += len(f[0] or "") + len(f[1] or "")
        else:
            n += len(getattr(f, "name", "") or "") + len(getattr(f, "value", "") or "")
    return n


def add_table_fields(
    embed,
    name: str,
    headers: List[str],
    rows: List[List[str]],
    aligns: Optional[List[str]] = None,
    max_width: int = 18,
    max_fields: int = 6,
    total_budget: int = EMBED_TOTAL_BUDGET,
) -> Tuple[int, int]:
    """Add a table to an embed, splitting it across fields as needed.

    Respects both Discord caps. Splitting for the 1024-per-field limit alone
    just moves the failure: ten 980-character fields is 9800, well past the
    6000-per-embed total. So fields are also stopped once the embed's own
    budget is spent, counting what the embed already holds.

    Returns ``(rows_shown, rows_total)``. Surplus rows are dropped and reported
    back so the caller can say so — quietly showing a subset would read as
    "that is everything".
    """
    total = len(rows)
    a = _normalise_aligns(len(headers), aligns)
    widths = _widths(headers, rows, max_width)

    shown = 0
    used = embed_len(embed)
    for i, group in enumerate(table_chunks(headers, rows, aligns, max_width)):
        if i >= max_fields:
            break
        label = name if i == 0 else f"{name} (cont.)"
        value = _render(headers, group, a, widths)
        if used + len(label) + len(value) > total_budget:
            break
        embed.add_field(name=label, value=value, inline=False)
        used += len(label) + len(value)
        shown += len(group)

    # An empty table still emits one field: table_chunks returns a single empty
    # group, so the loop above has already handled it.
    return shown, total


ALERTS_HEADERS = ["When", "Token", "Dir", "Trigger", "Current", "By"]
ALERTS_ALIGNS = ["l", "l", "c", "r", "r", "l"]


def alerts_rows(events, name_by_id: Dict[int, str], current_by_ca: Dict[str, Optional[float]]) -> List[List[str]]:
    """Rows for the fired-alert history, so callers can chunk them."""
    rows = []
    for e in events:
        kind = getattr(e, "kind", "level")
        if kind == "move":
            dir_sym = "▲" if e.direction == "up" else "▼"
            trigger = f"{e.target_mc:g}%"
        else:
            dir_sym = "≥" if e.direction == "above" else "≤"
            trigger = f"${humanize(e.target_mc)}"
        rows.append([
            when_str(e.ts),
            e.symbol or e.name,
            dir_sym,
            trigger,
            f"${humanize(current_by_ca.get(e.ca))}",
            name_by_id.get(e.creator_id, f"user:{e.creator_id}"),
        ])
    return rows


def alerts_table(events, name_by_id: Dict[int, str], current_by_ca: Dict[str, Optional[float]]) -> str:
    """Single-string fired-alert history. Kept for callers that don't chunk."""
    return fixed_table(
        ALERTS_HEADERS,
        alerts_rows(events, name_by_id, current_by_ca),
        ALERTS_ALIGNS,
        max_width=14,
    )
