import math
from typing import Dict, List, Optional, Tuple

from .config import DEX_BLACKLIST, DEX_TOKEN_URL, SOLANA_USE_FDV
from .constants import DEX_ALIASES, LP_VENUES, MAJOR_QUOTES
from .helpers import _median, _percentile, humanize, is_solana_address
from .http import dex_limiter, get_json


async def fetch_dex_token(address: str):
    """Fetch every pair DexScreener knows about for one token.

    Deliberately one address per call: the multi-address form of this endpoint
    caps the response at 30 *pairs total* across the whole batch, so a busy
    token starves the others and they come back with no market cap at all.
    Rate limiting is handled centrally by ``dex_limiter``.
    """
    url = DEX_TOKEN_URL.format(address=address.strip())
    return await get_json(url, limiter=dex_limiter)

def resolve_mc_value(pair: Dict, ca: str) -> Tuple[Optional[float], str]:
    is_sol = (pair.get("chainId") == "solana") or is_solana_address(ca)
    first, second = ("fdv","marketCap") if (SOLANA_USE_FDV and is_sol) else ("marketCap","fdv")
    for key in (first, second):
        val = pair.get(key)
        if val is not None:
            try:
                f = float(val)
                if f > 0: return f, key
            except: pass
    try:
        price = float(pair.get("priceUsd") or 0)
        supply = float((pair.get("baseToken") or {}).get("circulatingSupply") or 0)
        if price>0 and supply>0: return price*supply, "computed"
    except: pass
    return None, "none"

def _not_blacklisted(p: Dict) -> bool:
    return ((p.get("dexId") or "").lower() not in DEX_BLACKLIST)

def get_image_url(pair: Dict, ca: str) -> Optional[str]:
    img = ((pair.get("info") or {}).get("imageUrl") or (pair.get("baseToken") or {}).get("logoURI"))
    if img and isinstance(img, str) and img.startswith("http"): return img
    return f"https://robohash.org/{ca}.png?size=200x200&set=set1"

def _liq_vol_tx(p: Dict) -> Tuple[float,float,int]:
    liq=float((p.get("liquidity") or {}).get("usd") or 0.0)
    vol=float((p.get("volume") or {}).get("h24") or 0.0)
    tx=(p.get("txns") or {}).get("h24") or {}
    txc=int(tx.get("buys") or 0)+int(tx.get("sells") or 0)
    return liq,vol,txc

def _lp_score(liq: float, vol: float, tx: int) -> float:
    return (math.log10(1+liq)*0.5) + (math.log10(1+vol)*0.4) + (math.log10(1+tx)*0.2)

def _dex_alias(p: Dict) -> str:
    dex_id = (p.get("dexId") or "").lower()
    return DEX_ALIASES.get(dex_id, dex_id)

def _own_pairs(pairs: List[Dict], ca: str) -> List[Dict]:
    """Pools where this token is the base asset, excluding blacklisted DEXes.

    DexScreener also returns pools where the address appears as the *quote*
    token; counting those would mix in a different token's metrics.
    """
    if is_solana_address(ca):
        out = [
            p for p in pairs
            if p.get("chainId") == "solana" and ((p.get("baseToken") or {}).get("address") == ca)
        ]
    else:
        cal = ca.lower()
        out = [p for p in pairs if ((p.get("baseToken") or {}).get("address", "").lower() == cal)]
    return [p for p in out if _not_blacklisted(p)]


def choose_consensus_pair(pairs: List[Dict], ca: str):
    if not pairs: return None, 0.0, []
    filtered = _own_pairs(pairs, ca)
    if not filtered: return None, 0.0, []

    cands=[]; valid=[]
    for p in filtered:
        mc,src=resolve_mc_value(p, ca)
        liq,vol,_tx=_liq_vol_tx(p)
        if mc and mc>0:
            valid.append(mc)
            cands.append((p,mc,liq,vol,src))
        else:
            cands.append((p,float("nan"),liq,vol,"none"))

    if not valid: return None, 0.0, cands

    logs=sorted([math.log10(v) for v in valid if v>0])
    if len(logs)>=3:
        q1=_percentile(logs,0.25); q3=_percentile(logs,0.75); iqr=q3-q1
        lo=q1-1.5*iqr; hi=q3+1.5*iqr
        kept=[10**x for x in logs if lo<=x<=hi]
        base_vals=kept if len(kept)>=2 else valid
    else:
        base_vals=valid
    consensus=_median(base_vals)

    best=None; best_key=None
    for p,mc,liq,vol,_src in cands:
        if mc is None or math.isnan(mc): continue
        rel=abs(mc-consensus)/consensus if consensus>0 else abs(mc-consensus)
        key=(rel,-liq,-vol)
        if best is None or key<best_key: best,best_key=p,key
    return best, consensus, cands

