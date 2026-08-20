from typing import List, Dict, Optional
from .helpers import humanize, when_str

# Payments tables (kept identical to your behavior)
def payments_table(items) -> str:
    headers=["ID","Asset","Amount","Status","When"]; widths=[8,6,14,8,16]; aligns=["l","l","r","l","l"]
    def c(v,w,a):
        s=str(v); s=(s if len(s)<=w else s[:max(1,w-1)]+"…")
        return s.rjust(w) if a=="r" else (s.center(w) if a=="c" else s.ljust(w))
    rows=[]
    for inv in items:
        amt=inv.amount_base/(10**inv.decimals); when=when_str(inv.created_ts)
        rows.append([inv.id, inv.asset, f"{amt:,.4f} {inv.asset}", inv.status.upper(), when])
    head="  ".join(c(h,w,'l') for h,w in zip(headers,widths))
    sep="  ".join("─"*w for w in widths)
    body="\n".join("  ".join(c(v,w,a) for v,w,a in zip(r,widths,aligns)) for r in rows) or "—"
    return f"```\n{head}\n{sep}\n{body}\n```"

def payments_table_with_users(items, name_by_id: Dict[int,str]) -> str:
    headers=["ID","Ast","Amount","By","Status","When"]; widths=[6,4,7,5,7,11]; aligns=["l","l","r","l","l","l"]
    def cut(s,w): s=(str(s).replace("\n"," ").strip()); return s if len(s)<=w else s[:max(1,w-1)]+"…"
    def c(v,w,a): s=cut(v,w); return s.rjust(w) if a=="r" else (s.center(w) if a=="c" else s.ljust(w))
    rows=[]
    for inv in items:
        amt=inv.amount_base/(10**inv.decimals); when=when_str(inv.created_ts)
        payer=cut(name_by_id.get(inv.user_id, f"user:{inv.user_id}"), widths[3])
        rows.append([cut(inv.id,widths[0]), cut(inv.asset,widths[1]), f"{amt:,.4f}", payer, cut(inv.status.upper(),widths[4]), cut(when,widths[5])])
    head="  ".join(c(h,w,'l') for h,w in zip(headers,widths))
    sep="  ".join("─"*w for w in widths)
    body="\n".join("  ".join(c(v,w,a) for v,w,a in zip(r,widths,aligns)) for r in rows) or "—"
    return f"```\n{head}\n{sep}\n{body}\n```"

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

# Recent alerts table (one line, At = current cache MC)
def alerts_table(events, name_by_id: Dict[int, str], current_by_ca: Dict[str, Optional[float]]) -> str:
    headers=["When","Token","Dir","Target","Current","By"]
    widths =[11,    6,     2,    7,       7,   6]
    aligns =["l",   "l",   "c",  "r",     "r", "l"]

    def cut(s,w):
        s=str(s).replace("\n"," ").strip()
        return s if len(s)<=w else s[:max(1,w-1)]+"…"

    def cell(v,w,a):
        s=cut(v,w)
        return s.rjust(w) if a=="r" else (s.center(w) if a=="c" else s.ljust(w))

    rows=[]
    for e in events:
        dir_sym="≥" if e.direction=="above" else "≤"
        rows.append([
            when_str(e.ts),
            cut(e.symbol or e.name, widths[1]),
            dir_sym,
            f"${humanize(e.target_mc)}",
            f"${humanize(current_by_ca.get(e.ca))}",  # current MC from cache
            cut(name_by_id.get(e.creator_id, f"user:{e.creator_id}"), widths[5]),
        ])

    head="  ".join(cell(h,w,'l') for h,w in zip(headers,widths))
    sep="  ".join("─"*w for w in widths)
    body="\n".join("  ".join(cell(v,w,a) for v,w,a in zip(r,widths,aligns)) for r in rows) or "—"
    return f"```\n{head}\n{sep}\n{body}\n```"
