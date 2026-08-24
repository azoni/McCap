"""Extract Solana mints from another bot's scan message.

Scanner bots like Rick reply to a pasted contract address with an embed of
stats plus trade quicklinks (Photon, BananaGun, Maestro, chart sites). Their
exact embed layout is undocumented and changes without notice, so this
deliberately does NOT target a specific field. It sweeps every text surface a
message has — content, all embed text, and component/button URLs — collects
base58 candidates, and lets the caller confirm them against DexScreener.

The important property is observability: `scan_surfaces` reports what it looked
at, so a message that yields no mint can be logged with enough detail to see
whether the format moved. A parser that silently matches nothing is worse than
no parser.
"""

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Set
from urllib.parse import unquote

from .helpers import is_solana_address

# Base58 run long enough to be a mint. Deliberately loose — candidates are
# validated by is_solana_address and then by an actual DexScreener lookup.
# Anchored on both sides: unanchored, an 88-character transaction signature
# is chopped into two 44-character runs that both look like valid mints.
_B58 = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")

# Hosts whose URLs carry the mint in the path or query. Used only to prioritise
# candidates, never to gate them: an unknown host still gets its base58 scanned.
KNOWN_SCAN_HOSTS = (
    "dexscreener.com", "birdeye.so", "gmgn.ai", "solscan.io", "pump.fun",
    "photon-sol.tinyastro.io", "bananagun.io", "t.me", "jup.ag", "raydium.io",
    "bullx.io", "axiom.trade", "neo.bullx.io", "solanatracker.io",
    "geckoterminal.com", "rugcheck.xyz", "dextools.io",
)

# Mints that show up in almost every pool link and are never the scanned token.
IGNORED_MINTS = {
    "So11111111111111111111111111111111111111112",   # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


@dataclass
class Surfaces:
    """Every text region of a message that could hold a mint."""

    texts: List[str] = field(default_factory=list)
    urls: List[str] = field(default_factory=list)

    def describe(self) -> str:
        return f"{len(self.texts)} text region(s), {len(self.urls)} url(s)"


def scan_surfaces(message) -> Surfaces:
    """Collect text and URLs from a discord.Message without assuming a layout.

    Tolerant of missing attributes so it can be fed real messages, partial
    interaction-resolved messages, or plain test doubles.
    """
    s = Surfaces()

    content = getattr(message, "content", None)
    if content:
        s.texts.append(content)

    for embed in getattr(message, "embeds", None) or []:
        # discord.Embed exposes attributes; dicts turn up in raw payloads.
        get = (lambda k: embed.get(k)) if isinstance(embed, dict) else (lambda k: getattr(embed, k, None))

        for key in ("title", "description"):
            val = get(key)
            if val:
                s.texts.append(str(val))

        url = get("url")
        if url:
            s.urls.append(str(url))

        for key in ("author", "footer"):
            sub = get(key)
            if sub is None:
                continue
            for attr in ("name", "text", "url", "icon_url"):
                val = sub.get(attr) if isinstance(sub, dict) else getattr(sub, attr, None)
                if val:
                    (s.urls if "url" in attr else s.texts).append(str(val))

        for f in (get("fields") or []):
            for attr in ("name", "value"):
                val = f.get(attr) if isinstance(f, dict) else getattr(f, attr, None)
                if val:
                    s.texts.append(str(val))

        for key in ("thumbnail", "image"):
            sub = get(key)
            if sub is not None:
                val = sub.get("url") if isinstance(sub, dict) else getattr(sub, "url", None)
                if val:
                    s.urls.append(str(val))

    # Link buttons: Rick's trade shortcuts point at the mint.
    for row in getattr(message, "components", None) or []:
        for child in (getattr(row, "children", None) or getattr(row, "components", None) or []):
            url = getattr(child, "url", None) or (child.get("url") if isinstance(child, dict) else None)
            if url:
                s.urls.append(str(url))
            label = getattr(child, "label", None) or (child.get("label") if isinstance(child, dict) else None)
            if label:
                s.texts.append(str(label))

    return s


def _candidates(blob: str) -> List[str]:
    return [m for m in _B58.findall(blob) if is_solana_address(m) and m not in IGNORED_MINTS]


def extract_mints(message) -> List[str]:
    """Ordered, de-duplicated mint candidates, most likely first.

    URLs from recognised scanner/chart hosts rank above loose text, because a
    mint sitting in a dexscreener link is far more likely to be *the* scanned
    token than an arbitrary base58 run in a stats blob.
    """
    s = scan_surfaces(message)
    ranked: List[str] = []
    seen: Set[str] = set()

    def push(items: Iterable[str]) -> None:
        for m in items:
            if m not in seen:
                seen.add(m)
                ranked.append(m)

    known = [u for u in s.urls if any(h in u.lower() for h in KNOWN_SCAN_HOSTS)]
    other = [u for u in s.urls if u not in known]

    for group in (known, other):
        for url in group:
            push(_candidates(unquote(url)))
    for text in s.texts:
        push(_candidates(text))

    return ranked


def is_scanner_message(message, scanner_ids: Set[int], self_id: Optional[int]) -> bool:
    """Whether a message should be treated as a scan to parse.

    Ignoring our own id is not optional: McCap's alert embeds contain mints and
    chart links, so reacting to them would make the bot scan itself in a loop.
    """
    author = getattr(message, "author", None)
    if author is None:
        return False
    author_id = getattr(author, "id", None)
    if author_id is None or (self_id is not None and author_id == self_id):
        return False
    if not getattr(author, "bot", False):
        return False
    if scanner_ids:
        return author_id in scanner_ids
    # No allowlist configured: accept any other bot, but only if the message
    # actually carries something mint-shaped, so ordinary chatter is ignored.
    return bool(extract_mints(message))
