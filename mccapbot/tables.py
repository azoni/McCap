from typing import Dict, List, Optional

from .helpers import humanize, when_str


def fixed_table(
    headers: List[str],
    rows: List[List[str]],
    aligns: Optional[List[str]] = None,
    max_width: int = 18,
) -> str:
    """Render a monospace table, sizing each column to its widest cell.

    The old version hard-coded five column widths, so any table with a different
    shape silently truncated or misaligned. Columns now auto-size (capped at
    ``max_width``) and the alignment list defaults to left for text.
    """
    ncols = len(headers)
    aligns = (aligns or ["l"] * ncols)[:ncols]
    aligns += ["l"] * (ncols - len(aligns))

    widths = []
    for i in range(ncols):
        longest = max([len(str(headers[i]))] + [len(str(r[i])) for r in rows if i < len(r)] or [0])
        widths.append(min(max(1, longest), max_width))

    def c(v, w, a):
        s = str(v).replace("\n", " ")
        if len(s) > w:
            s = s[: max(1, w - 1)] + "…"
        return s.rjust(w) if a == "r" else (s.center(w) if a == "c" else s.ljust(w))

    head = "  ".join(c(h, w, "l") for h, w in zip(headers, widths))
    sep = "  ".join("─" * w for w in widths)
    body = "\n".join("  ".join(c(v, w, a) for v, w, a in zip(r, widths, aligns)) for r in rows) or "—"
    return f"```\n{head}\n{sep}\n{body}\n```"


def alerts_table(events, name_by_id: Dict[int, str], current_by_ca: Dict[str, Optional[float]]) -> str:
    """Recent fired alerts. Level alerts show a target; move alerts show a %."""
    headers = ["When", "Token", "Dir", "Trigger", "Current", "By"]
    aligns = ["l", "l", "c", "r", "r", "l"]
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
    return fixed_table(headers, rows, aligns, max_width=14)
