"""KyberSwap client parsing and the pre-signing guard."""

import pytest

from mccapbot.config import RHC_KYBER_ROUTER
from mccapbot.rhc import chain, guard, kyber

PONS = "0x39dbed3a2bd333467115de45665cc57f813c4571"
PHANTOM = "0x000000000000000000000000000000000000dEaD"


def route_body(amount_out="36000000000000000000", router=RHC_KYBER_ROUTER, token_in=None):
    return {
        "code": 0, "message": "successfully",
        "data": {
            "routeSummary": {
                "tokenIn": (token_in or chain.NATIVE).lower(), "amountIn": "10000000000000000", "amountInUsd": "24.8",
                "tokenOut": PONS, "amountOut": amount_out, "amountOutUsd": "24.7",
                "gas": "655171", "gasPrice": "302816000", "gasUsd": "0.49",
                "route": [[{"exchange": "uniswap-v4-fee", "poolType": "uniswap-v4"}, {"exchange": "ramses-v3"}]],
                "routeID": "r", "checksum": "c", "timestamp": 1,
            },
            "routerAddress": router,
        },
    }


def build_body(router=RHC_KYBER_ROUTER, amount_out="36100000000000000000", value="10000000000000000",
               amount_in="10000000000000000"):
    return {"code": 0, "message": "successfully", "data": {
        "routerAddress": router, "data": "0xe21fd0e9" + "00" * 40, "transactionValue": value,
        "amountIn": amount_in, "amountOut": amount_out, "amountInUsd": "24.8", "amountOutUsd": "24.75",
        "gas": "655171", "gasUsd": "0.49",
    }}


@pytest.fixture(autouse=True)
def reset_guard():
    guard.reset_router_verification()
    yield
    guard.reset_router_verification()


def fake_get(body):
    async def _get(url):
        fake_get.urls.append(url)
        return body
    fake_get.urls = []
    return _get


# ---------------- kyber ----------------


@pytest.mark.asyncio
async def test_route_parses_amounts_usd_gas_and_hops(monkeypatch):
    g = fake_get(route_body())
    monkeypatch.setattr(kyber, "_get", g)
    rt = await kyber.route(chain.NATIVE, PONS, 10**16)
    assert "tokenIn=" in fake_get.urls[0] and "amountIn=10000000000000000" in fake_get.urls[0]
    assert rt.amount_out == 36 * 10**18 and rt.amount_in_usd == pytest.approx(24.8)
    assert rt.hops == ["uniswap-v4-fee", "ramses-v3"] and rt.gas == 655171
    assert rt.price_impact_pct == pytest.approx((24.7 / 24.8 - 1) * 100)
    assert not rt.is_stale()


@pytest.mark.asyncio
async def test_no_route_is_distinct_from_kyber_being_down(monkeypatch):
    monkeypatch.setattr(kyber, "_get", fake_get({"code": 4008, "message": "no route found", "data": {}}))
    with pytest.raises(kyber.NoRoute):
        await kyber.route(chain.NATIVE, PONS, 10**16)

    async def down(url):
        raise kyber.KyberUnavailable("rate limited")
    monkeypatch.setattr(kyber, "_get", down)
    with pytest.raises(kyber.KyberUnavailable):
        await kyber.route(chain.NATIVE, PONS, 10**16)
    with pytest.raises(kyber.KyberError):
        await kyber.route(chain.NATIVE, PONS, 0)


@pytest.mark.asyncio
async def test_get_maps_http_status_to_the_right_error(monkeypatch):
    class Resp:
        def __init__(self, status, body):
            self.status, self._body = status, body

        async def json(self, content_type=None):
            if isinstance(self._body, Exception):
                raise self._body
            return self._body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Session:
        def __init__(self, status, body):
            self.status, self.body = status, body

        def get(self, url, headers=None):
            assert headers["X-Client-Id"]
            return Resp(self.status, self.body)

    async def session_for(status, body):
        async def get_session():
            return Session(status, body)
        return get_session

    monkeypatch.setattr(kyber, "get_session", await session_for(429, {"message": "slow down"}))
    with pytest.raises(kyber.KyberUnavailable, match="rate-limit"):
        await kyber._get("u")
    monkeypatch.setattr(kyber, "get_session", await session_for(503, None))
    with pytest.raises(kyber.KyberUnavailable, match="503"):
        await kyber._get("u")
    monkeypatch.setattr(kyber, "get_session", await session_for(400, {"message": "invalid token"}))
    with pytest.raises(kyber.KyberError, match="invalid token"):
        await kyber._get("u")
    monkeypatch.setattr(kyber, "get_session", await session_for(200, {"code": 0, "data": {}}))
    assert await kyber._get("u") == {"code": 0, "data": {}}


