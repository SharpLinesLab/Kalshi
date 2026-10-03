"""Kalshi REST client: RSA-PSS / Ed25519 signing, market data, orders, portfolio.

Facts and doc links live in spec.py. Prices/counts are parsed as Decimal -> float only at the edge.
The private key is loaded once, never logged, and never exposed via repr().
"""
from __future__ import annotations

import base64
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import spec


class KalshiError(Exception):
    def __init__(self, msg: str, status: Optional[int] = None, definite: bool = True):
        super().__init__(msg)
        self.status = status
        self.definite = definite      # False = outcome unknown (timeout / connection reset)


class Signer:
    """Signs requests. Holds the key privately; repr() reveals nothing."""

    def __init__(self, key_id: str, private_key_path: str):
        with open(private_key_path, "rb") as fh:
            self._key = serialization.load_pem_private_key(fh.read(), password=None)
        self._key_id = key_id

    def __repr__(self) -> str:      # never leak key material through logs / tracebacks
        return "<Signer redacted>"

    def sign(self, ts_ms: str, method: str, path: str) -> str:
        msg = f"{ts_ms}{method}{path.split('?')[0]}".encode()
        if isinstance(self._key, Ed25519PrivateKey):
            sig = self._key.sign(msg)
        else:
            sig = self._key.sign(
                msg,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256(),
            )
        return base64.b64encode(sig).decode()

    def headers(self, method: str, path: str) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        return {spec.HDR_KEY: self._key_id, spec.HDR_TS: ts, spec.HDR_SIG: self.sign(ts, method, path)}


def D(x: Any) -> Decimal:
    return Decimal(str(x))


def fp(x: Any) -> float:
    return float(D(x)) if x not in (None, "") else 0.0


def fmt_price(p: float) -> str:
    return f"{D(round(p, 4)):.4f}"


def fmt_count(c: int) -> str:
    return f"{c}.00"


def on_grid(price: float, price_ranges: list[dict]) -> bool:
    """A price is valid only if it lies on the market's tick grid (price_ranges is the source of truth)."""
    pr = D(round(price, 4))
    for band in price_ranges or []:
        lo, hi, step = D(band["start"]), D(band["end"]), D(band["step"])
        if lo <= pr <= hi and ((pr - lo) % step) == 0:
            return True
    return False


def derive_asks(orderbook_fp: dict) -> dict[str, Optional[float]]:
    """Orderbook lists BIDS only, ascending (best = last). yes_ask = 1 - best_no_bid, no_ask = 1 - best_yes_bid."""
    yb = orderbook_fp.get("yes_dollars") or []
    nb = orderbook_fp.get("no_dollars") or []
    best_yes = D(yb[-1][0]) if yb else None
    best_no = D(nb[-1][0]) if nb else None
    return {
        "yes_bid": float(best_yes) if best_yes is not None else None,
        "no_bid": float(best_no) if best_no is not None else None,
        "yes_ask": float(Decimal(1) - best_no) if best_no is not None else None,
        "no_ask": float(Decimal(1) - best_yes) if best_yes is not None else None,
    }


@dataclass
class Market:
    ticker: str
    status: str
    open_ts: float
    close_ts: float
    strike: Optional[float]
    yes_bid: Optional[float]
    yes_ask: Optional[float]
    no_bid: Optional[float]
    no_ask: Optional[float]
    price_ranges: list
    result: str
    expiration_value: Optional[float]
    recv_ts: float
    exchange_index: Optional[int] = None      # shard that matches this market (Crypto = 2); collateral must be there


