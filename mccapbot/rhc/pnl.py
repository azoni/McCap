"""Cost basis, worth, multiples and profit for a wallet, from the trade journal.

The journal records every confirmed buy and sell with the dollars that went in
or came out (KyberSwap's figures at the time), the tokens moved, the market cap
at the time, and the gas the receipt charged. From that:

* cost basis of a position (average cost per token, entry market cap),
* realized profit on what was sold, unrealized profit on what is still held
  (valued at DexScreener's current price), gas as its own line,
* the multiple: current (or sold-at) market cap over the entry market cap,
  which is the "bought at 50K, sold at 200K, 4x" people actually say.

Prices at trade time come from the aggregator's USD figures and DexScreener,
so this is an honest estimate, not an exchange statement.
"""

import io
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..dex import token_summary
from ..logging_setup import log
from . import chain, ledger, wallets


@dataclass
class TokenPnl:
    token: str
    symbol: str = "?"
    decimals: int = 18
    bought_raw: int = 0
    cost_usd: float = 0.0
    buys: int = 0
    sold_raw: int = 0
    proceeds_usd: float = 0.0
    sells: int = 0
    gas_wei: int = 0
    entry_mc_weight: float = 0.0      # sum(mc at buy × usd_in), for a cost-weighted entry MC
    last_sell_mc: Optional[float] = None
    last_trade_ts: float = 0.0
    # live
    balance_raw: int = 0
    price_now: Optional[float] = None
    mc_now: Optional[float] = None

    def _u(self, raw: int) -> float:
        return raw / 10 ** self.decimals

    @property
    def bought(self) -> float:
        return self._u(self.bought_raw)

    @property
    def sold(self) -> float:
        return self._u(self.sold_raw)

    @property
    def balance(self) -> float:
        return self._u(self.balance_raw)

    @property
    def avg_cost(self) -> Optional[float]:
        """Dollars paid per token, over every buy."""
        return self.cost_usd / self.bought if self.bought > 0 and self.cost_usd > 0 else None

    @property
    def entry_mc(self) -> Optional[float]:
        return self.entry_mc_weight / self.cost_usd if self.cost_usd > 0 and self.entry_mc_weight > 0 else None

    @property
    def worth_usd(self) -> Optional[float]:
        return self.balance * self.price_now if self.price_now is not None else None

    @property
    def open_cost_usd(self) -> Optional[float]:
        return self.balance * self.avg_cost if self.avg_cost is not None else None

    @property
    def realized_usd(self) -> Optional[float]:
        if self.sells == 0:
            return 0.0
        if self.avg_cost is None:
            return None
        return self.proceeds_usd - self.sold * self.avg_cost

    @property
    def unrealized_usd(self) -> Optional[float]:
        if self.worth_usd is None or self.open_cost_usd is None:
            return None
        return self.worth_usd - self.open_cost_usd

    @property
    def pnl_usd(self) -> Optional[float]:
        r, u = self.realized_usd, self.unrealized_usd
        if r is None and u is None:
            return None
        return (r or 0.0) + (u or 0.0)

    @property
    def pnl_pct(self) -> Optional[float]:
        p = self.pnl_usd
        return p / self.cost_usd * 100.0 if p is not None and self.cost_usd > 0 else None

    @property
    def multiple_now(self) -> Optional[float]:
        """Current market cap over entry market cap (falls back to price over avg cost)."""
        if self.entry_mc and self.mc_now:
            return self.mc_now / self.entry_mc
        if self.avg_cost and self.price_now:
            return self.price_now / self.avg_cost
        return None


@dataclass
class UserPnl:
    user_id: int
    address: str
    positions: List[TokenPnl] = field(default_factory=list)
    eth_wei: int = 0
    eth_usd: Optional[float] = None
    gas_wei: int = 0
    fetched_ts: float = field(default_factory=time.time)

    @property
    def eth(self) -> float:
        return self.eth_wei / 1e18

    @property
    def eth_value_usd(self) -> Optional[float]:
        return self.eth * self.eth_usd if self.eth_usd else None

    @property
    def gas_eth(self) -> float:
        return self.gas_wei / 1e18

    @property
    def gas_usd(self) -> Optional[float]:
        return self.gas_eth * self.eth_usd if self.eth_usd else None

    @property
    def open_positions(self) -> List[TokenPnl]:
        return [p for p in self.positions if p.balance_raw > 0]

    @property
    def tokens_worth_usd(self) -> float:
        return sum(p.worth_usd or 0.0 for p in self.open_positions)

    @property
    def total_usd(self) -> Optional[float]:
        if self.eth_value_usd is None and not self.open_positions:
            return None
        return (self.eth_value_usd or 0.0) + self.tokens_worth_usd

    @property
    def realized_usd(self) -> float:
        return sum(p.realized_usd or 0.0 for p in self.positions)

    @property
    def unrealized_usd(self) -> float:
        return sum(p.unrealized_usd or 0.0 for p in self.open_positions)

    @property
    def pnl_usd(self) -> float:
        """Realized plus unrealized, minus gas: the number that matters."""
        return self.realized_usd + self.unrealized_usd - (self.gas_usd or 0.0)

    @property
    def cost_usd(self) -> float:
        return sum(p.cost_usd for p in self.positions)


# ---------------- from the journal ----------------

