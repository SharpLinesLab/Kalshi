import os

import pytest

from btc15 import model
from btc15.caller import State
from btc15.config import Settings
from btc15.kalshi import KalshiError, Market, fp

ENV_KEYS = ["MODE", "KALSHI_KEY_ID", "KALSHI_PRIVATE_KEY_PATH", "DASHBOARD_PASSWORD", "LIVE_CONFIRM", "ALLOW_UNPROVEN",
            "DAILY_LOSS_LIMIT_USD", "MAX_TOTAL_LOSS_USD", "MAX_CONTRACTS_PER_TRADE", "PAPER_BANKROLL", "DB_PATH", "KILL_FILE"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


def make_cfg(**kw) -> Settings:
    kw.setdefault("MAX_CONTRACTS_PER_TRADE", 5)
    return Settings(_env_file=None, **kw)


def base_state(**kw) -> State:
    """A state that passes every gate and yields BUY_UP (verified in test_gates)."""
    d = dict(now=1300.0, ticker="KXBTC15M-26OCT031200-00", strike=100000.0, open_ts=1000.0, close_ts=1900.0,
             yes_bid=0.64, yes_ask=0.66, no_bid=0.34, no_ask=0.36, quote_ts=1299.5, composite=100250.0,
             ref_ts=1299.8, n_fresh_feeds=4, basis=0.0, realized_vol=0.5, gap_pct=0.0, bankroll=1000.0,
             daily_pnl=0.0, total_pnl=0.0, open_exposure=0.0, has_position_or_order=False, halted=None,
             proof_status="PROVEN", mode="PAPER")
    d.update(kw)
    return State(**d)


class FakeKalshi:
    """In-memory stand-in for KalshiClient (same async surface the executor/engine use)."""

    def __init__(self, fill="full", balance=1000.0, market=None):
        self.fill_mode = fill
        self.balance = balance
        self.orders: dict[str, dict] = {}
        self.fills: dict[str, list] = {}
        self.positions: dict[str, float] = {}
        self.cancelled: list[str] = []
        self.placed: list[dict] = []
        self.consecutive_errors = 0
        self.skew_position = 0.0          # inject a ledger/API mismatch
        self.fail_place: KalshiError | None = None
        self.hide_orders = False          # simulate "order never reached the exchange"
        self.market = market
        self.n = 0

    async def aclose(self): ...

    async def open_markets(self):
        return [self.market] if self.market else []

    async def get_market(self, ticker):
        return self.market

    async def get_balance(self):
        return self.balance

    async def get_positions(self, ticker=None):
        pos = {k: v for k, v in self.positions.items() if v != 0 and (ticker is None or k == ticker)}
        if self.skew_position and ticker and self.orders:       # drift appears only after we traded
            pos[ticker] = pos.get(ticker, 0.0) + self.skew_position
        return pos

    async def list_orders(self, ticker=None, status=None):
        if self.hide_orders:
            return []
        return [o for o in self.orders.values() if (ticker is None or o["ticker"] == ticker) and (status is None or o["status"] == status)]

    async def place_order(self, ticker, book_side, count, price, client_order_id=None):
        self.placed.append({"ticker": ticker, "book_side": book_side, "count": count, "price": price})
        if self.fail_place:
            if not self.fail_place.definite:                       # ambiguous: the order may have landed
                self._create(ticker, book_side, count, price, client_order_id)
            raise self.fail_place
        o = self._create(ticker, book_side, count, price, client_order_id)
        return {"order_id": o["order_id"], "fill_count": o["fill_count_fp"], "remaining_count": o["remaining_count_fp"]}

    def _create(self, ticker, book_side, count, price, cid):
        self.n += 1
        oid = f"ord{self.n}"
        n_fill = {"full": count, "none": 0, "partial": count // 2}[self.fill_mode]
        side_px = price if book_side == "bid" else round(1 - price, 4)       # price paid for the side we hold
        o = {"order_id": oid, "client_order_id": cid, "ticker": ticker,
             "status": "executed" if n_fill == count else "resting",
             "fill_count_fp": f"{n_fill}.00", "remaining_count_fp": f"{count - n_fill}.00",
             "book_side": book_side, "side_px": side_px}
        self.orders[oid] = o
        if n_fill:
            fee = model.fee(side_px, n_fill)
            key = "yes_price_dollars" if book_side == "bid" else "no_price_dollars"
            self.fills[oid] = [{"count_fp": f"{n_fill}.00", key: f"{side_px:.4f}", "fee_cost": f"{fee:.4f}"}]
            self.positions[ticker] = self.positions.get(ticker, 0.0) + (n_fill if book_side == "bid" else -n_fill)
            self.balance -= n_fill * side_px + fee
        return o

    async def get_order(self, oid):
        return dict(self.orders[oid])

    async def cancel_order(self, oid, ticker):
        self.orders[oid]["status"] = "canceled"
        self.orders[oid]["remaining_count_fp"] = "0.00"
        self.cancelled.append(oid)
        return {"order_id": oid}

    async def get_fills(self, oid):
        return list(self.fills.get(oid, []))


def make_market(now=1300.0, **kw) -> Market:
    d = dict(ticker="KXBTC15M-26OCT031200-00", status="active", open_ts=now - 300, close_ts=now + 600, strike=100000.0,
             yes_bid=0.64, yes_ask=0.66, no_bid=0.34, no_ask=0.36,
             price_ranges=[{"start": "0.0000", "end": "0.1000", "step": "0.0010"},
                           {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
                           {"start": "0.9000", "end": "1.0000", "step": "0.0010"}],
             result="", expiration_value=None, recv_ts=now)
    d.update(kw)
    return Market(**d)