@pytest.mark.asyncio
async def test_build_pins_the_router(monkeypatch):
    monkeypatch.setattr(kyber, "_get", fake_get(route_body()))
    rt = await kyber.route(chain.NATIVE, PONS, 10**16)

    async def wrong_router(url, payload, **kw):
        return 200, build_body(router="0xE592427A0AEce92De3Edee1F18E0157C05861564")
    monkeypatch.setattr(kyber, "post_json", wrong_router)
    with pytest.raises(kyber.KyberError, match="expected"):
        await kyber.build(rt, PHANTOM, 200)

    async def good(url, payload, **kw):
        assert payload["sender"] == payload["recipient"] == chain.to_checksum(PHANTOM)
        assert payload["slippageTolerance"] == 200 and payload["routeSummary"] is rt.summary
        return 200, build_body()
    monkeypatch.setattr(kyber, "post_json", good)
    built = await kyber.build(rt, PHANTOM, 200)
    assert built.router == RHC_KYBER_ROUTER and built.is_buy
    assert built.value == 10**16 and built.amount_out == 361 * 10**17
    assert built.min_out == kyber.min_out(361 * 10**17, 200) == 361 * 10**17 * 9800 // 10000


@pytest.mark.asyncio
async def test_build_refuses_a_transaction_that_spends_more_than_quoted(monkeypatch):
    """The value field is ETH leaving the wallet; nothing in a response may raise it."""
    monkeypatch.setattr(kyber, "_get", fake_get(route_body()))
    rt = await kyber.route(chain.NATIVE, PONS, 10**16)

    async def inflated_value(url, payload, **kw):
        return 200, build_body(value="20000000000000000")
    monkeypatch.setattr(kyber, "post_json", inflated_value)
    with pytest.raises(kyber.KyberError, match="does not match the quote"):
        await kyber.build(rt, PHANTOM, 200)

    async def inflated_amount(url, payload, **kw):
        return 200, build_body(amount_in="20000000000000000")
    monkeypatch.setattr(kyber, "post_json", inflated_amount)
    with pytest.raises(kyber.KyberError, match="does not match the quote"):
        await kyber.build(rt, PHANTOM, 200)

    # A sell must carry zero value.
    monkeypatch.setattr(kyber, "_get", fake_get(route_body(token_in=PONS)))
    sell = await kyber.route(PONS, chain.NATIVE, 10**16)

    async def sell_with_value(url, payload, **kw):
        return 200, build_body(value="1")
    monkeypatch.setattr(kyber, "post_json", sell_with_value)
    with pytest.raises(kyber.KyberError, match="does not match the quote"):
        await kyber.build(sell, PHANTOM, 200)


@pytest.mark.asyncio
async def test_build_refuses_stale_routes_and_maps_http_failures(monkeypatch):
    monkeypatch.setattr(kyber, "_get", fake_get(route_body()))
    rt = await kyber.route(chain.NATIVE, PONS, 10**16)
    rt.fetched_ts -= 60
    with pytest.raises(kyber.KyberError, match="stale"):
        await kyber.build(rt, PHANTOM, 200)

    rt2 = await kyber.route(chain.NATIVE, PONS, 10**16)

    async def forbidden(url, payload, **kw):
        return 403, None
    monkeypatch.setattr(kyber, "post_json", forbidden)
    with pytest.raises(kyber.KyberError, match="HTTP 403"):
        await kyber.build(rt2, PHANTOM, 200)

    async def down(url, payload, **kw):
        return 0, None
    monkeypatch.setattr(kyber, "post_json", down)
    with pytest.raises(kyber.KyberUnavailable):
        await kyber.build(rt2, PHANTOM, 200)


def test_min_out_math():
    assert kyber.min_out(10_000, 200) == 9_800
    assert kyber.min_out(10_000, 0) == 10_000
    assert kyber.min_out(1, 200) == 0


@pytest.mark.asyncio
async def test_non_route_api_errors_are_not_read_as_honeypots(monkeypatch):
    monkeypatch.setattr(kyber, "_get", fake_get({"code": 4221, "message": "invalid parameter", "data": {}}))
    with pytest.raises(kyber.KyberError) as exc:
        await kyber.route(chain.NATIVE, PONS, 10**16)
    assert not isinstance(exc.value, kyber.NoRoute)
    monkeypatch.setattr(kyber, "_get", fake_get({"code": 4008, "message": "route not found", "data": {}}))
    with pytest.raises(kyber.NoRoute):
        await kyber.route(chain.NATIVE, PONS, 10**16)


def test_route_goes_stale_after_ten_seconds():
    rt = kyber.Route(token_in=chain.NATIVE, token_out=PONS, amount_in=1, amount_out=1, amount_in_usd=1,
                     amount_out_usd=1, gas=1, gas_usd=0, router="", summary={})
    assert not rt.is_stale(rt.fetched_ts + 5)
    assert rt.is_stale(rt.fetched_ts + 12)


# ---------------- guard ----------------


