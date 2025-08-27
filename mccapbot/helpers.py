import re, math, time
from typing import Optional, List, Dict
from .constants import LAMPORTS
from .config import SOLANA_USE_FDV
import discord

def is_solana_address(addr: str) -> bool:
    if not addr or addr.startswith("0x"): return False
    return 32 <= len(addr) <= 44 and re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]+", addr) is not None

def short_ca(ca: str) -> str: return f"{ca[:4]}…{ca[-4:]}"
def to_lamports(sol: float) -> int: return int(round(sol * LAMPORTS))

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
    v=v.lower().replace(",","").strip(); m=1
    if v.endswith("k"): m,v=1_000, v[:-1]
    elif v.endswith("m"): m,v=1_000_000, v[:-1]
    elif v.endswith("b"): m,v=1_000_000_000, v[:-1]
    elif v.endswith("t"): m,v=1_000_000_000_000, v[:-1]
    return float(v)*m

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