def _consensus_change_24h(own_pairs: List[Dict], total_liq: float) -> float:
    """Median 24h price change across pools quoted in a major asset.

    Liquidity alone is not a safe filter here. BONK's single deepest pool is
    quoted in an obscure token and reports +542,339%, while every SOL/USDC pool
    agrees on ~11.6%. A pair priced against a junk quote produces a junk price
    change regardless of how much sits in it, so restrict to recognised quote
    assets first and take the median of those.
    """
    def changes_for(pairs: List[Dict]) -> List[float]:
        out = []
        for p in pairs:
            raw = (p.get("priceChange") or {}).get("h24")
            if raw is None:
                continue
            try:
                out.append(float(raw))
            except (TypeError, ValueError):
                continue
        return out

    major = [
        p for p in own_pairs
        if ((p.get("quoteToken") or {}).get("symbol") or "").upper() in MAJOR_QUOTES
    ]
    vals = changes_for(major) or changes_for(own_pairs)
    if not vals:
        return 0.0
    return _median(vals)


async def token_summary(ca: str) -> Optional[Dict]:
    """One-shot lookup for display: name, symbol, MC, 24h change, liquidity.

    Used by the watchlist, which is read-on-demand and drives no background
    polling — so a big watchlist costs nothing until someone looks at it.
    """
    data = await fetch_dex_token(ca)
    if not data or not data.get("pairs"):
        return None
    best, consensus, _ = choose_consensus_pair(data["pairs"], ca)
    if not best:
        return None
    base = best.get("baseToken") or {}
    mc, _src = resolve_mc_value(best, ca)

    # The consensus pair is picked for market-cap accuracy, which can land on a
    # thin pool — reporting its liquidity alone made BONK look like it had $8k.
    # Liquidity and volume are therefore totals across every pool for this
    # token, and the 24h change is resolved by consensus (see below).
    own = _own_pairs(data["pairs"], ca)
    liq = vol = 0.0
    for p in own:
        p_liq, p_vol, _tx = _liq_vol_tx(p)
        liq += p_liq
        vol += p_vol

    change24 = _consensus_change_24h(own, liq)

    return {
        "ca": ca,
        "name": base.get("name") or base.get("symbol") or "Token",
        "symbol": base.get("symbol") or "",
        "mc": mc,
        "liq": liq,
        "vol24": vol,
        "change24": change24,
        "pools": len(own),
        "url": build_token_url(ca, best),
        "image_url": get_image_url(best, ca),
        "consensus": consensus,
    }


def build_token_url(ca: str, pair: Optional[Dict]) -> str:
    if is_solana_address(ca): return f"https://gmgn.ai/sol/token/{ca}"
    if pair and pair.get("url"): return pair["url"]
    return "https://dexscreener.com"

def summarize_lp_venues(pairs: List[Dict], ca: str):
    flt = _own_pairs(pairs, ca)
    if not flt: return {}, None

    agg={}
    for p in flt:
        venue=_dex_alias(p)
        if venue not in LP_VENUES: continue
        liq,vol,txc=_liq_vol_tx(p)
        quote=((p.get("quoteToken") or {}).get("symbol") or "").upper()
        url=p.get("url") or build_token_url(ca,p)
        a=agg.setdefault(venue, dict(pools=0, liq=0.0, vol=0.0, tx=0, quotes={}, best_url=url, best_liq=0.0))
        a["pools"]+=1; a["liq"]+=liq; a["vol"]+=vol; a["tx"]+=txc
        if quote: a["quotes"][quote]=a["quotes"].get(quote,0)+1
        if liq>a["best_liq"]: a["best_liq"]=liq; a["best_url"]=url
    for v,a in agg.items(): a["score"]=_lp_score(a["liq"],a["vol"],a["tx"])
    ranked=sorted(agg.items(), key=lambda kv:(-(kv[1]["score"]), -(kv[1]["liq"]), -(kv[1]["vol"])))
    best=ranked[0] if ranked else None
    return agg, best

def table_lp(agg: Dict[str, Dict]) -> str:
    headers=["Venue","Pools","Liq","Vol24","Tx24","Quotes","Score"]; widths=[9,5,12,12,8,10,6]; aligns=["l","r","r","r","r","l","r"]
    def cut(s,w): s=str(s); return s if len(s)<=w else s[:max(1,w-1)]+"…"
    def c(v,w,a): s=cut(v,w); return s.rjust(w) if a=="r" else (s.center(w) if a=="c" else s.ljust(w))
    rows=[]
    from .constants import LP_VENUES as ORDER
    for v in ORDER:
        a=agg.get(v)
        if a:
            rows.append([v.capitalize(), str(a["pools"]), f"${humanize(a['liq'])}", f"${humanize(a['vol'])}", f"{a['tx']}", ",".join(sorted(a["quotes"].keys())[:2]) or "—", f"{a['score']:.2f}"])
        else:
            rows.append([v.capitalize(), "0", "$—", "$—", "0", "—", "0.00"])
    head="  ".join(c(h,w,'l') for h,w in zip(headers,widths))
    sep="  ".join("─"*w for w in widths)
    body="\n".join("  ".join(c(v,w,a) for v,w,a in zip(r,widths,aligns)) for r in rows) or "—"
    return f"```\n{head}\n{sep}\n{body}\n```"
