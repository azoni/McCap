import asyncio
from typing import Dict
from .models import TokenSnapshot
from .helpers import humanize
from .dex import build_token_url

token_cache: Dict[str, TokenSnapshot] = {}
TOKEN_CACHE_LOCK = asyncio.Lock()

async def update_cache(ca: str, *, mc, url, source="unknown", dex="", chain="", quote="", consensus=0.0, image_url=""):
    delta = (abs((mc or 0) - consensus) if (mc is not None and consensus) else None)
    async with TOKEN_CACHE_LOCK:
        token_cache[ca] = TokenSnapshot(
            mc=mc, url=(url or build_token_url(ca, None)), updated_ts=0.0,
            source=source, dex=dex or "", chain=chain or "", quote=quote or "",
            consensus=consensus, delta=delta, image_url=image_url or ""
        )
