import os
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple, Set

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
import pytz
import asyncio
import json
import random

# Optional GraphQL streaming deps (for real-time graduations)
try:
    from gql import Client, gql
    from gql.transport.websockets import WebsocketsTransport
    _GQL_AVAILABLE = True
except Exception:
    _GQL_AVAILABLE = False

from contextlib import asynccontextmanager

from ..http import get_session
from ..logging_setup import log

load_dotenv()


@asynccontextmanager
async def _shared_session():
    """Hand out the process-wide aiohttp session without closing it on exit.

    Drop-in for `async with _shared_session() as session:` so these call
    sites stop paying a TCP+TLS handshake per request.
    """
    yield await get_session()

# ============================ ENV / CONFIG ============================

def _clean_env(v: Optional[str]) -> str:
    if not v:
        return ""
    return v.strip().strip('"').strip("'")

MORALIS_API_KEY = _clean_env(os.getenv("MORALIS_API_KEY"))

# Bitquery — value can be either a true API key or an OAuth/Bearer token.
BITQUERY_API_VALUE = _clean_env(os.getenv("BITQUERY_API_KEY"))
BITQUERY_ENDPOINT = _clean_env(os.getenv("BITQUERY_ENDPOINT")) or "https://graphql.bitquery.io"

# Debug controls
BITQUERY_DEBUG = os.getenv("BITQUERY_DEBUG", "1").strip() not in ("0", "false", "False", "")
BITQUERY_DEBUG_DUMP = os.getenv("BITQUERY_DEBUG_DUMP", "0").strip() in ("1", "true", "True")

CHANNEL_ID = int(os.getenv("GRADUATED_CHANNEL", "1406150656527564908"))

# Display threshold & concurrency
MIN_MC_DISPLAY = int(os.getenv("MIN_MC_DISPLAY", "100000"))
MAX_CONCURRENT_REQUESTS = int(os.getenv("MAX_CONCURRENT_REQUESTS", "10"))

# Pacific timezone
PACIFIC_TZ = pytz.timezone("America/Los_Angeles")

# Raydium (Mainnet) program addresses
RAYDIUM = {
    "launchlab": "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj",
    "cpmm": "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",
    "amm_v4": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    "stable_swap": "5quBtoiQqxF9Jv6KYKctB59NT3gtJD2Y65kdnB1Uev3h",
    "clmm": "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
}
WSOL_MINT = "So11111111111111111111111111111111111111112"

# Meteora / Bags / Heaven program constants
METEORA_DBC_PROGRAM = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"   # DBC program (migration source)
BAGS_UPDATE_AUTH    = "BAGSB9TpGrZxQbEsrEznv5jXXdwyP6AXerN8aVRiAmcv"   # Bags update authority
HEAVEN_PROGRAM      = "HEAVENoP2qxoeuF8Dj2oT1GHEnu49U5mJYkdeC8BAX2o"   # Heaven DEX program

# ----- Real-time stream controls -----
GRAD_STREAM_ENABLE = os.getenv("GRAD_STREAM_ENABLE", "0").strip() in ("1","true","True")
GRAD_STREAM_URL = _clean_env(os.getenv("BITQUERY_STREAM_URL")) or "wss://streaming.bitquery.io/eap"
GRAD_STREAM_PROGRAM_ID = _clean_env(os.getenv("LB_PROGRAM_ID")) or RAYDIUM["launchlab"]
GRAD_STREAM_POST = os.getenv("GRAD_STREAM_POST", "0").strip() in ("1","true","True")  # post live alerts? default off
GRAD_STREAM_RETENTION_HOURS = int(os.getenv("GRAD_STREAM_RETENTION_HOURS", "72"))      # in-memory buffer TTL

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _parse_token_timestamp(t: Dict[str, Any]) -> Optional[dt.datetime]:
    ts_raw = (
        t.get("graduatedAt")
        or t.get("blockTimestamp")
        or t.get("timestamp")
        or t.get("createdAt")
        or t.get("pairCreatedAt")
        or t.get("info", {}).get("listedAt")
        or t.get("created_at")
    )
    if not ts_raw:
        return None
    try:
        if isinstance(ts_raw, (int, float)):
            if ts_raw > 1e12:  # ms
                return dt.datetime.fromtimestamp(ts_raw / 1000, dt.timezone.utc)
            return dt.datetime.fromtimestamp(ts_raw, dt.timezone.utc)
        s = str(ts_raw)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return dt.datetime.fromisoformat(s).astimezone(dt.timezone.utc)
    except Exception as e:
        log.debug(f"Failed to parse timestamp {ts_raw}: {e}")
        return None

def human_money(x: float) -> str:
    try:
        n = float(x)
    except Exception:
        return "0.00"
    for suffix in ["", "K", "M", "B", "T"]:
        if abs(n) < 1000:
            return f"{n:,.2f}{suffix}"
        n /= 1000
    return f"{n:,.2f}P"

async def safe_send(channel: discord.TextChannel, content: str):
    """Split long messages into <=2000 chunks and send them sequentially."""
    limit = 2000
    for i in range(0, len(content), limit):
        await channel.send(content[i:i+limit])

def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")

# ---------------------------------------------------------------------
# In-memory stream stores
# ---------------------------------------------------------------------

# BONK
_bonk_stream_lock = asyncio.Lock()
_bonk_stream_events: Dict[str, Dict[str, Any]] = {}  # key = mint

async def _bonk_stream_store_event(mint: str, ts_iso: str, method: str, signature: str):
    """Store/refresh a BONK graduation event (idempotent on mint)."""
    if not mint or mint == WSOL_MINT:
        return
    ev = {
        "timestamp": ts_iso,              # ISO string with Z
        "address": mint,                  # keep shape consistent with rest of code
        "symbol": "—",
        "name": "—",
        "launchpad": "bonk",
        "source": "launchlab_stream",
        "method": method,
        "signature": signature,
    }
    async with _bonk_stream_lock:
        # keep earliest timestamp if we somehow see multiple
        if mint in _bonk_stream_events:
            old = _bonk_stream_events[mint]
            if _parse_token_timestamp({"timestamp": ts_iso}) < _parse_token_timestamp({"timestamp": old.get("timestamp")}):
                _bonk_stream_events[mint] = ev
        else:
            _bonk_stream_events[mint] = ev