def test_denylist_and_pin_are_enforced():
    with pytest.raises(guard.GuardError, match="drainer"):
        guard.assert_not_denied("0xE592427A0AEce92De3Edee1F18E0157C05861564")
    guard.assert_not_denied(RHC_KYBER_ROUTER)
    with pytest.raises(guard.GuardError, match="pinned"):
        guard.assert_pinned_router("0xcaf681a66d020601342297493863e78c959e5cb2")
    guard.assert_pinned_router(RHC_KYBER_ROUTER.lower())


def test_decode_refuses_anything_that_is_not_a_swap_result():
    with pytest.raises(guard.GuardError, match="drainer"):
        guard.decode_swap_result(b"")
    with pytest.raises(guard.GuardError):
        guard.decode_swap_result(b"\x00" * 31)
    assert guard.decode_swap_result((12345).to_bytes(32, "big") + (7).to_bytes(32, "big")) == 12345


def make_built(min_out=35 * 10**18, router=RHC_KYBER_ROUTER):
    rt = kyber.Route(token_in=chain.NATIVE, token_out=PONS, amount_in=10**16, amount_out=36 * 10**18,
                     amount_in_usd=24.8, amount_out_usd=24.7, gas=1, gas_usd=0.5, router=router, summary={})
    return kyber.BuiltSwap(router=router, data="0xe21fd0e9", value=10**16, amount_in=10**16, amount_out=36 * 10**18,
                           amount_in_usd=24.8, amount_out_usd=24.7, gas=1, gas_usd=0.5, slippage_bps=200,
                           min_out=min_out, route=rt)


@pytest.mark.asyncio
async def test_simulate_returns_the_output_and_funds_the_phantom(monkeypatch):
    seen = {}

    async def fake_call(tx, overrides=None):
        seen["tx"], seen["overrides"] = tx, overrides
        return (36 * 10**18).to_bytes(32, "big") + (5).to_bytes(32, "big")
    monkeypatch.setattr(chain, "call", fake_call)
    got = await guard.simulate(make_built(), PHANTOM, fund_sender=True)
    assert got == 36 * 10**18
    assert seen["tx"]["to"] == RHC_KYBER_ROUTER and seen["tx"]["value"] == 10**16
    assert seen["overrides"][chain.to_checksum(PHANTOM)]["balance"] > 10**16
    await guard.simulate(make_built(), PHANTOM)
    assert seen["overrides"] is None


@pytest.mark.asyncio
async def test_simulate_refuses_below_floor_empty_result_reverts_and_blindness(monkeypatch):
    async def low(tx, overrides=None):
        return (30 * 10**18).to_bytes(32, "big") + (5).to_bytes(32, "big")
    monkeypatch.setattr(chain, "call", low)
    with pytest.raises(guard.GuardError, match="slippage floor"):
        await guard.simulate(make_built(), PHANTOM)

    async def empty(tx, overrides=None):
        return b""
    monkeypatch.setattr(chain, "call", empty)
    with pytest.raises(guard.GuardError, match="drainer"):
        await guard.simulate(make_built(), PHANTOM)

    async def revert(tx, overrides=None):
        raise chain.RevertError("execution reverted: Return amount is not enough")
    monkeypatch.setattr(chain, "call", revert)
    with pytest.raises(guard.GuardError, match="slippage limit"):
        await guard.simulate(make_built(), PHANTOM)

    async def other_revert(tx, overrides=None):
        raise chain.RevertError("execution reverted: 0x08c379a" + "0" * 120)
    monkeypatch.setattr(chain, "call", other_revert)
    with pytest.raises(guard.GuardError, match="would revert") as exc:
        await guard.simulate(make_built(), PHANTOM)
    assert "0x…" in str(exc.value) and "Nothing was spent" in str(exc.value)

    async def blind(tx, overrides=None):
        raise chain.RpcUnavailable("all RPCs failed")
    monkeypatch.setattr(chain, "call", blind)
    with pytest.raises(guard.GuardError, match="blind"):
        await guard.simulate(make_built(), PHANTOM)

    with pytest.raises(guard.GuardError, match="pinned"):
        await guard.simulate(make_built(router="0xcaf681a66d020601342297493863e78c959e5cb2"), PHANTOM)


@pytest.mark.asyncio
async def test_verify_router_caches_success_but_not_failure(monkeypatch):
    calls = []

    async def small(addr):
        calls.append(addr)
        return 2109
    monkeypatch.setattr(chain, "code_size", small)
    with pytest.raises(guard.GuardError, match="bytes of code"):
        await guard.verify_router()
    with pytest.raises(guard.GuardError):
        await guard.verify_router()
    assert len(calls) == 2, "a failure must be re-checked next time"

    async def big(addr):
        calls.append(addr)
        return 13724
    monkeypatch.setattr(chain, "code_size", big)
    await guard.verify_router()
    await guard.verify_router()
    assert len(calls) == 3, "a pass is cached"
