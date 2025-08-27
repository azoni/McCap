import asyncio
import time
from typing import Optional
import discord
from .storage import invoices, save_invoices
from .config import PAY_EXPIRY_SEC, PAY_POLL_SEC, USDC_MINT, DONATION_WALLET
from .solana import sol_rpc, _random_pubkey
from .helpers import to_lamports, is_solana_address
from .logging_setup import log

def solana_pay_link(recipient: str, amount: float, *, label: str="McCap", message: str="", reference: str="", spl_token: Optional[str]=None) -> str:
    from urllib.parse import quote
    qs=[f"amount={amount:.9f}"]
    if reference: qs.append(f"reference={quote(reference)}")
    if label:     qs.append(f"label={quote(label)}")
    if message:   qs.append(f"message={quote(message)}")
    if spl_token: qs.append(f"spl-token={quote(spl_token)}")
    return f"solana:{recipient}?{'&'.join(qs)}"

def qr_url(data: str, size: int = 240) -> str:
    from urllib.parse import quote
    return f"https://api.qrserver.com/v1/create-qr-code/?size={size}x{size}&data={quote(data)}"

def _sum_usdc_delta_for_wallet(meta: dict, msg: dict, mint: str, owner_wallet: str) -> int:
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in (msg.get("message") or {}).get("accountKeys", [])]
    pre = meta.get("preTokenBalances") or []; post= meta.get("postTokenBalances") or []
    def _tb_map(tbs):
        out={}; 
        for tb in tbs:
            try:
                if tb.get("mint") != mint: continue
                idx=tb.get("accountIndex"); owner=tb.get("owner")
                dec=int(((tb.get("uiTokenAmount") or {}).get("decimals") or 0))
                amt=int(((tb.get("uiTokenAmount") or {}).get("amount") or "0"))
                out[idx]=(owner, amt, dec)
            except Exception: continue
        return out
    pre_map=_tb_map(pre); post_map=_tb_map(post)
    delta_total=0
    for idx in set(pre_map.keys()) | set(post_map.keys()):
        pre_t=pre_map.get(idx); post_t=post_map.get(idx)
        owner=(post_t[0] if post_t else (pre_t[0] if pre_t else None))
        if owner != owner_wallet: continue
        pre_amt=pre_t[1] if pre_t else 0; post_amt=post_t[1] if post_t else 0
        dec=(post_t[2] if post_t else (pre_t[2] if pre_t else 0))
        if dec != 6: continue
        delta=post_amt-pre_amt
        if delta>0: delta_total+=delta
    return delta_total

async def _find_matching_tx(reference: str, inv) -> Optional[str]:
    if not is_solana_address(DONATION_WALLET):
        log.warning("DONATION_WALLET not set or invalid; cannot verify payments.")
        return None
    sigs = await sol_rpc("getSignaturesForAddress", [DONATION_WALLET, {"limit": 50}])
    if not sigs or "result" not in sigs: return None
    for ent in sigs["result"]:
        sig = ent.get("signature")
        tx = await sol_rpc("getTransaction", [sig, {"encoding":"jsonParsed","maxSupportedTransactionVersion":0}])
        try:
            res=(tx or {}).get("result") or {}; msg=res.get("transaction") or {}; meta=res.get("meta") or {}
            keys=[k.get("pubkey") if isinstance(k, dict) else k for k in (msg.get("message") or {}).get("accountKeys", [])]
            if reference not in keys: continue
            if inv.asset=="SOL":
                pre=meta.get("preBalances") or []; post=meta.get("postBalances") or []
                idx=None
                for i,k in enumerate(keys):
                    if k==DONATION_WALLET: idx=i; break
                if idx is None: continue
                pre_bal=pre[idx] if idx<len(pre) else None; post_bal=post[idx] if idx<len(post) else None
                if pre_bal is None or post_bal is None: continue
                if post_bal - pre_bal >= inv.amount_base: return sig
            else:
                delta=_sum_usdc_delta_for_wallet(meta, msg, inv.mint, DONATION_WALLET)
                if delta >= inv.amount_base: return sig
        except Exception:
            continue
    return None

async def payments_watcher(client: discord.Client):
    await client.wait_until_ready()
    while not client.is_closed():
        now = time.time(); changed=False
        for inv in list(invoices):
            if inv.status != "pending": continue
            if now - inv.created_ts > PAY_EXPIRY_SEC:
                inv.status="expired"; changed=True
                try:
                    ch=await client.fetch_channel(inv.channel_id)
                    await ch.send(f"⌛ Payment `{inv.id}` expired.")
                    log.info(f"Payment expired | id={inv.id} user={inv.user_id} asset={inv.asset}")
                except Exception:
                    import logging; logging.exception("Failed to send payment expiry message")
                continue
            sig = await _find_matching_tx(inv.reference, inv)
            if sig:
                inv.status="paid"; inv.tx_sig=sig; changed=True
                try:
                    ch=await client.fetch_channel(inv.channel_id)
                    amt_str=f"{inv.amount_base / (10**inv.decimals):,.4f} {inv.asset}"
                    embed=discord.Embed(title="✅ Payment received", description=f"{amt_str} to bot wallet\n`{sig}`", color=0x2ecc71)
                    await ch.send(content=f"<@{inv.user_id}>", embed=embed,
                                  allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False, replied_user=False))
                    log.info(f"Payment confirmed | id={inv.id} user={inv.user_id} asset={inv.asset} sig={sig}")
                except Exception:
                    import logging; logging.exception("Failed to send payment confirmation")
        if changed: await save_invoices()
        await asyncio.sleep(PAY_POLL_SEC)

def parse_asset_choice(s: Optional[str]) -> tuple[str,int,Optional[str]]:
    if not s: return ("SOL", 9, None)
    t=s.strip().upper()
    if t=="USDC": return ("USDC", 6, USDC_MINT)
    return ("SOL", 9, None)

def new_invoice_id() -> str:
    return _random_pubkey()[:6]