async def _bonk_stream_prune():
    """Drop events older than retention window to cap memory."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=GRAD_STREAM_RETENTION_HOURS)
    async with _bonk_stream_lock:
        for m in list(_bonk_stream_events.keys()):
            ts = _parse_token_timestamp(_bonk_stream_events[m])
            if not ts or ts < cutoff:
                _bonk_stream_events.pop(m, None)

async def _bonk_stream_snapshot(start: dt.datetime, end: dt.datetime) -> List[Dict[str, Any]]:
    """Copy events that fall within [start, end)."""
    out: List[Dict[str, Any]] = []
    async with _bonk_stream_lock:
        for ev in _bonk_stream_events.values():
            ts = _parse_token_timestamp(ev)
            if ts and (start <= ts < end):
                out.append(dict(ev))  # shallow copy
    return out

# BAGS
_bags_stream_lock = asyncio.Lock()
_bags_stream_events: Dict[str, Dict[str, Any]] = {}

async def _bags_stream_store_event(mint: str, ts_iso: str, method: str, signature: str):
    if not mint or mint == WSOL_MINT:
        return
    ev = {
        "timestamp": ts_iso,
        "address": mint,
        "symbol": "—",
        "name": "—",
        "launchpad": "bags",
        "source": "dbc_migration",
        "method": method,
        "signature": signature,
    }
    async with _bags_stream_lock:
        if mint in _bags_stream_events:
            old = _bags_stream_events[mint]
            if _parse_token_timestamp({"timestamp": ts_iso}) < _parse_token_timestamp({"timestamp": old.get("timestamp")}):
                _bags_stream_events[mint] = ev
        else:
            _bags_stream_events[mint] = ev

async def _bags_stream_prune():
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=GRAD_STREAM_RETENTION_HOURS)
    async with _bags_stream_lock:
        for m in list(_bags_stream_events.keys()):
            ts = _parse_token_timestamp(_bags_stream_events[m])
            if not ts or ts < cutoff:
                _bags_stream_events.pop(m, None)

async def _bags_stream_snapshot(start: dt.datetime, end: dt.datetime) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    async with _bags_stream_lock:
        for ev in _bags_stream_events.values():
            ts = _parse_token_timestamp(ev)
            if ts and (start <= ts < end):
                out.append(dict(ev))
    return out

# HEAVEN
_heaven_stream_lock = asyncio.Lock()
_heaven_stream_events: Dict[str, Dict[str, Any]] = {}

async def _heaven_stream_store_event(mint: str, ts_iso: str, method: str, signature: str):
    if not mint or mint == WSOL_MINT:
        return
    ev = {
        "timestamp": ts_iso,
        "address": mint,
        "symbol": "—",
        "name": "—",
        "launchpad": "heaven",
        "source": "heaven_pool_created",
        "method": method,
        "signature": signature,
    }
    async with _heaven_stream_lock:
        if mint in _heaven_stream_events:
            old = _heaven_stream_events[mint]
            if _parse_token_timestamp({"timestamp": ts_iso}) < _parse_token_timestamp({"timestamp": old.get("timestamp")}):
                _heaven_stream_events[mint] = ev
        else:
            _heaven_stream_events[mint] = ev

async def _heaven_stream_prune():
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=GRAD_STREAM_RETENTION_HOURS)
    async with _heaven_stream_lock:
        for m in list(_heaven_stream_events.keys()):
            ts = _parse_token_timestamp(_heaven_stream_events[m])
            if not ts or ts < cutoff:
                _heaven_stream_events.pop(m, None)

async def _heaven_stream_snapshot(start: dt.datetime, end: dt.datetime) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    async with _heaven_stream_lock:
        for ev in _heaven_stream_events.values():
            ts = _parse_token_timestamp(ev)
            if ts and (start <= ts < end):
                out.append(dict(ev))
    return out

# ---------------------------------------------------------------------
# Bitquery POST (API key first, Bearer fallback) + Health Check
# ---------------------------------------------------------------------

def _is_probably_bearer(s: str) -> bool:
    # Common bearer/token shapes: ory_* or JWT (eyJ...)
    return bool(s) and (s.startswith("ory_") or s.startswith("oryat_") or s.startswith("ory_at_") or s.startswith("eyJ"))

def _headers_api_key(v: str) -> Dict[str, str]:
    return {"Content-Type": "application/json", "X-API-KEY": v}

def _headers_bearer(v: str) -> Dict[str, str]:
    return {"Content-Type": "application/json", "Authorization": f"Bearer {v}"}

async def _bitquery_post(session: aiohttp.ClientSession, payload: Dict[str, Any]) -> Tuple[int, Optional[Dict[str, Any]]]:
    """
    Try X-API-KEY first. If 401, retry as Bearer with the same value.
    Prints detailed debug including which mode was used and rate-limit headers.
    """
    if not BITQUERY_API_VALUE:
        log.debug("No BITQUERY_API_KEY value set.")
        return 401, None

    url = BITQUERY_ENDPOINT

    async def _once(headers: Dict[str, str], mode: str):
        try:
            if BITQUERY_DEBUG:
                log.debug(f"Bitquery: POST {url} (mode={mode})")
            async with session.post(url, json=payload, headers=headers, timeout=45) as r:
                txt = await r.text()
                if BITQUERY_DEBUG:
                    rl = {
                        "limit": r.headers.get("x-ratelimit-limit") or r.headers.get("X-RateLimit-Limit"),
                        "rem": r.headers.get("x-ratelimit-remaining") or r.headers.get("X-RateLimit-Remaining"),
                        "reset": r.headers.get("x-ratelimit-reset") or r.headers.get("X-RateLimit-Reset"),
                    }
                    log.debug(f"Bitquery resp status={r.status} ratelimit={rl}")
                if r.status != 200:
                    if BITQUERY_DEBUG:
                        log.debug(f"Bitquery body (first 300): {txt[:300]}")
                    return r.status, None
                try:
                    data = json.loads(txt)
                    return r.status, data
                except Exception as e:
                    log.warning(f"Bitquery JSON parse error: {e}")
                    return r.status, None
        except Exception as e:
            log.debug(f"Bitquery POST exception ({mode}): {type(e).__name__}: {e}")
            return 0, None

    # Prefer API key unless it looks like a bearer
    tried_api = False
    if not _is_probably_bearer(BITQUERY_API_VALUE):
        tried_api = True
        status, data = await _once(_headers_api_key(BITQUERY_API_VALUE), "x-api-key")
        if status == 200:
            if BITQUERY_DEBUG:
                log.debug("Bitquery success using X-API-KEY mode")
            return status, data
        if status == 401:
            log.debug("Bitquery 401 under X-API-KEY, retrying as Bearer")

    # Bearer fallback
    status, data = await _once(_headers_bearer(BITQUERY_API_VALUE), "bearer")
    if status == 200 and BITQUERY_DEBUG:
        log.debug("Bitquery success using Bearer mode")

    # Retry on rate-limit or transient
    if status in (429, 500, 502, 503, 504, 0):
        await asyncio.sleep(0.5 + random.random())
        mode = "bearer" if (not tried_api or _is_probably_bearer(BITQUERY_API_VALUE)) else "x-api-key"
        headers = _headers_bearer(BITQUERY_API_VALUE) if mode == "bearer" else _headers_api_key(BITQUERY_API_VALUE)
        if BITQUERY_DEBUG:
            log.debug(f"Bitquery retry after status {status} (mode={mode})")
        status, data = await _once(headers, mode)
    return status, data

async def _bitquery_healthcheck() -> None:
    """Verify connectivity and log which auth mode works."""
    pv = BITQUERY_API_VALUE
    log.debug(f"Bitquery endpoint: {BITQUERY_ENDPOINT}")
    log.debug(f"Bitquery API value present: {bool(pv)}   shape: {'bearer-like' if _is_probably_bearer(pv) else 'apikey-like'}")
    if not pv:
        return
    payload = {"query": "query HC { Solana { Blocks(limit: {count: 1}) { Time } } }", "variables": {}}
    try:
        async with _shared_session() as session:
            s, d = await _bitquery_post(session, payload)
            if s == 200:
                log.debug("Bitquery healthcheck OK ✅")
                if BITQUERY_DEBUG_DUMP and d:
                    with open("bitquery_healthcheck.json", "w") as f:
                        json.dump(d, f, indent=2)
            else:
                log.debug(f"Bitquery healthcheck failed with status {s}")
    except Exception as e:
        log.debug(f"Bitquery healthcheck exception: {type(e).__name__}: {e}")

# ---------------------------------------------------------------------
# Dexscreener metrics (for MC/Liq)
# ---------------------------------------------------------------------

async def _dexscreener_info(ca: str) -> Dict[str, Any]:
    url = f"https://api.dexscreener.com/latest/dex/tokens/{ca}"
    try:
        async with _shared_session() as session:
            async with session.get(url, timeout=12) as r:
                if r.status != 200:
                    log.debug(f"Dexscreener {ca} failed HTTP {r.status}")
                    return {}
                data = await r.json()

        pairs = data.get("pairs") or []
        if not pairs:
            log.debug(f"Dexscreener {ca} returned no pairs")
            return {}

        def score(p):
            base = (p.get("baseToken") or {}).get("address")
            liq = float((p.get("liquidity") or {}).get("usd") or 0.0)
            exact = 1 if base and base.lower() == ca.lower() else 0
            return (exact, liq)

        best = max(pairs, key=score)

        return {
            "mc": float(best.get("marketCap") or 0.0),
            "fdv": float(best.get("fdv") or 0.0),
            "liq": float((best.get("liquidity") or {}).get("usd") or 0.0),
            "symbol": (best.get("baseToken") or {}).get("symbol") or "—",
            "name": (best.get("baseToken") or {}).get("name") or "—",
        }
    except Exception as e:
        log.warning(f"Dexscreener error for {ca}: {e}")
        return {}

# ---------------------------------------------------------------------
# PumpFun (Moralis)
# ---------------------------------------------------------------------

MORALIS_GW = "https://solana-gateway.moralis.io"
MORALIS_DI = "https://deep-index.moralis.io"

async def _pumpfun_get_graduated(limit: int = 100) -> List[Dict[str, Any]]:
    path = "/token/mainnet/exchange/pumpfun/graduated"
    headers = {"X-API-Key": MORALIS_API_KEY}
    if not MORALIS_API_KEY:
        log.debug("No MORALIS_API_KEY set")
        return []

    async with _shared_session() as session:
        for base in (MORALIS_GW, MORALIS_DI):
            url = base + path
            try:
                async with session.get(url, headers=headers, params={"limit": str(limit)}, timeout=20) as r:
                    if r.status != 200:
                        log.debug(f"PumpFun Moralis {base} HTTP {r.status}")
                        continue
                    data = await r.json()
                if isinstance(data, list):
                    log.debug(f"PumpFun Moralis {base} returned list len={len(data)}")
                    return data
                if isinstance(data, dict):
                    arr = data.get("result") or data.get("results") or data.get("items") or data.get("data")
                    if isinstance(arr, list):
                        log.debug(f"PumpFun Moralis {base} returned dict list len={len(arr)}")
                        return arr
            except Exception as e:
                log.debug(f"PumpFun Moralis failed {base}{path} -> {e}")
                continue
    return []

# ---------------------------------------------------------------------
# Bonk (Raydium LaunchLab) via Bitquery — migration-first with deep debug
# ---------------------------------------------------------------------

def _extract_mints_from_instruction(instr: Dict[str, Any]) -> Set[str]:
    mints: Set[str] = set()
    accounts = ((instr.get("Instruction") or {}).get("Accounts")) or []
    for acc in accounts:
        tok = acc.get("Token") or {}
        mint = tok.get("Mint")
        if mint and mint != WSOL_MINT:
            mints.add(mint)
    return mints

async def _bonk_get_graduated_bitquery(start_time: dt.datetime, end_time: dt.datetime) -> List[Dict[str, Any]]:
    """
    Graduation = LaunchLab migration in the window:
      1) Primary: Instructions where Program = LaunchLab AND Method in ["migrate_to_amm", "migrate_to_cpswap"]
      2) Fallback: DEXTradeByTokens with Dex.ProtocolName == 'raydium_launchpad' OR Dex.ProgramAddress == LaunchLab
                   (helps when trades accompany graduation)
    """
    if not BITQUERY_API_VALUE:
        log.debug("No Bitquery value set in .env (BITQUERY_API_KEY).")
        return []

    start_iso = start_time.strftime("%Y-%m-%dT%H:%M:%S")
    end_iso = end_time.strftime("%Y-%m-%dT%H:%M:%S")

    if BITQUERY_DEBUG:
        log.debug("=== Bitquery LaunchLab Graduates ===")
        log.debug(f"Window UTC:   {start_iso} -> {end_iso}")
        log.debug(f"Window PT:    {start_time.astimezone(PACIFIC_TZ).isoformat()} -> {end_time.astimezone(PACIFIC_TZ).isoformat()}")

    graduates: Dict[str, Dict[str, Any]] = {}

    # --------- A) Migration instructions (authoritative for graduation) ---------
    instr_query = """
    query LaunchLabMigrations($from: DateTime, $till: DateTime, $prog: String!) {
      Solana {
        Instructions(
          where: {
            Instruction: { Program: { Address: { is: $prog }, Method: { in: ["migrate_to_amm","migrate_to_cpswap"] } } }
            Transaction: { Result: { Success: true } }
            Block: { Time: { after: $from, before: $till } }
          }
          orderBy: { ascending: Block_Time }
          limit: { count: 2000 }
        ) {
          Block { Time }
          Transaction { Signature }
          Instruction {
            Program { Method Address }
            Accounts {
              Address
              Token { Mint ProgramId Owner }
            }
          }
        }
      }
    }
    """
    variables = {"from": start_iso, "till": end_iso, "prog": RAYDIUM["launchlab"]}

    async with _shared_session() as session:
        if BITQUERY_DEBUG:
            log.debug("Query A: LaunchLab migrations (migrate_to_amm / migrate_to_cpswap)")
        sA, dA = await _bitquery_post(session, {"query": instr_query, "variables": variables})
        rowsA = (((dA or {}).get("data") or {}).get("Solana") or {}).get("Instructions") or []
        log.debug(f"Query A status={sA} rows={len(rowsA)} data_present={bool(dA)}")
        if BITQUERY_DEBUG_DUMP and dA:
            with open("bitquery_launchlab_migrations.json", "w") as f:
                json.dump(dA, f, indent=2)

        if rowsA:
            # show sample
            log.debug("Query A sample (up to 5):")
            for r in rowsA[:5]:
                method = ((r.get("Instruction") or {}).get("Program") or {}).get("Method")
                ts = (r.get("Block") or {}).get("Time")
                sig = (r.get("Transaction") or {}).get("Signature")
                log.debug(f"    - {ts} | {method} | tx={sig}")

            # extract mints from accounts
            for r in rowsA:
                ts = (r.get("Block") or {}).get("Time")
                for mint in _extract_mints_from_instruction(r):
                    if mint not in graduates:
                        graduates[mint] = {
                            "timestamp": ts,
                            "address": mint,
                            "symbol": "—",
                            "name": "—",
                            "launchpad": "bonk",
                            "source": "launchlab_migration",
                        }

        # --------- B) Fallback: broadened trades on launchpad/program address ---------
        if not graduates:
            if BITQUERY_DEBUG:
                log.debug("No migrations found in window — trying trades fallback (protocol OR program)")

            trades_query = """
            query LaunchpadTrades($from: DateTime, $till: DateTime, $prog: String!) {
              Solana {
                DEXTradeByTokens(
                  where: {
                    or: [
                      { Trade: { Dex: { ProtocolName: { is: "raydium_launchpad" } } } },
                      { Trade: { Dex: { ProgramAddress: { is: $prog } } } }
                    ]
                    Block: { Time: { after: $from, before: $till } }
                  }
                  orderBy: { ascending: Block_Time }
                  limit: { count: 2000 }
                ) {
                  Block { Time }
                  Trade {
                    Dex { ProtocolName ProgramAddress }
                    Currency { MintAddress Symbol Name }
                  }
                }
              }
            }
            """
            sB, dB = await _bitquery_post(session, {"query": trades_query, "variables": variables})
            rowsB = (((dB or {}).get("data") or {}).get("Solana") or {}).get("DEXTradeByTokens") or []
            log.debug(f"Query B status={sB} rows={len(rowsB)} data_present={bool(dB)}")
            if BITQUERY_DEBUG_DUMP and dB:
                with open("bitquery_launchpad_trades.json", "w") as f:
                    json.dump(dB, f, indent=2)

            if rowsB:
                log.debug("Query B sample (up to 5):")
                for r in rowsB[:5]:
                    cur = (r.get("Trade") or {}).get("Currency") or {}
                    dex = (r.get("Trade") or {}).get("Dex") or {}
                    log.debug(f"    - {r.get('Block',{}).get('Time')} | mint={cur.get('MintAddress')} sym={cur.get('Symbol')} proto={dex.get('ProtocolName')} prog={dex.get('ProgramAddress')}")
                for r in rowsB:
                    cur = (r.get("Trade") or {}).get("Currency") or {}
                    mint = cur.get("MintAddress")
                    ts = (r.get("Block") or {}).get("Time")
                    if mint and mint != WSOL_MINT and mint not in graduates:
                        graduates[mint] = {
                            "timestamp": ts,
                            "address": mint,
                            "symbol": cur.get("Symbol") or "—",
                            "name": cur.get("Name") or "—",
                            "launchpad": "bonk",
                            "source": "launchpad_trades",
                        }

    log.debug(f"Bonk (LaunchLab) unique tokens: {len(graduates)}")
    if graduates and BITQUERY_DEBUG:
        log.debug("Unique mint sample (up to 5):")
        for m, info in list(graduates.items())[:5]:
            log.debug(f"    - {info['timestamp']} | {m}")

    return list(graduates.values())

# ---------------------------------------------------------------------
# Bags: verify Bags mint via update authority
# ---------------------------------------------------------------------

async def _is_bags_mint(session: aiohttp.ClientSession, mint: str) -> bool:
    q = """
    query IsBags($mint: String!, $auth: String!) {
      Solana {
        Transfers(
          limit: {count: 1}
          where: {Transfer: {Currency: {MintAddress: {is: $mint}, UpdateAuthority: {is: $auth}}}}
        ) {
          Transfer { Currency { MintAddress UpdateAuthority } }
        }
      }
    }
    """
    s, d = await _bitquery_post(session, {"query": q, "variables": {"mint": mint, "auth": BAGS_UPDATE_AUTH}})
    arr = (((d or {}).get("data") or {}).get("Solana") or {}).get("Transfers") or []
    return bool(arr)

# ---------------------------------------------------------------------
# Subscriptions (WebSocket) — BONK / BAGS / HEAVEN
# ---------------------------------------------------------------------

_BONK_SUBSCRIPTION = """
subscription LaunchLabGraduations($program: String!) {
  Solana(network: solana) {
    Instructions(
      where: {
        Instruction: {
          Program: {
            Address: { is: $program }
            Method: { in: ["migrate_to_amm","migrate_to_cpswap"] }
          }
        }
        Transaction: { Result: { Success: true } }
      }
    ) {
      Block { Time }
      Transaction { Signature }
      Instruction {
        Program { Method }
        Accounts { Token { Mint } }
      }
    }
  }
}
"""

_BAGS_SUBSCRIPTION = """
subscription DBCMigrations($program: String!) {
  Solana(network: solana) {
    Instructions(
      where: {
        Instruction: { Program: { Address: { is: $program }, Method: { in: ["migrate_meteora_damm","migration_damm_v2"] } } }
        Transaction: { Result: { Success: true } }
      }
    ) {
      Block { Time }
      Transaction { Signature }
      Instruction {
        Program { Method Address }
        Accounts { Address Token { Mint ProgramId Owner } }
      }
    }
  }
}
"""

_HEAVEN_SUBSCRIPTION = """
subscription HeavenPoolCreates($program: String!) {
  Solana(network: solana) {
    Instructions(
      where: {
        Instruction: { Program: { Address: { is: $program }, Method: { is: "create_standard_liquidity_pool" } } }
        Transaction: { Result: { Success: true } }
      }
    ) {
      Block { Time }
      Transaction { Signature }
      Instruction {
        Program { Method Address }
        Accounts { Address Token { Mint ProgramId Owner } }
      }
    }
  }
}
"""

# -------------------- Workers --------------------

async def _bonk_stream_worker(discord_bot: Optional[discord.Client] = None):
    """Persistent WebSocket subscriber with backoff; stores graduations and optional live post."""
    if not GRAD_STREAM_ENABLE:
        log.debug("BONK stream disabled via GRAD_STREAM_ENABLE=0")
        return
    if not BITQUERY_API_VALUE:
        log.debug("BONK stream not started: missing BITQUERY_API_KEY")
        return
    if not _GQL_AVAILABLE:
        log.debug("BONK stream not started: `gql[websockets]` not installed (`pip install gql[websockets]`)")
        return

    backoff = 1
    while True:
        try:
            url = f"{GRAD_STREAM_URL}?token={BITQUERY_API_VALUE}"
            transport = WebsocketsTransport(
                url=url,
                subprotocols=["graphql-ws", "graphql-transport-ws"],
                ping_interval=30,   # send a ws ping every 30s
                pong_timeout=10,    # expect a pong within 10s
            )
            async with Client(transport=transport, fetch_schema_from_transport=False) as session:
                log.debug(f"BONK stream connected to {GRAD_STREAM_URL}")
                query = gql(_BONK_SUBSCRIPTION)
                variables = {"program": GRAD_STREAM_PROGRAM_ID}
                async for msg in session.subscribe(query, variable_values=variables):
                    rows = (((msg or {}).get("Solana") or {}).get("Instructions")) or []
                    if not rows:
                        continue
                    for r in rows:
                        ts_iso = (r.get("Block") or {}).get("Time") or _utc_now_iso()
                        sig = (r.get("Transaction") or {}).get("Signature") or ""
                        method = ((r.get("Instruction") or {}).get("Program") or {}).get("Method") or "migrate_to_amm"
                        # pull first mint
                        mint = None
                        for acc in ((r.get("Instruction") or {}).get("Accounts")) or []:
                            tok = acc.get("Token") or {}
                            if tok.get("Mint") and tok["Mint"] != WSOL_MINT:
                                mint = tok["Mint"]
                                break
                        if not mint:
                            continue

                        await _bonk_stream_store_event(mint, ts_iso, method, sig)
                        await _bonk_stream_prune()
                        ts_dt = _parse_token_timestamp({"timestamp": ts_iso})
                        pt_str = ts_dt.astimezone(PACIFIC_TZ).strftime("%b %d, %Y • %I:%M:%S %p %Z") if ts_dt else ts_iso
                        log.info(
                            "BONK → Raydium | method=%s | mint=%s | tx=%s | timePT=%s",
                            method, mint, sig, pt_str,
                        )
                        if GRAD_STREAM_POST and discord_bot is not None:
                            ch = discord_bot.get_channel(CHANNEL_ID)
                            if ch and hasattr(ch, "send"):
                                raydium_kind = "AMM v4" if method == "migrate_to_amm" else "CPMM"
                                sig_url = f"https://solscan.io/tx/{sig}"
                                mint_url = f"https://solscan.io/token/{mint}"
                                ts_dt = _parse_token_timestamp({"timestamp": ts_iso})
                                when = ts_dt.astimezone(PACIFIC_TZ).strftime("%b %d, %Y • %I:%M:%S %p %Z") if ts_dt else ts_iso
                                msg_out = (
                                    "🎓 **Graduated (BONK → Raydium)**\n"
                                    f"• **Time (PT):** {when}\n"
                                    f"• **Method:** `{method}` ({raydium_kind})\n"
                                    f"• **Mint:** [`{mint}`]({mint_url})\n"
                                    f"• **Tx:** [{sig}]({sig_url})"
                                )
                                try:
                                    await ch.send(msg_out)
                                except Exception as e:
                                    log.warning(f"BONK stream post error: {e}")

            backoff = 1  # reset on clean exit
        except asyncio.CancelledError:
            log.debug("BONK stream task cancelled")
            return
        except Exception as e:
            log.warning(f"BONK stream error: {type(e).__name__}: {e}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

async def _bags_stream_worker(discord_bot: Optional[discord.Client] = None):
    """Stream DBC migrations; keep only those that are BAGS (by update authority)."""
    if not GRAD_STREAM_ENABLE:
        log.debug("BAGS stream disabled via GRAD_STREAM_ENABLE=0")
        return
    if not BITQUERY_API_VALUE:
        log.debug("BAGS stream not started: missing BITQUERY_API_KEY")
        return
    if not _GQL_AVAILABLE:
        log.debug("BAGS stream not started: `gql[websockets]` not installed (`pip install gql[websockets]`)")
        return

    backoff = 1
    while True:
        try:
            url = f"{GRAD_STREAM_URL}?token={BITQUERY_API_VALUE}"
            transport = WebsocketsTransport(
                url=url,
                subprotocols=["graphql-ws", "graphql-transport-ws"],
                ping_interval=30,
                pong_timeout=10,
            )
            async with Client(transport=transport, fetch_schema_from_transport=False) as session:
                log.debug(f"BAGS stream connected to {GRAD_STREAM_URL}")
                query = gql(_BAGS_SUBSCRIPTION)
                variables = {"program": METEORA_DBC_PROGRAM}
                async for msg in session.subscribe(query, variable_values=variables):
                    rows = (((msg or {}).get("Solana") or {}).get("Instructions")) or []
                    if not rows:
                        continue
                    async with _shared_session() as https:
                        for r in rows:
                            ts_iso = (r.get("Block") or {}).get("Time") or _utc_now_iso()
                            sig = (r.get("Transaction") or {}).get("Signature") or ""
                            method = ((r.get("Instruction") or {}).get("Program") or {}).get("Method") or ""
                            mints = _extract_mints_from_instruction(r)
                            for mint in mints:
                                if mint == WSOL_MINT:
                                    continue
                                try:
                                    is_bags = await _is_bags_mint(https, mint)
                                except Exception as e:
                                    log.debug(f"Bags authority check failed for {mint}: {e}")
                                    is_bags = False
                                if not is_bags:
                                    continue

                                await _bags_stream_store_event(mint, ts_iso, method, sig)
                                await _bags_stream_prune()
                                ts_dt = _parse_token_timestamp({"timestamp": ts_iso})
                                pt_str = ts_dt.astimezone(PACIFIC_TZ).strftime("%b %d, %Y • %I:%M:%S %p %Z") if ts_dt else ts_iso
                                log.info(
                                    "BAGS → DAMM | method=%s | mint=%s | tx=%s | timePT=%s",
                                    method, mint, sig, pt_str,
                                )

                                if GRAD_STREAM_POST and discord_bot is not None:
                                    ch = discord_bot.get_channel(CHANNEL_ID)
                                    if ch and hasattr(ch, "send"):
                                        msg_out = (
                                            "👜 **Graduated (Bags / DBC → DAMM)**\n"
                                            f"• **Time (PT):** {pt_str}\n"
                                            f"• **Method:** `{method}`\n"
                                            f"• **Mint:** [`{mint}`](https://solscan.io/token/{mint})\n"
                                            f"• **Tx:** [{sig}](https://solscan.io/tx/{sig})"
                                        )
                                        try:
                                            await ch.send(msg_out)
                                        except Exception as e:
                                            log.warning(f"BAGS stream post error: {e}")

            backoff = 1
        except asyncio.CancelledError:
            log.debug("BAGS stream task cancelled")
            return
        except Exception as e:
            log.warning(f"BAGS stream error: {type(e).__name__}: {e}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

async def _heaven_stream_worker(discord_bot: Optional[discord.Client] = None):
    """Stream Heaven pool creates; treat as 'go-live' graduations."""
    if not GRAD_STREAM_ENABLE:
        log.debug("HEAVEN stream disabled via GRAD_STREAM_ENABLE=0")
        return
    if not BITQUERY_API_VALUE:
        log.debug("HEAVEN stream not started: missing BITQUERY_API_KEY")
        return
    if not _GQL_AVAILABLE:
        log.debug("HEAVEN stream not started: `gql[websockets]` not installed (`pip install gql[websockets]`)")
        return

    backoff = 1
    while True:
        try:
            url = f"{GRAD_STREAM_URL}?token={BITQUERY_API_VALUE}"
            transport = WebsocketsTransport(
                url=url,
                subprotocols=["graphql-ws", "graphql-transport-ws"],
                ping_interval=30,
                pong_timeout=10,
            )
            async with Client(transport=transport, fetch_schema_from_transport=False) as session:
                log.debug(f"HEAVEN stream connected to {GRAD_STREAM_URL}")
                query = gql(_HEAVEN_SUBSCRIPTION)
                variables = {"program": HEAVEN_PROGRAM}
                async for msg in session.subscribe(query, variable_values=variables):
                    rows = (((msg or {}).get("Solana") or {}).get("Instructions")) or []
                    if not rows:
                        continue
                    for r in rows:
                        ts_iso = (r.get("Block") or {}).get("Time") or _utc_now_iso()
                        sig = (r.get("Transaction") or {}).get("Signature") or ""
                        method = ((r.get("Instruction") or {}).get("Program") or {}).get("Method") or ""
                        mints = [m for m in _extract_mints_from_instruction(r) if m != WSOL_MINT]
                        for mint in mints:
                            await _heaven_stream_store_event(mint, ts_iso, method, sig)
                            await _heaven_stream_prune()
                            ts_dt = _parse_token_timestamp({"timestamp": ts_iso})
                            pt_str = ts_dt.astimezone(PACIFIC_TZ).strftime("%b %d, %Y • %I:%M:%S %p %Z") if ts_dt else ts_iso
                            log.info(
                                "HEAVEN → Pool Created | method=%s | mint=%s | tx=%s | timePT=%s",
                                method, mint, sig, pt_str,
                            )

                            if GRAD_STREAM_POST and discord_bot is not None:
                                ch = discord_bot.get_channel(CHANNEL_ID)
                                if ch and hasattr(ch, "send"):
                                    msg_out = (
                                        "👼 **Heaven Pool Created (Go-Live)**\n"
                                        f"• **Time (PT):** {pt_str}\n"
                                        f"• **Method:** `{method}`\n"
                                        f"• **Mint:** [`{mint}`](https://solscan.io/token/{mint})\n"
                                        f"• **Tx:** [{sig}](https://solscan.io/tx/{sig})"
                                    )
                                    try:
                                        await ch.send(msg_out)
                                    except Exception as e:
                                        log.warning(f"HEAVEN stream post error: {e}")

            backoff = 1
        except asyncio.CancelledError:
            log.debug("HEAVEN stream task cancelled")
            return
        except Exception as e:
            log.warning(f"HEAVEN stream error: {type(e).__name__}: {e}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

# ---------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------

async def _fetch_recent_graduates_all(start: dt.datetime, end: dt.datetime, limit: int = 100) -> Dict[str, List[Dict[str, Any]]]:
    results: Dict[str, List[Dict[str, Any]]] = {}

    log.debug("=== Fetching PumpFun tokens ===")
    results["pumpfun"] = await _pumpfun_get_graduated(limit)

    log.debug("=== Fetching Bonk tokens (Raydium LaunchLab via Bitquery) ===")
    query_bonk = await _bonk_get_graduated_bitquery(start, end)

    log.debug("=== Snapshot Bonk tokens (stream buffer) ===")
    snap_bonk = await _bonk_stream_snapshot(start, end)
    log.debug(f"BONK stream snapshot count in window: {len(snap_bonk)}")

    # Deduplicate: prefer 'launchlab_migration' (query) over 'launchlab_stream'
    dedup: Dict[str, Dict[str, Any]] = {}
    for row in (query_bonk + snap_bonk):
        mint = (row.get("address") or row.get("mint") or "").strip()
        if not mint:
            continue
        if mint not in dedup:
            dedup[mint] = row
        else:
            if dedup[mint].get("source") != "launchlab_migration" and row.get("source") == "launchlab_migration":
                dedup[mint] = row

    results["bonk"] = list(dedup.values())

    log.debug("=== Snapshot Bags tokens (stream buffer) ===")
    results["bags"] = await _bags_stream_snapshot(start, end)
    log.debug(f"BAGS stream snapshot count in window: {len(results['bags'])}")

    log.debug("=== Snapshot Heaven tokens (stream buffer) ===")
    results["heaven"] = await _heaven_stream_snapshot(start, end)
    log.debug(f"HEAVEN stream snapshot count in window: {len(results['heaven'])}")

    log.debug("=== Fetch Summary ===")
    for lp, tokens in results.items():
        log.debug(f"{lp}: {len(tokens)} tokens fetched")
    return results

# ---------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------

class GraduatedCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._healthcheck_done = False
        self._bonk_stream_task: Optional[asyncio.Task] = None
        self._bags_stream_task: Optional[asyncio.Task] = None
        self._heaven_stream_task: Optional[asyncio.Task] = None
        self.hourly_graduated_report.start()

    def cog_unload(self):
        self.hourly_graduated_report.cancel()
        for t in (self._bonk_stream_task, self._bags_stream_task, self._heaven_stream_task):
            if t and not t.done():
                t.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        """Start the real-time graduation streams, if enabled.

        These were previously commented out, which silently made the Bags and
        Heaven snapshots always return empty (they are fed *only* by the
        streams). They are now behind GRAD_STREAM_ENABLE, which defaults off so
        Bitquery quota is opt-in — set GRAD_STREAM_ENABLE=1 to turn them on.
        """
        if not GRAD_STREAM_ENABLE:
            return
        if not (BITQUERY_API_VALUE and _GQL_AVAILABLE):
            log.warning(
                "GRAD_STREAM_ENABLE is set but streams cannot start "
                "(bitquery_key=%s, gql_installed=%s).",
                bool(BITQUERY_API_VALUE),
                _GQL_AVAILABLE,
            )
            return

        for attr, worker, label in (
            ("_bonk_stream_task", _bonk_stream_worker, "BONK"),
            ("_bags_stream_task", _bags_stream_worker, "BAGS"),
            ("_heaven_stream_task", _heaven_stream_worker, "HEAVEN"),
        ):
            if getattr(self, attr) is None:
                setattr(self, attr, asyncio.create_task(worker(self.bot), name=f"grad-stream-{label.lower()}"))
                log.info("%s graduation stream task started.", label)

    # ---------------- /graduated_report ----------------

    @app_commands.command(name="graduated_report", description="Show graduated tokens in the past X hours")
    @app_commands.describe(hours="How many hours back to report (1-24)")
    async def graduated_report(self, interaction: discord.Interaction, hours: int = 1):
        hours = max(1, min(int(hours or 1), 24))
        await interaction.response.defer(thinking=True)
        # Floor to whole hours so manual runs line up with the hourly task.
        end = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
        start = end - dt.timedelta(hours=hours)
        try:
            msg = await self.fetch_report(start, end)
        except Exception as e:
            log.exception("graduated_report failed")
            msg = f"⚠️ Graduated report error: {type(e).__name__}: {e}"
        # Discord caps a single message at 2000 chars.
        await interaction.followup.send(msg[:1990] if len(msg) > 2000 else msg)

    @tasks.loop(hours=1)
    async def hourly_graduated_report(self):
        log.debug("========== Starting Hourly Graduated Report ==========")

        if not self._healthcheck_done:
            await _bitquery_healthcheck()
            self._healthcheck_done = True

        end = dt.datetime.now(dt.timezone.utc)
        start = end - dt.timedelta(hours=1)

        log.debug(f"Report time window: {start.isoformat()} to {end.isoformat()}")

        try:
            msg = await self.fetch_report(start, end)
        except Exception as e:
            log.warning(f"Report generation error: {type(e).__name__}: {e}")
            import traceback
            log.debug(f"Traceback: {traceback.format_exc()}")
            msg = f"⚠️ Graduated report error: {e}"

        ch = self.bot.get_channel(CHANNEL_ID)
        if ch:
            log.debug(f"Sending report to channel {CHANNEL_ID}")
            await safe_send(ch, msg)
        else:
            log.warning(f"ERROR: Could not find channel {CHANNEL_ID}")

        log.debug("========== Report Complete ==========\n")

    @hourly_graduated_report.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    async def get_recent_graduates(self, start: dt.datetime, end: dt.datetime) -> List[Dict[str, Any]]:
        tokens_by_lp = await _fetch_recent_graduates_all(start, end, limit=100)

        log.debug("=== Filtering tokens by time window ===")
        for lp, tokens in tokens_by_lp.items():
            log.debug(f"{lp}: {len(tokens)} raw tokens before time filtering")

        grads: List[Dict[str, Any]] = []
        for lp, tokens in tokens_by_lp.items():
            filtered_count = 0
            for t in tokens:
                ts = _parse_token_timestamp(t)
                if ts and (start <= ts < end):
                    grads.append({**t, "launchpad": lp})
                    filtered_count += 1
            log.debug(f"{lp}: {filtered_count} tokens after time filtering")

        log.debug(f"Total graduated tokens in window: {len(grads)}")
        return grads

    async def fetch_report(self, start: dt.datetime, end: dt.datetime) -> str:
        log.debug("Fetching graduated tokens...")
        grads = await self.get_recent_graduates(start, end)

        window_str = (
            f"{start.astimezone(PACIFIC_TZ):%I:%M %p %Z} – "
            f"{end.astimezone(PACIFIC_TZ):%I:%M %p %Z}"
        )

        if not grads:
            return f"🕒 {window_str}: No tokens graduated."

        log.debug(f"=== Fetching market cap data for {len(grads)} tokens ===")

        # Fetch market cap data for all tokens (bounded concurrency)
        sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

        async def enrich(g: Dict[str, Any]):
            ca = (g.get("mint") or g.get("address") or g.get("tokenAddress") or "").strip()
            if not ca:
                g["mc_val"] = 0.0
                g["liq_val"] = 0.0
                return g
            async with sem:
                ds = await _dexscreener_info(ca)
            if ds:
                g["mc_val"] = ds["mc"]
                g["liq_val"] = ds["liq"]
                g["name"] = ds.get("name") or g.get("name", "—")
                g["symbol"] = ds.get("symbol") or g.get("symbol", "—")
            else:
                g["mc_val"] = float(g.get("marketCap", 0.0))
                g["liq_val"] = float(g.get("liquidity", 0.0))
            return g

        grads = await asyncio.gather(*(enrich(g) for g in grads))

        # Separate by platform and filter by market cap
        pumpfun_grads = [g for g in grads if g.get("launchpad") == "pumpfun"]
        bonk_grads = [g for g in grads if g.get("launchpad") == "bonk"]
        bags_grads = [g for g in grads if g.get("launchpad") == "bags"]
        heaven_grads = [g for g in grads if g.get("launchpad") == "heaven"]
        other_grads = [g for g in grads if g.get("launchpad") not in ["pumpfun", "bonk", "bags", "heaven"]]

        log.debug("=== Platform breakdown ===")
        log.debug(f"PumpFun: {len(pumpfun_grads)} tokens")
        log.debug(f"Bonk: {len(bonk_grads)} tokens")
        log.debug(f"Bags: {len(bags_grads)} tokens")
        log.debug(f"Heaven: {len(heaven_grads)} tokens")
        log.debug(f"Other: {len(other_grads)} tokens")

        # Filter for display (only show >= threshold)
        pumpfun_display = [g for g in pumpfun_grads if g.get("mc_val", 0) >= MIN_MC_DISPLAY]
        bonk_display = [g for g in bonk_grads if g.get("mc_val", 0) >= MIN_MC_DISPLAY]
        bags_display = [g for g in bags_grads if g.get("mc_val", 0) >= MIN_MC_DISPLAY]
        heaven_display = [g for g in heaven_grads if g.get("mc_val", 0) >= MIN_MC_DISPLAY]
        other_display = [g for g in other_grads if g.get("mc_val", 0) >= MIN_MC_DISPLAY]

        log.debug(f"=== After {MIN_MC_DISPLAY:,} MC filter ===")
        log.debug(f"PumpFun display: {len(pumpfun_display)} tokens")
        log.debug(f"Bonk display: {len(bonk_display)} tokens")
        log.debug(f"Bags display: {len(bags_display)} tokens")
        log.debug(f"Heaven display: {len(heaven_display)} tokens")
        log.debug(f"Other display: {len(other_display)} tokens")

        # Build report sections
        report_sections: List[str] = []

        # Header (show total counts including sub-threshold tokens)
        if len(grads) > 10:
            report_sections.append('🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥')
        elif len(grads) < 4:
            report_sections.append("🧊🧊🧊🧊🧊🧊🧊🧊🧊🧊🧊🧊🧊🧊🧊🧊🧊")
        else:
            report_sections.append("------------------------------------------")
        report_sections.append(
            f"\n🕒 Graduated between {window_str}\n"
            f"🎓 Total: **{len(grads)}** tokens\n"
        )

        # Summary counts (all tokens)
        counts: Dict[str, int] = {}
        for g in grads:
            lp = g.get("launchpad", "unknown")
            counts[lp] = counts.get(lp, 0) + 1
        breakdown = "\n".join(f"   {lp}: {n}" for lp, n in counts.items())
        report_sections.append(f"{breakdown}\n")

        # Section divider
        if len(grads) > 10:
            report_sections.append('🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥')
        elif len(grads) < 4:
            report_sections.append("🧊🧊🧊🧊🧊🧊🧊🧊🧊🧊🧊🧊🧊🧊🧊🧊🧊")
        else:
            report_sections.append("------------------------------------------")

        def _line(g: Dict[str, Any]) -> str:
            ca = (g.get("mint") or g.get("address") or g.get("tokenAddress") or "").strip()
            name = g.get("name", "—")
            symbol = g.get("symbol", "—")
            mc_val = g.get("mc_val", 0)
            liq_val = g.get("liq_val", 0)
            gmgn_url = f"<https://gmgn.ai/sol/token/{ca}>" if ca else ""
            # ONLY the token name is clickable to GMGN; no extra links.
            if gmgn_url:
                title = f"[{name} ({symbol})]({gmgn_url})"
            else:
                title = f"{name} ({symbol})"
            return f"• {title} — MC: {human_money(mc_val):>7}   Liq: {human_money(liq_val):>7}"

        # Sections
        if pumpfun_display:
            report_sections.append("\n📈 **PUMPFUN GRADUATES** (MC ≥ 100K)")
            for g in pumpfun_display:
                report_sections.append(_line(g))

        if bonk_display:
            report_sections.append("\n🚀 **BONK / LAUNCHLAB GRADUATES** (MC ≥ 100K)")
            for g in bonk_display:
                report_sections.append(_line(g))

        if bags_display:
            report_sections.append("\n💼 **BAGS (DBC → DAMM) GRADUATES** (MC ≥ 100K)")
            for g in bags_display:
                report_sections.append(_line(g))

        if heaven_display:
            report_sections.append("\n👼 **HEAVEN GO-LIVE** (MC ≥ 100K)")
            for g in heaven_display:
                report_sections.append(_line(g))

        if other_display:
            report_sections.append("\n🎯 **OTHER PLATFORMS** (MC ≥ 100K)")
            for g in other_display:
                report_sections.append(_line(g))

        if len(grads) > 0 and (len(pumpfun_display) + len(bonk_display) + len(bags_display) + len(heaven_display) + len(other_display) == 0):
            report_sections.append("\n💡 All graduated tokens are currently under the display threshold")

        log.debug("Report generation complete")
        return "\n".join(report_sections)

async def setup(bot: commands.Bot):
    await bot.add_cog(GraduatedCog(bot))
