import math
import re
from typing import List, Optional, Tuple

import discord


def is_solana_address(addr: str) -> bool:
    if not addr or addr.startswith("0x"): return False
    return 32 <= len(addr) <= 44 and re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]+", addr) is not None


def short_ca(ca: str) -> str: return f"{ca[:4]}…{ca[-4:]}"


def is_evm_address(s: Optional[str]) -> bool:
    """0x plus 40 hex characters; the shape a Robinhood Chain token address has."""
    return re.fullmatch(r"0x[0-9a-fA-F]{40}", s or "") is not None


# ---------------- one way to write each kind of number ----------------
#
# Every command formats through these, so a market cap, a dollar figure or a
# percentage looks the same in an alert, a table cell and a trade receipt.

UNKNOWN = "—"                 # the only rendering of "no data"
SEP = " · "                   # the only separator, footers included
NEUTRAL, GOOD, BAD = 0x2B90D9, 0x57F287, 0xED4245


def colour_for(v: Optional[float]) -> int:
    """Embed colour from a signed figure: green at or above zero, red below, blue when unknown."""
    if v is None:
        return NEUTRAL
    return GOOD if v >= 0 else BAD


def compact(n: float) -> str:
    """4.79, 24.80, 1.5K, 222K, 1.23M, 1.2M, 500M: two decimals under a thousand,
    three significant figures above it, trailing zeros dropped."""
    n = float(n)
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n < 999.5:
        return f"{sign}{n:,.2f}"
    units = ["", "K", "M", "B", "T", "Q"]
    k = 0
    while n >= 999.5 and k < len(units) - 1:
        n /= 1000
        k += 1
    if n >= 99.95:
        s = f"{n:.0f}"
    elif n >= 9.995:
        s = f"{n:.1f}"
    else:
        s = f"{n:.2f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return f"{sign}{s}{units[k]}"


def humanize(x: Optional[float]) -> str:
    """A bare compact number, or the unknown mark."""
    if x is None:
        return UNKNOWN
    return compact(float(x))


def usd(v: Optional[float], signed: bool = False) -> str:
    """$4.79, $221K, $1.2M; signed gives +$4.95 / -$3.20."""
    if v is None:
        return UNKNOWN
    v = float(v)
    if signed:
        return f"{'+' if v >= 0 else '-'}${compact(abs(v))}"
    return f"-${compact(abs(v))}" if v < 0 else f"${compact(v)}"


def pct(v: Optional[float], signed: bool = True) -> str:
    """+37.5%, -12.3%, +4%; signed=False gives 30%."""
    if v is None:
        return UNKNOWN
    v = float(v)
    if abs(v) < 0.05:
        v = 0.0
    s = f"{v:+.1f}" if signed else f"{v:.1f}"
    if s.endswith(".0"):
        s = s[:-2]
    return s + "%"


def mult(m: Optional[float]) -> str:
    """0.96x, 1.26x, 4x, 12.3x, 150x."""
    if m is None:
        return UNKNOWN
    m = float(m)
    if m >= 99.95:
        s = f"{m:,.0f}"
    elif m >= 9.995:
        s = f"{m:.1f}"
    else:
        s = f"{m:.2f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s + "x"


def qty(v: Optional[float]) -> str:
    """Token amounts: 2,062 / 36 / 3.33 / 0.0004."""
    if v is None:
        return UNKNOWN
    v = float(v)
    if v >= 1000:
        return f"{v:,.0f}"
    s = f"{v:,.2f}" if v >= 1 else f"{v:.4f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def eth_str(eth: Optional[float]) -> str:
    """An ETH amount from a float: 1 / 0.05 / 0.0181 (six places at most)."""
    if eth is None:
        return UNKNOWN
    s = f"{float(eth):,.6f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def when(ts: float) -> str:
    """Discord renders this as a relative time in the reader's own zone."""
    return f"<t:{int(ts)}:R>"


def plural(n: int, word: str, plural_word: Optional[str] = None) -> str:
    """1 alert / 3 alerts."""
    return f"{n:,} {word if n == 1 else (plural_word or word + 's')}"


def footer(*parts) -> str:
    """Join the non-empty pieces of a footer or a status line."""
    return SEP.join(str(p) for p in parts if p)


def chunk_lines(lines: List[str], limit: int) -> List[str]:
    """Split lines into blocks that each fit ``limit`` characters (an embed field is 1024)."""
    out: List[str] = []
    cur: List[str] = []
    used = 0
    for line in lines:
        line = line if len(line) <= limit else line[: limit - 1] + "…"
        if cur and used + len(line) + 1 > limit:
            out.append("\n".join(cur))
            cur, used = [], 0
        cur.append(line)
        used += len(line) + (1 if used else 0)
    if cur:
        out.append("\n".join(cur))
    return out


