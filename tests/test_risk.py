"""Robinhood token risk line: parsing, caching, limiter yield and rendering."""

import asyncio
import copy
import time

import pytest

from mccapbot import gecko
from mccapbot.rhc import risk
from mccapbot.rhc.risk import TokenInfo, is_honeypot, parse_info, risk_line

# Shaped like the live reply for PONS (0x39db...4571) on 2026-09-09.
PONS = "0x39dbed3a2bd333467115de45665cc57f813c4571"
PAYLOAD = {
    "data": {
        "id": f"robinhood_{PONS}",
        "type": "token",
        "attributes": {
            "address": PONS,
            "name": "Pons",
            "symbol": "PONS",
            "decimals": 18,
            "coingecko_coin_id": "pons",
            "websites": ["https://ponsfamily.com"],
            "discord_url": None,
            "farcaster_url": None,
            "zora_url": None,
            "telegram_handle": None,
            "twitter_handle": "ponsdotfamily",
            "description": "",
            "gt_score": 93.65079365079364,
            "gt_score_details": {"pool": 93.333, "transaction": 100.0, "creation": 100.0, "info": 100.0, "holders": 87.5},
            "gt_verified": True,
            "categories": ["Pons Launchpad"],
            "gt_category_ids": ["pons-launchpad"],
            "holders": {
                "count": 88739,
                "distribution_percentage": {"top_10": "59.7907", "11_30": "13.8624", "31_50": "8.6633", "rest": "17.6836"},
                "last_updated": "2026-09-09T14:13:00Z",
            },
            "mint_authority": None,
            "freeze_authority": None,
            "is_honeypot": False,
            "developer_address": None,
            "developer_holding_percentage": None,
        },
    }
}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    risk.clear_cache()
    # A full bucket so nothing yields to the limiter unless a test empties it.
    monkeypatch.setattr(gecko.gecko_limiter, "tokens", float(gecko.gecko_limiter.capacity))
    monkeypatch.setattr(gecko.gecko_limiter, "updated", time.monotonic())
    yield
    risk.clear_cache()


def fake_fetch(monkeypatch, reply, calls=None):
    calls = calls if calls is not None else []

    async def fake(url, limiter=None, **kw):
        calls.append((url, limiter))
        return copy.deepcopy(reply) if callable(getattr(reply, "get", None)) else reply

    monkeypatch.setattr(risk, "get_json", fake)
    return calls


# ---------------- parsing ----------------


def test_parse_live_shaped_payload():
    info = parse_info(PAYLOAD, now=1234.0)
    assert info.holders == 88739
    assert info.top10_pct == pytest.approx(59.7907)
    assert info.dev_pct is None
    assert info.honeypot == "no"
    assert info.gt_score == pytest.approx(93.65, abs=0.01)
    assert info.verified is True
    assert info.socials == 2, "one website + twitter"
    assert info.categories == ["Pons Launchpad"]
    assert info.fetched_ts == 1234.0


def test_unknown_and_missing_fields_are_tolerated():
    doc = copy.deepcopy(PAYLOAD)
    a = doc["data"]["attributes"]
    a["is_honeypot"] = "unknown"
    a["holders"] = {"count": "88126", "distribution_percentage": {"top_10": 73.6}}
    a["developer_holding_percentage"] = "4.2"
    a["categories"] = None
    a["gt_score"] = "not a number"
    a["surprise_field"] = {"nested": [1, 2, 3]}
    del a["gt_verified"]
    info = parse_info(doc)
    assert info.holders == 88126
    assert info.top10_pct == pytest.approx(73.6)
    assert info.dev_pct == pytest.approx(4.2)
    assert info.honeypot == "unknown"
    assert info.gt_score is None
    assert info.verified is False
    assert info.categories == []

    bare = parse_info({"data": {"id": "x", "type": "token"}})
    assert bare is not None and bare.holders is None and bare.honeypot == "unknown"
    assert parse_info({"errors": [{"status": "404"}]}) is None
    assert parse_info(None) is None
    assert parse_info("junk") is None


@pytest.mark.parametrize("raw,expect", [(True, "yes"), (False, "no"), ("true", "yes"), ("no", "no"), (None, "unknown"), ("maybe", "unknown")])
def test_honeypot_values_fold_to_three_states(raw, expect):
    doc = copy.deepcopy(PAYLOAD)
    doc["data"]["attributes"]["is_honeypot"] = raw
    assert parse_info(doc).honeypot == expect