def _ts(iso: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _px(m: dict, key: str) -> Optional[float]:
    v = m.get(key)
    return None if v in (None, "") else float(D(v))


def parse_market(m: dict, recv_ts: float) -> Market:
    ev = m.get("expiration_value")
    return Market(
        ticker=m["ticker"], status=m.get("status", ""), open_ts=_ts(m["open_time"]), close_ts=_ts(m["close_time"]),
        strike=m.get("floor_strike"),      # JSON double, kept exactly as published (no rounding)
        yes_bid=_px(m, "yes_bid_dollars"), yes_ask=_px(m, "yes_ask_dollars"),
        no_bid=_px(m, "no_bid_dollars"), no_ask=_px(m, "no_ask_dollars"),
        price_ranges=m.get("price_ranges") or [], result=m.get("result") or "",
        expiration_value=float(D(ev)) if ev not in (None, "") else None, recv_ts=recv_ts,
        exchange_index=m.get("exchange_index"),
    )


class KalshiClient:
    def __init__(self, base_url: str, signer: Optional[Signer] = None, timeout: float = 8.0,
                 http: Optional[httpx.AsyncClient] = None):
        self.base = base_url.rstrip("/")
        self.signer = signer
        self.http = http or httpx.AsyncClient(timeout=timeout)
        self.consecutive_errors = 0
        self.total_errors = 0

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _request(self, method: str, path: str, *, params: Optional[dict] = None,
                       body: Optional[dict] = None, auth: bool = True) -> dict:
        full_path = spec.API_PATH_PREFIX + path           # signed path: from root, no host, no query
        headers = {"Content-Type": "application/json"}
        if auth:
            if self.signer is None:
                raise KalshiError("authenticated call without credentials")
            headers.update(self.signer.headers(method, full_path))
        try:
            r = await self.http.request(method, self.base + path, params=params, json=body, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            self._err()
            raise KalshiError(f"transport error: {type(e).__name__}", None, definite=False) from None
        if r.status_code >= 400:
            self._err()
            # 5xx on a write may have been applied; 4xx is a definite rejection.
            raise KalshiError(f"HTTP {r.status_code} {method} {path}: {r.text[:300]}", r.status_code,
                              definite=r.status_code < 500)
        self.consecutive_errors = 0
        return r.json() if r.content else {}

    def _err(self) -> None:
        self.consecutive_errors += 1
        self.total_errors += 1

    # ---------------- public market data
    async def get_market(self, ticker: str) -> Market:
        d = await self._request("GET", f"/markets/{ticker}", auth=False)
        return parse_market(d["market"], time.time())

    async def open_markets(self, series: str = spec.SERIES) -> list[Market]:
        d = await self._request("GET", "/markets", params={"series_ticker": series, "status": "open", "limit": 20}, auth=False)
        now = time.time()
        return [parse_market(m, now) for m in d.get("markets", [])]

    async def get_orderbook(self, ticker: str) -> dict:
        d = await self._request("GET", f"/markets/{ticker}/orderbook", auth=False)
        return d["orderbook_fp"]

    # ---------------- portfolio (authenticated)
    async def get_balance(self, exchange_index: Optional[int] = None) -> float:
        """Available balance in dollars. Orders are collateralised PER EXCHANGE SHARD, so pass the market's
        exchange_index to get what can actually fund an order (the default total spans all shards)."""
        d = await self._request("GET", "/portfolio/balance")
        if exchange_index is not None:
            for row in d.get("balance_breakdown") or []:
                if row.get("exchange_index") == exchange_index:
                    return float(D(row["balance"]))
        if d.get("balance_dollars") not in (None, ""):
            return float(D(d["balance_dollars"]))
        return d["balance"] / 100.0

    async def get_positions(self, ticker: Optional[str] = None) -> dict[str, float]:
        """ticker -> signed contracts (positive = YES, negative = NO). UNVERIFIED sign convention: DEMO test."""
        params = {"limit": 200}
        if ticker:
            params["ticker"] = ticker
        d = await self._request("GET", "/portfolio/positions", params=params)
        return {p["ticker"]: fp(p.get("position_fp")) for p in d.get("market_positions", []) if fp(p.get("position_fp")) != 0}

    async def get_order(self, order_id: str) -> dict:
        return (await self._request("GET", f"/portfolio/orders/{order_id}"))["order"]

    async def list_orders(self, ticker: Optional[str] = None, status: Optional[str] = None) -> list[dict]:
        params: dict = {"limit": 200}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        return (await self._request("GET", "/portfolio/orders", params=params)).get("orders", [])

    async def get_fills(self, order_id: str) -> list[dict]:
        return (await self._request("GET", "/portfolio/fills", params={"order_id": order_id, "limit": 200})).get("fills", [])

    async def place_order(self, ticker: str, book_side: str, count: int, price: float,
                          client_order_id: Optional[str] = None, exchange_index: Optional[int] = None) -> dict:
        """V2 order, YES-book only: 'bid' = buy YES, 'ask' = sell YES (== buy NO at 1-price). Always a limit GTC order."""
        body = {
            "ticker": ticker, "client_order_id": client_order_id or str(uuid.uuid4()),
            "side": book_side, "count": fmt_count(count), "price": fmt_price(price),
            "time_in_force": "good_till_canceled", "self_trade_prevention_type": "taker_at_cross",
            "post_only": False, "reduce_only": False,
        }
        if exchange_index is not None:                      # explicit shard; omitted => auto-route by ticker
            body["exchange_index"] = exchange_index
        return await self._request("POST", "/portfolio/events/orders", body=body)

    async def cancel_order(self, order_id: str, ticker: str) -> dict:
        return await self._request("DELETE", f"/portfolio/events/orders/{order_id}", params={"market_ticker": ticker})