def fit_lines(lines: List[str], limit: int = 2000) -> str:
    """Join lines into one message Discord will accept, saying how many were cut.

    Removing 40 alerts once produced a 2015-character reply, which Discord
    rejects; since the interaction was already deferred, the command just hung.
    """
    out: List[str] = []
    used = 0
    for i, line in enumerate(lines):
        tail = f"…and {plural(len(lines) - i, 'more line')}"
        if used + len(line) + 1 + len(tail) > limit:
            out.append(tail)
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)[:limit]


def parse_mc_input(v: str) -> float:
    """Parse an absolute market cap: 2500000, 250k, 2.5m, 1b, 1t."""
    v=v.lower().replace(",","").replace("$","").strip(); m=1
    if v.endswith("k"): m,v=1_000, v[:-1]
    elif v.endswith("m"): m,v=1_000_000, v[:-1]
    elif v.endswith("b"): m,v=1_000_000_000, v[:-1]
    elif v.endswith("t"): m,v=1_000_000_000_000, v[:-1]
    out = float(v) * m
    # nan/inf pass the >0 guard at the call site but can never satisfy
    # meets(), producing an alert that silently never fires. json.dump
    # also refuses to serialise them.
    if not math.isfinite(out):
        raise ValueError("target must be a finite number")
    return out


class RelativeTargetError(ValueError):
    """A relative target was given but there is no current MC to anchor to."""



def parse_target(raw: str, current_mc: Optional[float]) -> Tuple[float, str]:
    """Resolve a target that may be absolute or relative to the current MC.

    Returns ``(absolute_target, spec)`` where ``spec`` is the shorthand the user
    typed for relative targets ("2x", "+50%") and empty for absolute ones.

    During a run you rarely know the market cap you want in advance — you know
    you want out at 2x. Relative targets are resolved once, at creation, so the
    alert still has a fixed number behind it.
    """
    s = (raw or "").strip().lower().replace(",", "").replace(" ", "")
    if not s:
        raise ValueError("empty target")

    # 2x / 0.5x / x2
    m = re.fullmatch(r"(?:x(\d*\.?\d+)|(\d*\.?\d+)x)", s)
    if m:
        mult = float(m.group(1) or m.group(2))
        if mult <= 0:
            raise ValueError("multiplier must be positive")
        if not current_mc or current_mc <= 0:
            raise RelativeTargetError("no current market cap to multiply")
        return current_mc * mult, f"{mult:g}x"

    # +50% / -30% / 50%
    m = re.fullmatch(r"([+-]?)(\d*\.?\d+)%", s)
    if m:
        sign, num = m.group(1), float(m.group(2))
        if not current_mc or current_mc <= 0:
            raise RelativeTargetError("no current market cap to apply a percentage to")
        if sign == "-":
            if num >= 100:
                raise ValueError("cannot drop 100% or more")
            return current_mc * (1 - num / 100), f"-{num:g}%"
        return current_mc * (1 + num / 100), f"+{num:g}%"

    return parse_mc_input(s), ""


def parse_window(raw: str) -> int:
    """Parse a duration like 15m, 1h, 4h, 1d into seconds."""
    s = (raw or "").strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d*\.?\d+)([smhd])", s)
    if not m:
        raise ValueError("use a duration like 15m, 1h, 4h or 1d")
    n, unit = float(m.group(1)), m.group(2)
    secs = int(n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit])
    if secs < 60:
        raise ValueError("window must be at least 1 minute")
    if secs > 7 * 86400:
        raise ValueError("window must be at most 7 days")
    return secs


def human_window(seconds: int) -> str:
    if seconds % 86400 == 0: return f"{seconds // 86400}d"
    if seconds % 3600 == 0: return f"{seconds // 3600}h"
    if seconds % 60 == 0: return f"{seconds // 60}m"
    return f"{seconds}s"


def age(seconds: float) -> str:
    """Elapsed time the way a board shows it: 4m, 2h, 3d (never negative)."""
    secs = max(0.0, float(seconds))
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


def meets(dir_: str, current: Optional[float], target: float) -> bool:
    if current is None: return False
    return (current >= target) if dir_=="above" else (current <= target)


def _percentile(sorted_vals: List[float], p: float) -> float:
    k=(len(sorted_vals)-1)*p; f=math.floor(k); c=math.ceil(k)
    if f==c: return sorted_vals[int(k)]
    return sorted_vals[f] + (sorted_vals[c]-sorted_vals[f])*(k-f)


def _median(vals: List[float]) -> float:
    s=sorted(vals); n=len(s)
    if n==0: return 0.0
    return s[n//2] if n%2 else 0.5*(s[n//2-1] + s[n//2])


async def username_from_id(client: discord.Client, user_id: int) -> str:
    # 0 means "no human owner" — an alert armed automatically from a detected
    # scan. Looking that up would fail and render the literal "user:0".
    if not user_id:
        return "auto"
    user = client.get_user(user_id)
    if user is None:
        try: user = await client.fetch_user(user_id)
        except Exception: user = None
    return user.name if user else f"user:{user_id}"