# ---------------- fetching + cache ----------------


def test_token_info_fetches_the_info_endpoint_through_the_gecko_limiter(monkeypatch):
    calls = fake_fetch(monkeypatch, PAYLOAD)
    info = asyncio.run(risk.token_info(PONS))
    assert info is not None and info.holders == 88739
    url, limiter = calls[0]
    assert url == f"{gecko.BASE}/networks/robinhood/tokens/{PONS}/info"
    assert limiter is gecko.gecko_limiter


def test_cache_hit_avoids_a_second_request(monkeypatch):
    calls = fake_fetch(monkeypatch, PAYLOAD)
    first = asyncio.run(risk.token_info(PONS))
    second = asyncio.run(risk.token_info(PONS.upper()))  # case must not split the cache
    assert first is second
    assert len(calls) == 1


def test_positive_cache_expires_after_the_configured_window(monkeypatch):
    calls = fake_fetch(monkeypatch, PAYLOAD)
    asyncio.run(risk.token_info(PONS))
    clock = [time.time() + risk.RHC_RISK_CACHE_SECONDS + 1]
    monkeypatch.setattr(risk.time, "time", lambda: clock[0])
    asyncio.run(risk.token_info(PONS))
    assert len(calls) == 2


def test_negative_cache_remembers_a_failure_for_a_minute(monkeypatch):
    calls = fake_fetch(monkeypatch, None)  # 429 / timeout flattened by get_json
    assert asyncio.run(risk.token_info(PONS)) is None
    assert asyncio.run(risk.token_info(PONS)) is None
    assert len(calls) == 1, "a failed lookup must not be retried immediately"

    clock = [time.time() + risk.NEGATIVE_CACHE_SECONDS + 1]
    monkeypatch.setattr(risk.time, "time", lambda: clock[0])
    assert asyncio.run(risk.token_info(PONS)) is None
    assert len(calls) == 2, "after the negative window it may try again"


def test_token_info_never_raises(monkeypatch):
    async def boom(url, limiter=None, **kw):
        raise RuntimeError("socket exploded")

    monkeypatch.setattr(risk, "get_json", boom)
    assert asyncio.run(risk.token_info(PONS)) is None
    assert asyncio.run(risk.token_info("")) is None


def test_limiter_nearly_empty_skips_the_request_and_serves_the_cache(monkeypatch):
    calls = fake_fetch(monkeypatch, PAYLOAD)
    cached = asyncio.run(risk.token_info(PONS))
    assert len(calls) == 1

    # Bucket almost dry: a cold address gets None with no request at all...
    monkeypatch.setattr(gecko.gecko_limiter, "tokens", 1.0)
    monkeypatch.setattr(gecko.gecko_limiter, "updated", time.monotonic())
    other = "0x" + "ab" * 20
    assert asyncio.run(risk.token_info(other)) is None
    assert len(calls) == 1

    # ...and a stale entry is served as is rather than refreshed.
    clock = [time.time() + risk.RHC_RISK_CACHE_SECONDS + 1]
    monkeypatch.setattr(risk.time, "time", lambda: clock[0])
    assert asyncio.run(risk.token_info(PONS)) is cached
    assert len(calls) == 1


def test_cache_is_capped_at_the_max_entries_oldest_first(monkeypatch):
    fake_fetch(monkeypatch, PAYLOAD)
    monkeypatch.setattr(risk, "CACHE_MAX_ENTRIES", 3)
    for i in range(4):
        asyncio.run(risk.token_info(f"0x{i:040x}"))
    assert len(risk._cache) == 3
    assert risk._key(f"0x{0:040x}", "robinhood") not in risk._cache
    assert risk._key(f"0x{3:040x}", "robinhood") in risk._cache


# ---------------- rendering ----------------


def test_risk_line_safe_text():
    info = TokenInfo(holders=8120, top10_pct=33.0, dev_pct=4.0, honeypot="no", gt_score=62.4)
    assert risk_line(info) == "Risk: 8,120 holders · top-10 **33%** · dev 4% · gt 62 · honeypot: no"


