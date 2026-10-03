"""Order execution + reconciliation. Limit orders at the ask only; never market, never sell/scalp.

PAPER simulates a full fill at the ask. DEMO/LIVE place a real V2 limit order, wait up to
ORDER_TIMEOUT_SEC, cancel the remainder, then reconcile order / fills / positions / balance against the
ledger.  ANY mismatch halts trading.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from . import model
from .caller import SERIES_PREFIX, Decision
from .config import Settings
from .kalshi import KalshiClient, KalshiError, Market, fp, on_grid
from .ledger import Ledger

log = logging.getLogger("btc15.exec")
FEE_TOL = 0.0101          # per-order predicted-vs-actual fee tolerance (see spec.FEE_RATE note)
BAL_TOL = 0.02
QTY_TOL = 1e-6


@dataclass
class ExecResult:
    ok: bool
    filled: float = 0.0
    fill_price: Optional[float] = None
    fee: float = 0.0
    order_id: Optional[str] = None
    note: str = ""


class Halt:
    """Persistent halt flag backed by the ledger (survives restarts until `reset-halt`)."""

    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    @property
    def reason(self) -> Optional[str]:
        return self.ledger.halt_reason()

    def halt(self, reason: str) -> None:
        log.error("HALT: %s", reason)
        self.ledger.set_halt(reason)


class Executor:
    def __init__(self, cfg: Settings, client: Optional[KalshiClient], ledger: Ledger, halt: Halt,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep, clock: Callable[[], float] = time.time):
        self.cfg, self.client, self.ledger, self.halt = cfg, client, ledger, halt
        self.sleep, self.clock = sleep, clock
        self.bankroll: Optional[float] = None

    # ------------------------------------------------------------------ entry point
    async def execute(self, d: Decision, mkt: Market) -> ExecResult:
        if d.action not in ("BUY_UP", "BUY_DOWN") or d.contracts < 1 or d.price is None:
            return ExecResult(False, note="not a BUY")
        if not d.ticker.startswith(SERIES_PREFIX):
            return ExecResult(False, note="NO_COMBOS")
        if self.halt.reason:
            return ExecResult(False, note=f"halted: {self.halt.reason}")
        if self.cfg.MODE == "PAPER":
            return self._paper(d)
        return await self._real(d, mkt)

    # ------------------------------------------------------------------ PAPER
    def _paper(self, d: Decision) -> ExecResult:
        fee = model.fee(d.price, d.contracts, self.cfg.FEE_RATE)
        self.ledger.update_row(d.ticker, d.mode, fill_price=d.price, filled_contracts=float(d.contracts),
                               fee_paid=fee, note="paper: simulated full fill at ask")
        return ExecResult(True, float(d.contracts), d.price, fee, None, "paper")

    # ------------------------------------------------------------------ DEMO / LIVE
    async def _real(self, d: Decision, mkt: Market) -> ExecResult:
        c = self.client
        assert c is not None
        t = d.ticker
        # BUY_DOWN = buy NO at no_ask  ==  sell YES at 1 - no_ask (V2 endpoint is YES-book only)
        book_side, px = ("bid", d.price) if d.side == "yes" else ("ask", round(1.0 - d.price, 4))
        if not on_grid(px, mkt.price_ranges):
            self.ledger.update_row(t, d.mode, note=f"price {px} off the tick grid; not sent")
            return ExecResult(False, note="off-grid price")
        try:    # fresh, authoritative one-per-market check straight from the API
            if await c.get_positions(t) or await c.list_orders(ticker=t, status="resting"):
                self.ledger.update_row(t, d.mode, note="ONE_PER_MARKET: API shows existing position/order")
                return ExecResult(False, note="existing position/order")
            bal_before = await c.get_balance()
        except KalshiError as e:
            self.ledger.update_row(t, d.mode, note=f"pre-trade check failed: {e}")
            return ExecResult(False, note="pre-trade check failed")

        cid = str(uuid.uuid4())
        self.ledger.update_row(t, d.mode, client_order_id=cid, note="order submitting")
        try:
            resp = await c.place_order(t, book_side, d.contracts, px, cid)
        except KalshiError as e:
            if e.definite:
                self.ledger.update_row(t, d.mode, note=f"order rejected: {e}")
                return ExecResult(False, note="rejected")
            order_id = await self._recover(t, cid)        # outcome unknown -> find out or HALT
            if order_id is None:
                return ExecResult(False, note="not placed")
            resp = {"order_id": order_id}
        order_id = resp["order_id"]
        self.ledger.update_row(t, d.mode, order_id=order_id, note="order placed")

        # wait up to the timeout for a full fill, then cancel the remainder
        deadline = self.clock() + self.cfg.ORDER_TIMEOUT_SEC
        order = None
        try:
            while True:
                order = await c.get_order(order_id)
                if order["status"] != "resting" or fp(order["remaining_count_fp"]) <= 0 or self.clock() >= deadline:
                    break
                await self.sleep(1.0)
            if order["status"] == "resting":
                try:
                    await c.cancel_order(order_id, t)
                except KalshiError as e:
                    if e.status != 404:                  # 404 = already executed/cancelled
                        raise
                order = await c.get_order(order_id)
        except KalshiError as e:
            self.halt.halt(f"ORDER_STATE_UNKNOWN {order_id}: {e}")
            return ExecResult(False, order_id=order_id, note="state unknown")
        return await self._reconcile_order(d, order_id, order, bal_before)

    async def _recover(self, ticker: str, cid: str) -> Optional[str]:
        """POST timed out: the order may or may not exist. Look it up by client_order_id; HALT if unsure."""
        for attempt in range(2):
            try:
                for o in await self.client.list_orders(ticker=ticker):
                    if o.get("client_order_id") == cid:
                        return o["order_id"]
                if await self.client.get_positions(ticker):
                    self.halt.halt(f"UNKNOWN_POSITION after ambiguous order on {ticker}")
                    return None
            except KalshiError:
                break
            await self.sleep(1.0)
        self.halt.halt(f"ORDER_OUTCOME_UNKNOWN {ticker} client_order_id={cid}")
        return None

    async def _reconcile_order(self, d: Decision, order_id: str, order: dict, bal_before: float) -> ExecResult:
        c, t = self.client, d.ticker
        problems: list[str] = []
        try:
            fills = await c.get_fills(order_id)
            positions = await c.get_positions(t)
            bal_after = await c.get_balance()
        except KalshiError as e:
            self.halt.halt(f"RECONCILE_FETCH_FAILED {order_id}: {e}")
            return ExecResult(False, order_id=order_id, note="reconcile fetch failed")
        self.bankroll = bal_after

        filled = fp(order["fill_count_fp"])
        key = "yes_price_dollars" if d.side == "yes" else "no_price_dollars"
        n_fill = sum(fp(f["count_fp"]) for f in fills)
        avg = (sum(fp(f["count_fp"]) * fp(f[key]) for f in fills) / n_fill) if n_fill > 0 else None
        fee_paid = sum(fp(f.get("fee_cost")) for f in fills)

        if order["status"] == "resting":
            problems.append("order still resting after cancel")
        if abs(n_fill - filled) > QTY_TOL:
            problems.append(f"fills sum {n_fill} != order fill_count {filled}")
        if filled > d.contracts + QTY_TOL:
            problems.append(f"filled {filled} > requested {d.contracts}")
        if avg is not None and avg > d.price + 1e-6:
            problems.append(f"fill price {avg} above limit {d.price}")
        expected_pos = filled if d.side == "yes" else -filled
        if abs(positions.get(t, 0.0) - expected_pos) > QTY_TOL:
            problems.append(f"position {positions.get(t, 0.0)} != expected {expected_pos}")
        if avg is not None:
            predicted = model.fee(avg, filled, self.cfg.FEE_RATE)
            if abs(fee_paid - predicted) > FEE_TOL:
                problems.append(f"fee {fee_paid} vs formula {predicted}")
            spent = bal_before - bal_after
            if abs(spent - (filled * avg + fee_paid)) > BAL_TOL:
                problems.append(f"balance change {spent:.4f} vs cost {filled * avg + fee_paid:.4f}")
        elif abs(bal_before - bal_after) > BAL_TOL:
            problems.append(f"balance moved {bal_before - bal_after:.4f} with no fills")

        # ledger always records what the API says, even when we then halt
        self.ledger.update_row(t, d.mode, order_id=order_id, fill_price=avg, filled_contracts=filled,
                               fee_paid=fee_paid, note="; ".join(problems) or f"order {order['status']}, filled {filled}")
        if problems:
            self.halt.halt("RECONCILIATION_MISMATCH " + "; ".join(problems))
            return ExecResult(False, filled, avg, fee_paid, order_id, "reconciliation mismatch")
        return ExecResult(True, filled, avg, fee_paid, order_id, f"filled {filled}/{d.contracts}")

    # ------------------------------------------------------------------ periodic reconciliation
    async def reconcile_positions(self, now: float) -> list[str]:
        """API positions must equal the ledger's open trades. Any difference halts."""
        if self.cfg.MODE == "PAPER" or self.client is None:
            return []
        try:
            api = await self.client.get_positions()
        except KalshiError:
            return []                                   # consecutive-error rule handles outages
        problems: list[str] = []
        led: dict[str, float] = {}
        grace: set[str] = set()
        for r in self.ledger.unsettled_trades(self.cfg.MODE):
            led[r["ticker"]] = r["filled_contracts"] * (1 if r["side"] == "yes" else -1)
            if r["close_ts"] and now > r["close_ts"] - 1:
                grace.add(r["ticker"])                   # settling: position may legitimately vanish
        for t in set(api) | set(led):
            if t in grace:
                continue
            if abs(api.get(t, 0.0) - led.get(t, 0.0)) > QTY_TOL:
                problems.append(f"{t}: api={api.get(t, 0.0)} ledger={led.get(t, 0.0)}")
        if problems:
            self.halt.halt("RECONCILIATION_MISMATCH " + "; ".join(problems))
        return problems

    async def cancel_all_resting(self) -> int:
        if self.cfg.MODE == "PAPER" or self.client is None:
            return 0
        n = 0
        try:
            for o in await self.client.list_orders(status="resting"):
                try:
                    await self.client.cancel_order(o["order_id"], o["ticker"])
                    n += 1
                except KalshiError as e:
                    log.error("cancel failed %s: %s", o.get("order_id"), e)
        except KalshiError as e:
            log.error("could not list resting orders: %s", e)
        return n