def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def aggregate(trades: List[Dict[str, Any]]) -> Dict[str, TokenPnl]:
    """Per-token totals from confirmed trades (``ledger.trades`` output)."""
    out: Dict[str, TokenPnl] = {}
    for e in trades:
        kind = e.get("kind")
        if kind not in ("buy", "sell"):
            continue
        token = (e.get("token") or "").lower()
        if not token:
            continue
        p = out.get(token)
        if p is None:
            p = out[token] = TokenPnl(token=token, symbol=e.get("symbol") or "?", decimals=int(e.get("decimals") or 18))
        p.gas_wei += int(_f(e.get("gas_cost_wei")))
        p.last_trade_ts = max(p.last_trade_ts, _f(e.get("ts")))
        mc = _f(e.get("mc_usd"), 0.0) or None
        if kind == "buy":
            got = int(_f(e.get("actual_out_estimate") or e.get("quoted_out")))
            usd = _f(e.get("usd_in"))
            p.bought_raw += got
            p.cost_usd += usd
            p.buys += 1
            if mc and usd:
                p.entry_mc_weight += mc * usd
        else:
            p.sold_raw += int(_f(e.get("amount_in")))
            p.proceeds_usd += _f(e.get("usd_out"))
            p.sells += 1
            if mc:
                p.last_sell_mc = mc
    return out


def entry_for(user_id: int, token: str) -> Optional[TokenPnl]:
    """The position record for one token, journal only (no network)."""
    return aggregate(ledger.trades(user_id)).get((token or "").lower())


async def user_pnl(user_id: int, address: str) -> UserPnl:
    """Journal totals plus live balances and prices. Best effort per token."""
    u = UserPnl(user_id=user_id, address=address)
    positions = aggregate(ledger.trades(user_id))
    u.gas_wei = sum(int(_f(e.get("gas_cost_wei"))) for e in ledger.trades(user_id))
    try:
        eth = await token_summary(chain.WETH)
        u.eth_usd = (eth or {}).get("price")
    except Exception:
        u.eth_usd = None
    try:
        u.eth_wei = await chain.native_balance(address)
    except chain.ChainError:
        u.eth_wei = 0
    for token, p in positions.items():
        try:
            p.balance_raw = await chain.erc20_balance(token, address)
            sym, dec = await chain.erc20_meta(token)
            p.symbol, p.decimals = sym or p.symbol, dec
            info = await token_summary(token)
            p.price_now = (info or {}).get("price")
            p.mc_now = (info or {}).get("mc")
        except Exception as e:  # noqa: BLE001
            log.debug("Could not enrich %s for %s: %s", token, address, e)
        u.positions.append(p)
    u.positions.sort(key=lambda p: -(p.worth_usd or 0.0))
    return u


# ---------------- group metrics ----------------

@dataclass
class GroupStats:
    wallets: int = 0
    traders: int = 0
    buys: int = 0
    sells: int = 0
    volume_usd: float = 0.0
    gas_wei: int = 0
    realized_usd: float = 0.0
    best_multiple: Optional[float] = None
    best_symbol: str = ""


def group_stats() -> GroupStats:
    """Across every wallet, from the journal alone."""
    g = GroupStats(wallets=wallets.count())
    for w in wallets.wallets:
        trades = ledger.trades(w.user_id)
        if not trades:
            continue
        g.traders += 1
        for e in trades:
            g.gas_wei += int(_f(e.get("gas_cost_wei")))
            if e.get("kind") == "buy":
                g.buys += 1
                g.volume_usd += _f(e.get("usd_in"))
            elif e.get("kind") == "sell":
                g.sells += 1
                g.volume_usd += _f(e.get("usd_out"))
                m = _f(e.get("multiple"), 0.0)
                if m and (g.best_multiple is None or m > g.best_multiple):
                    g.best_multiple, g.best_symbol = m, e.get("symbol") or ""
        for p in aggregate(trades).values():
            g.realized_usd += p.realized_usd or 0.0
    return g


# ---------------- formatting ----------------

def fmt_usd(v: Optional[float], signed: bool = False) -> str:
    if v is None:
        return "—"
    if signed:
        return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"
    return f"${v:,.2f}"


def fmt_x(m: Optional[float]) -> str:
    if m is None:
        return "—"
    return f"{m:.2f}x" if m < 10 else f"{m:.1f}x"


def fmt_amount(v: float) -> str:
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 1:
        return f"{v:,.2f}"
    return f"{v:.4f}".rstrip("0").rstrip(".") or "0"


# ---------------- chart ----------------

def render_chart(u: UserPnl, title: str) -> Optional[bytes]:
    """A PNG bar chart of profit per token (realized + unrealized), gas as its own bar."""
    rows = [(p.symbol, p.pnl_usd) for p in u.positions if p.pnl_usd is not None]
    if u.gas_usd:
        rows.append(("gas", -u.gas_usd))
    if not rows:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:  # noqa: BLE001
        log.warning("matplotlib unavailable; no chart")
        return None
    labels = [r[0][:10] for r in rows]
    values = [r[1] for r in rows]
    fig, ax = plt.subplots(figsize=(7, 3.4), dpi=150)
    fig.patch.set_facecolor("#2b2d31")
    ax.set_facecolor("#2b2d31")
    colours = ["#57F287" if v >= 0 else "#ED4245" for v in values]
    bars = ax.bar(labels, values, color=colours)
    ax.axhline(0, color="#9aa0a6", linewidth=0.8)
    for bar, v in zip(bars, values):
        ax.annotate(fmt_usd(v, signed=True), (bar.get_x() + bar.get_width() / 2, v),
                    ha="center", va="bottom" if v >= 0 else "top", fontsize=8, color="#e3e5e8",
                    xytext=(0, 3 if v >= 0 else -3), textcoords="offset points")
    ax.set_title(f"{title} · total {fmt_usd(u.pnl_usd, signed=True)}", color="#e3e5e8", fontsize=11)
    ax.tick_params(colors="#e3e5e8", labelsize=8)
    for spine in ax.spines.values():
        spine.set_color("#4e5058")
    ax.set_ylabel("USD", color="#9aa0a6", fontsize=8)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()