def test_risk_line_warns_on_concentration_dev_bag_or_honeypot():
    assert risk_line(TokenInfo(holders=8120, top10_pct=73.0, dev_pct=4.0, honeypot="no", gt_score=62.0)) == (
        "⚠️ Risk: 8,120 holders · top-10 **73%** · dev 4% · gt 62 · honeypot: no"
    )
    assert risk_line(TokenInfo(top10_pct=10.0, dev_pct=25.0)).startswith("⚠️ Risk: top-10 **10%** · dev 25%")
    assert risk_line(TokenInfo(holders=5, honeypot="yes")) == "⚠️ Risk: 5 holders · honeypot: yes"
    # At the threshold is not over it.
    assert not risk_line(TokenInfo(top10_pct=risk.TOP_HOLDER_WARN_PCT, dev_pct=20.0)).startswith("⚠️")


def test_risk_line_omits_unknown_parts():
    assert risk_line(None) == "Risk: —"
    assert risk_line(TokenInfo()) == "Risk: —"
    assert risk_line(TokenInfo(holders=1)) == "Risk: 1 holder"
    # PONS's top-10 share (59.8%) is over the default 50% warning line, so the
    # prefix is expected; the unknown dev share is simply left out.
    line = risk_line(parse_info(PAYLOAD))
    assert line == "⚠️ Risk: 88,739 holders · top-10 **59.8%** · gt 94 · honeypot: no"
    assert "dev" not in line


def test_is_honeypot_only_on_an_explicit_yes():
    assert is_honeypot(TokenInfo(honeypot="yes")) is True
    assert is_honeypot(TokenInfo(honeypot="no")) is False
    assert is_honeypot(TokenInfo(honeypot="unknown")) is False
    assert is_honeypot(None) is False


# ---------------- the links under a post ----------------


def test_a_projects_own_links_are_read_and_normalised():
    """GeckoTerminal stores a handle in one field and a full URL in another; a
    post should not care which, and should never render a bare @name as a link."""
    info = parse_info({"data": {"attributes": {
        "twitter_handle": "@ponsdotfamily",
        "telegram_handle": "https://t.me/ponschat/",
        "discord_url": "https://discord.gg/pons",
        "websites": ["https://ponsfamily.com", "https://backup.example"],
    }}})
    assert info.links == {
        "X": "https://x.com/ponsdotfamily",
        "Telegram": "https://t.me/ponschat/",     # a published URL is passed through as published
        "Discord": "https://discord.gg/pons",
        "Site": "https://ponsfamily.com",
    }
    assert list(info.links) == ["X", "Telegram", "Discord", "Site"], "a fixed order everywhere"
    assert info.socials == 4


def test_junk_link_fields_are_dropped_rather_than_rendered():
    info = parse_info({"data": {"attributes": {
        "twitter_handle": "   ", "telegram_handle": None, "discord_url": "not-a-url",
        "websites": ["ftp://nope", 7],
    }}})
    assert info.links == {}


def test_links_line_always_offers_the_chart_gmgn_and_the_explorer(monkeypatch):
    monkeypatch.setattr(risk, "RHC_GMGN_SLUG", "robinhood")
    info = parse_info({"data": {"attributes": {"twitter_handle": "pons"}}})
    line = risk.links_line(info, PONS)
    assert line.startswith("[X](https://x.com/pons)")
    assert f"[Chart](https://www.geckoterminal.com/{risk.RHCHAIN_NETWORK}/tokens/{PONS})" in line
    assert f"[GMGN](https://gmgn.ai/robinhood/token/{PONS})" in line
    assert f"/token/{PONS})" in line and "[Explorer](" in line


def test_the_gmgn_link_can_be_switched_off_without_a_deploy(monkeypatch):
    """The chain's slug on GMGN cannot be verified from here — their site
    answers 200 for any slug — so it is one env var, not a hard-coded URL."""
    monkeypatch.setattr(risk, "RHC_GMGN_SLUG", "")
    line = risk.links_line(None, PONS)
    assert "GMGN" not in line and "[Chart](" in line and "[Explorer](" in line


def test_no_address_means_no_line_at_all():
    assert risk.links_line(None, "") == ""
    assert risk.links_line(parse_info({"data": {"attributes": {"twitter_handle": "x"}}}), "") == "[X](https://x.com/x)"
