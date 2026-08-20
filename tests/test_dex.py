from mccapbot.dex import choose_consensus_pair, resolve_mc_value, summarize_lp_venues
from mccapbot.tables import fixed_table

SOL_CA = "So11111111111111111111111111111111111111112"


def pair(mc=None, fdv=None, liq=0.0, vol=0.0, dex="raydium", chain="solana", ca=SOL_CA, buys=0, sells=0):
    return {
        "chainId": chain,
        "dexId": dex,
        "baseToken": {"address": ca, "symbol": "TOK", "name": "Token"},
        "quoteToken": {"symbol": "SOL"},
        "marketCap": mc,
        "fdv": fdv,
        "liquidity": {"usd": liq},
        "volume": {"h24": vol},
        "txns": {"h24": {"buys": buys, "sells": sells}},
        "url": "https://dexscreener.com/solana/x",
    }


def test_resolve_mc_prefers_fdv_on_solana():
    val, src = resolve_mc_value(pair(mc=1_000, fdv=2_000), SOL_CA)
    assert (val, src) == (2_000, "fdv")


def test_resolve_mc_falls_back_to_market_cap():
    val, src = resolve_mc_value(pair(mc=1_000, fdv=None), SOL_CA)
    assert (val, src) == (1_000, "marketCap")


def test_resolve_mc_none_when_absent():
    val, src = resolve_mc_value(pair(), SOL_CA)
    assert val is None and src == "none"


def test_consensus_rejects_blacklisted_dex():
    # 'heaven' is blacklisted, so a heaven-only token has no usable pair.
    best, _, _ = choose_consensus_pair([pair(fdv=1_000, dex="heaven")], SOL_CA)
    assert best is None


def test_consensus_ignores_wild_outlier():
    pairs = [
        pair(fdv=1_000_000, liq=50_000),
        pair(fdv=1_020_000, liq=40_000),
        pair(fdv=980_000, liq=30_000),
        pair(fdv=900_000_000_000, liq=1),  # bogus pool
    ]
    best, consensus, _ = choose_consensus_pair(pairs, SOL_CA)
    assert 900_000 <= consensus <= 1_100_000
    assert best["fdv"] != 900_000_000_000


def test_consensus_ignores_pairs_for_other_tokens():
    other = pair(fdv=5_000_000, ca="OtherMintAddress1111111111111111111111111")
    mine = pair(fdv=1_000_000)
    best, _, _ = choose_consensus_pair([other, mine], SOL_CA)
    assert best is mine


def test_summarize_lp_picks_deepest_venue():
    pairs = [
        pair(fdv=1_000_000, dex="raydium", liq=500_000, vol=1_000_000, buys=500, sells=500),
        pair(fdv=1_000_000, dex="meteora", liq=1_000, vol=500, buys=1, sells=1),
    ]
    agg, best = summarize_lp_venues(pairs, SOL_CA)
    assert best is not None
    assert best[0] == "raydium"
    assert agg["raydium"]["liq"] == 500_000


def test_fixed_table_autosizes_columns():
    """The old hard-coded widths mangled any table that wasn't 5 columns."""
    out = fixed_table(
        ["#", "ID", "Token", "Target", "Current", "By"],
        [["1", "a1b2c3", "TOK", "≥ $1.00M", "$500.00K", "charlton"]],
        ["r", "l", "l", "r", "r", "l"],
    )
    assert "a1b2c3" in out
    # A 6th column is rendered rather than dropped.
    assert "charlton" in out
    assert out.startswith("```") and out.rstrip().endswith("```")


def test_fixed_table_truncates_past_max_width():
    out = fixed_table(["Name"], [["x" * 50]], ["l"], max_width=10)
    assert "…" in out
