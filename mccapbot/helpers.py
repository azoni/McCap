import math
import re
import time
from typing import List, Optional, Tuple

import discord


def is_solana_address(addr: str) -> bool:
    if not addr or addr.startswith("0x"): return False
    return 32 <= len(addr) <= 44 and re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]+", addr) is not None


def short_ca(ca: str) -> str: return f"{ca[:4]}…{ca[-4:]}"


def money(x: float) -> str:
    n=float(x)
    for u in ["","K","M","B","T"]:
        if abs(n) < 1000: return f"{n:,.2f}{u}"
        n/=1000
    return f"{n:,.2f}P"


def humanize(x: Optional[float]) -> str:
    if x is None: return "—"
    return money(float(x))


def parse_mc_input(v: str) -> float:
    """Parse an absolute market cap: 2500000, 250k, 2.5m, 1b, 1t."""
    v=v.lower().replace(",","").replace("$","").strip(); m=1
    if v.endswith("k"): m,v=1_000, v[:-1]
    elif v.endswith("m"): m,v=1_000_000, v[:-1]
    elif v.endswith("b"): m,v=1_000_000_000, v[:-1]
    elif v.endswith("t"): m,v=1_000_000_000_000, v[:-1]
    return float(v)*m


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
    user = client.get_user(user_id)
    if user is None:
        try: user = await client.fetch_user(user_id)
        except Exception: user = None
    return user.name if user else f"user:{user_id}"


def when_str(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts))
