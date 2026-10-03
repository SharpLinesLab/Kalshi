import base64
import json

import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from btc15 import spec
from btc15.kalshi import (KalshiClient, KalshiError, Signer, derive_asks, fmt_count, fmt_price, on_grid, parse_market)

MARKET_JSON = {
    "ticker": "KXBTC15M-26OCT030900-00", "status": "active", "open_time": "2026-10-03T12:45:00Z",
    "close_time": "2026-10-03T13:00:00Z", "floor_strike": 84802.16, "yes_bid_dollars": "0.7000",
    "yes_ask_dollars": "0.7100", "no_bid_dollars": "0.2900", "no_ask_dollars": "0.3000", "result": "",
    "expiration_value": "", "price_ranges": [{"start": "0.0000", "end": "0.1000", "step": "0.0010"},
                                             {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
                                             {"start": "0.9000", "end": "1.0000", "step": "0.0010"}],
}


def write_key(tmp_path, key):
    p = tmp_path / "k.pem"
    p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return str(p)


def test_rsa_pss_signature_verifies_and_excludes_query_string(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    s = Signer("kid", write_key(tmp_path, key))
    sig = s.sign("1703123456789", "GET", "/trade-api/v2/portfolio/orders?limit=5")
    msg = b"1703123456789GET/trade-api/v2/portfolio/orders"                   # path only, no query, no host
    key.public_key().verify(base64.b64decode(sig), msg,
                            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    with pytest.raises(InvalidSignature):
        key.public_key().verify(base64.b64decode(sig), msg + b"x",
                                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())


def test_ed25519_signature_verifies(tmp_path):
    key = Ed25519PrivateKey.generate()
    s = Signer("kid", write_key(tmp_path, key))
    sig = s.sign("1", "POST", "/trade-api/v2/portfolio/events/orders")
    key.public_key().verify(base64.b64decode(sig), b"1POST/trade-api/v2/portfolio/events/orders")


def test_headers_and_secret_hygiene(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    s = Signer("my-key-id", write_key(tmp_path, key))
    h = s.headers("GET", "/trade-api/v2/portfolio/balance")
    assert set(h) == {spec.HDR_KEY, spec.HDR_TS, spec.HDR_SIG} and h[spec.HDR_KEY] == "my-key-id"
    assert h[spec.HDR_TS].isdigit() and len(h[spec.HDR_TS]) == 13            # milliseconds
    assert "PRIVATE" not in repr(s) and "PRIVATE" not in str(s.__dict__.keys())
    assert repr(s) == "<Signer redacted>"


def test_orderbook_asks_are_derived_from_opposite_bids():
    ob = {"yes_dollars": [["0.4100", "10.00"], ["0.4200", "13.00"]], "no_dollars": [["0.5500", "1.00"], ["0.5600", "17.00"]]}
    a = derive_asks(ob)
    assert a == {"yes_bid": 0.42, "no_bid": 0.56, "yes_ask": pytest.approx(0.44), "no_ask": pytest.approx(0.58)}
    assert derive_asks({"yes_dollars": [], "no_dollars": []})["yes_ask"] is None


def test_parse_market_keeps_exact_strike_and_decimal_prices():
    m = parse_market(MARKET_JSON, 123.0)
    assert m.strike == 84802.16 and m.yes_ask == 0.71 and m.no_ask == 0.30
    assert m.close_ts - m.open_ts == 900 and m.recv_ts == 123.0 and m.result == ""
    j = dict(MARKET_JSON, floor_strike=None)
    assert parse_market(j, 0).strike is None


def test_price_formatting_and_tick_grid():
    assert fmt_price(0.66) == "0.6600" and fmt_price(0.7) == "0.7000" and fmt_count(5) == "5.00"
    pr = MARKET_JSON["price_ranges"]
    assert on_grid(0.66, pr) and on_grid(0.075, pr) and on_grid(0.0755, pr) is False
    assert on_grid(0.665, pr) is False and on_grid(0.9, pr) and on_grid(0.905, pr) and on_grid(0.9055, pr) is False


def client_with(handler, tmp_path=None):
    signer = None
    if tmp_path is not None:
        signer = Signer("kid", write_key(tmp_path, rsa.generate_private_key(public_exponent=65537, key_size=2048)))
    return KalshiClient("https://example.test/trade-api/v2", signer, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_place_order_sends_v2_limit_gtc_body_and_signed_headers(tmp_path):
    seen = {}

    def handler(req: httpx.Request):
        seen["req"] = req
        return httpx.Response(200, json={"order_id": "o1", "fill_count": "0.00", "remaining_count": "5.00", "ts_ms": 1})

    c = client_with(handler, tmp_path)
    await c.place_order("KXBTC15M-26OCT030900-00", "bid", 5, 0.66, "cid-1")
    r = seen["req"]
    assert r.method == "POST" and r.url.path == "/trade-api/v2/portfolio/events/orders"
    body = json.loads(r.content)
    assert body == {"ticker": "KXBTC15M-26OCT030900-00", "client_order_id": "cid-1", "side": "bid", "count": "5.00",
                    "price": "0.6600", "time_in_force": "good_till_canceled", "self_trade_prevention_type": "taker_at_cross",
                    "post_only": False, "reduce_only": False}
    assert all(h in r.headers for h in (spec.HDR_KEY, spec.HDR_TS, spec.HDR_SIG))
    assert "market" not in json.dumps(body).lower().replace("ticker", "")          # never a market order


async def test_cancel_uses_v2_delete_with_market_ticker(tmp_path):
    seen = {}

    def handler(req):
        seen["req"] = req
        return httpx.Response(200, json={"order_id": "o1", "reduced_by": "5.00", "ts_ms": 1})

    await client_with(handler, tmp_path).cancel_order("o1", "KXBTC15M-X")
    r = seen["req"]
    assert r.method == "DELETE" and r.url.path == "/trade-api/v2/portfolio/events/orders/o1"
    assert r.url.params["market_ticker"] == "KXBTC15M-X"


async def test_public_market_data_sends_no_auth_headers():
    seen = {}

    def handler(req):
        seen["h"] = dict(req.headers)
        return httpx.Response(200, json={"market": MARKET_JSON})

    m = await client_with(handler).get_market("KXBTC15M-26OCT030900-00")
    assert m.strike == 84802.16 and spec.HDR_SIG.lower() not in seen["h"]


async def test_authenticated_call_without_credentials_is_refused():
    with pytest.raises(KalshiError):
        await client_with(lambda r: httpx.Response(200, json={})).get_balance()


async def test_error_accounting_and_definiteness():
    codes = iter([500, 400, 200])

    def handler(req):
        c = next(codes)
        return httpx.Response(c, json={"market": MARKET_JSON} if c == 200 else {"error": "x"})

    c = client_with(handler)
    with pytest.raises(KalshiError) as e1:
        await c.get_market("T")
    assert e1.value.definite is False and c.consecutive_errors == 1       # 5xx: outcome unknown
    with pytest.raises(KalshiError) as e2:
        await c.get_market("T")
    assert e2.value.definite is True and c.consecutive_errors == 2
    await c.get_market("T")
    assert c.consecutive_errors == 0                                      # success resets the streak


async def test_transport_timeout_is_an_indefinite_error():
    def handler(req):
        raise httpx.ReadTimeout("slow")

    c = client_with(handler)
    with pytest.raises(KalshiError) as e:
        await c.get_market("T")
    assert e.value.definite is False and c.consecutive_errors == 1


def test_spec_constants_are_the_documented_values():
    assert spec.REST_BASE["PROD"] == "https://external-api.kalshi.com/trade-api/v2"
    assert spec.REST_BASE["DEMO"] == "https://external-api.demo.kalshi.co/trade-api/v2"
    assert spec.SERIES == "KXBTC15M" and spec.SETTLE_WINDOW_SEC_DEFAULT == 60
