import pytest

from mccapbot.dex import (
    _consensus_change_24h,
    _own_pairs,
    choose_consensus_pair,
    resolve_mc_value,
    summarize_lp_venues,
)
from mccapbot.tables import fixed_table

SOL_CA = "So11111111111111111111111111111111111111112"


def pair(mc=None, fdv=None, liq=0.0, vol=0.0, dex="raydium", chain="solana", ca=SOL_CA,
         buys=0, sells=0, quote="SOL", change24=None):
    return {
        "chainId": chain,
        "dexId": dex,
        "baseToken": {"address": ca, "symbol": "TOK", "name": "Token"},
        "quoteToken": {"symbol": quote},
        "marketCap": mc,
        "fdv": fdv,
        "liquidity": {"usd": liq},
        "volume": {"h24": vol},
        "txns": {"h24": {"buys": buys, "sells": sells}},
        "priceChange": ({"h24": change24} if change24 is not None else {}),
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


RSTR_CA = "0x78b96280c3347e0f58a7147b73eb0ec5ffff025d"


def evm_pair(mc, liq, quote="ETH"):
    p = pair(mc=mc, fdv=mc, liq=liq, dex="uniswap", chain="robinhood",
             ca="0x78b96280C3347E0f58a7147B73eb0EC5fFFf025d", quote=quote)
    return p


def test_dust_pools_cannot_drag_the_market_cap_down():
    """The real RSTR case (2026-09-07): two funded pools agree on ~$2.6M while
    twenty-one dust pools with stale prices pull the plain median to $1.04M."""
    real = [evm_pair(2_599_891, 151_898.98), evm_pair(2_586_099, 92_381.56, "USDG"),
            evm_pair(2_624_107, 9_547.70, "USDG"), evm_pair(2_494_676, 1_052.53, "USDG")]
    dust_mcs = [3_592_869, 1_038_694, 2_472_250, 2_714_243, 1_609_802, 802_219, 432_914,
                2_895_884, 849_506, 2_606_010, 328_237, 305_324, 2_334_781, 657_236,
                677_398, 262_511, 893_311, 555_454, 940_544]
    dust = [evm_pair(mc, liq) for mc, liq in zip(dust_mcs, [94, 53, 523, 390, 0, 10, 0.16,
                                                            12, 5, 6, 20, 18, 4, 6, 7, 30, 1, 1.6, 3])]
    best, consensus, _ = choose_consensus_pair(real + dust, RSTR_CA)
    assert 2_500_000 <= consensus <= 2_700_000, f"dust pools leaked into the consensus: {consensus}"
    assert best["liquidity"]["usd"] >= 90_000, "the reported pair should be a funded one"
    assert resolve_mc_value(best, RSTR_CA)[0] == consensus


def test_deep_pool_beats_a_tight_cluster_of_dust():
    """Outlier rejection alone would throw away the one real pool here."""
    pairs = [pair(fdv=5_000_000, liq=100_000)]
    pairs += [pair(fdv=1_000_000 + i * 10_000, liq=5 + i) for i in range(8)]
    _best, consensus, _ = choose_consensus_pair(pairs, SOL_CA)
    assert consensus == 5_000_000


def test_liquidity_weighted_median_prefers_the_funded_pool():
    pairs = [pair(fdv=1_000_000, liq=100_000), pair(fdv=2_000_000, liq=3_000)]
    _best, consensus, _ = choose_consensus_pair(pairs, SOL_CA)
    assert consensus == 1_000_000


def test_consensus_without_liquidity_data_is_the_plain_median():
    pairs = [pair(fdv=900_000, liq=0), pair(fdv=1_000_000, liq=0), pair(fdv=1_300_000, liq=0)]
    _best, consensus, _ = choose_consensus_pair(pairs, SOL_CA)
    assert consensus == 1_000_000


def test_bogus_funded_pool_is_still_an_outlier():
    """A pool with real liquidity but an absurd market cap must not win just
    because it clears the dust floor."""
    pairs = [
        pair(fdv=1_000_000, liq=50_000),
        pair(fdv=1_020_000, liq=40_000),
        pair(fdv=980_000, liq=30_000),
        pair(fdv=900_000_000_000, liq=10_000),
    ]
    _best, consensus, _ = choose_consensus_pair(pairs, SOL_CA)
    assert 900_000 <= consensus <= 1_100_000


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


# ---------------- 24h change consensus ----------------


def test_junk_quote_pool_cannot_hijack_the_change():
    """The real BONK case: its deepest pool is quoted in an obscure token and
    reports +542,339%, while every SOL/USDC pool agrees on ~11.6%."""
    pairs = [
        pair(liq=1_481_594, quote="TrumpBucks", change24=542339),
        pair(liq=108_575, quote="SOL", change24=11.21),
        pair(liq=104_056, quote="SOL", change24=11.69),
        pair(liq=102_227, quote="USDC", change24=11.67),
        pair(liq=68_984, quote="USDC", change24=11.7),
    ]
    change = _consensus_change_24h(pairs, total_liq=sum(p["liquidity"]["usd"] for p in pairs))
    assert 10 < change < 13, f"junk-quote pool leaked into the result: {change}"


def test_change_falls_back_when_no_major_quote_exists():
    """A token only paired against exotic quotes should still report something
    rather than silently claiming 0%."""
    pairs = [
        pair(liq=100, quote="WEIRD", change24=20.0),
        pair(liq=100, quote="ODD", change24=30.0),
    ]
    assert _consensus_change_24h(pairs, total_liq=200) == pytest.approx(25.0)


def test_change_is_zero_when_no_data():
    assert _consensus_change_24h([pair(liq=10, quote="SOL")], total_liq=10) == 0.0
    assert _consensus_change_24h([], total_liq=0) == 0.0


def test_change_median_ignores_a_single_outlier_major_pool():
    pairs = [
        pair(liq=100, quote="SOL", change24=10.0),
        pair(liq=100, quote="SOL", change24=11.0),
        pair(liq=100, quote="USDC", change24=9999.0),
    ]
    assert _consensus_change_24h(pairs, total_liq=300) == pytest.approx(11.0)


# ---------------- pair ownership ----------------


def test_own_pairs_excludes_other_tokens_and_blacklist():
    mine = pair(fdv=1_000)
    other = pair(fdv=1_000, ca="OtherMintAddress1111111111111111111111111")
    banned = pair(fdv=1_000, dex="heaven")
    out = _own_pairs([mine, other, banned], SOL_CA)
    assert out == [mine]


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


# ---------------- 1h volume and chain for the auto-order engine and alert buttons ----------------


def test_volume_1h_sums_own_pairs_and_tolerates_a_missing_figure():
    from mccapbot.dex import volume_1h
    a = pair(fdv=1_000, liq=10); a["volume"]["h1"] = 100.0
    b = pair(fdv=1_000, liq=10); b["volume"]["h1"] = 25.5
    c = pair(fdv=1_000, liq=10)                      # no h1 at all
    other = pair(fdv=1_000, liq=10, ca="Other111111111111111111111111111111111111111"); other["volume"]["h1"] = 999.0
    assert volume_1h([a, b, c, other], SOL_CA) == pytest.approx(125.5)
    assert volume_1h([c], SOL_CA) == 0.0


@pytest.mark.asyncio
async def test_token_summary_returns_vol1h_and_chain(monkeypatch):
    from mccapbot import dex
    evm = "0x" + "ab" * 20
    p = pair(fdv=2_000_000, liq=50_000, vol=1_000, chain="robinhood", ca=evm)
    p["volume"]["h1"] = 40.0
    p["priceUsd"] = "0.5"

    async def fake_fetch(ca):
        return {"pairs": [p]}
    monkeypatch.setattr(dex, "fetch_dex_token", fake_fetch)
    s = await dex.token_summary(evm)
    assert s["vol1h"] == 40.0 and s["chain"] == "robinhood" and s["price"] == 0.5 and s["vol24"] == 1_000


# ---------------- the rest of the pair payload ----------------


def test_pair_stats_tolerates_missing_and_junk_fields():
    from mccapbot.dex import liquidity_total, pair_stats
    full = pair(fdv=1_000, liq=10)
    full["priceChange"] = {"m5": "12.5", "h1": "-3"}
    full["txns"] = {"m5": {"buys": 41, "sells": 6}, "h1": {"buys": 300, "sells": "x"}, "h24": {"buys": 1, "sells": 1}}
    full["volume"] = {"h24": 10, "m5": "1800.5"}
    full["pairCreatedAt"] = 1_700_000_000_000
    full["pairAddress"] = "0xpool"
    s = pair_stats(full)
    assert s["change_m5"] == 12.5 and s["change_h1"] == -3.0 and s["buys_m5"] == 41 and s["sells_m5"] == 6
    assert s["buys_h1"] == 300 and s["sells_h1"] == 0 and s["vol_m5"] == 1800.5
    assert s["pair_created_ts"] == 1_700_000_000.0 and s["pair_address"] == "0xpool"
    bare = pair_stats(pair(fdv=1_000, liq=10))
    assert bare["change_m5"] is None and bare["buys_m5"] == 0 and bare["vol_m5"] is None and bare["pair_created_ts"] == 0.0
    assert pair_stats(None)["pair_address"] == ""
    assert liquidity_total([pair(fdv=1, liq=10), pair(fdv=1, liq=15), pair(fdv=1, liq=99, ca="Other111111111111111111111111111111111111111")], SOL_CA) == 25.0


@pytest.mark.asyncio
async def test_token_summary_carries_the_pair_stats(monkeypatch):
    from mccapbot import dex
    p = pair(fdv=2_000_000, liq=50_000, vol=1_000)
    p["txns"] = {"m5": {"buys": 3, "sells": 1}, "h24": {"buys": 10, "sells": 5}}
    p["priceChange"] = {"m5": "9", "h24": "20"}

    async def fake_fetch(ca):
        return {"pairs": [p]}
    monkeypatch.setattr(dex, "fetch_dex_token", fake_fetch)
    s = await dex.token_summary(SOL_CA)
    assert s["buys_m5"] == 3 and s["change_m5"] == 9.0 and s["change_h1"] is None and s["liq"] == 50_000


def test_usdg_quoted_pairs_count_toward_the_change_consensus():
    pairs = [pair(fdv=1_000, liq=500_000, quote="USDG", change24=10.0),
             pair(fdv=1_000, liq=1, quote="JUNK", change24=9999.0)]
    assert _consensus_change_24h(pairs, 500_001) == 10.0
