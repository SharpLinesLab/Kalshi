"""The 1-second loop: discover market -> poll quote -> build state -> decide -> (maybe) execute -> settle."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from . import model, proof as proofmod
from .caller import Decision, State, trade_caller
from .config import Settings
from .executor import Executor, Halt
from .feeds import FeedBook
from .kalshi import KalshiClient, KalshiError, Market
from .ledger import Ledger, utc_day_start

log = logging.getLogger("btc15.engine")
BASIS_MAX_AGE = 2 * 3600.0
SHADOW_AT_SEC = 300.0


@dataclass
class AppState:
    """Snapshot shared with the dashboard (read-only there)."""
    mode: str = "PAPER"
    decision: Optional[dict] = None
    market: Optional[dict] = None
    feeds: list = field(default_factory=list)
    clone_suspect: bool = False
    daily_pnl: float = 0.0
    total_pnl: float = 0.0
    open_position: Optional[dict] = None
    halted: Optional[str] = None
    proof: Optional[dict] = None
    basis: Optional[float] = None
    composite: Optional[float] = None
    bankroll: float = 0.0
    ts: float = 0.0
    readiness: Optional[dict] = None


class Engine:
    def __init__(self, cfg: Settings, ledger: Ledger, client: Optional[KalshiClient], book: FeedBook,
                 executor: Executor, halt: Halt, app: AppState):
        self.cfg, self.ledger, self.client, self.book = cfg, ledger, client, book
        self.executor, self.halt, self.app = executor, halt, app
        self.market: Optional[Market] = None
        self.prev_action: dict[str, str] = {}
        self.acted: set[str] = set()
        self.proof = proofmod.ProofReport()
        self.basis: Optional[float] = None
        self.basis_ts: Optional[float] = None
        self._vol: tuple[float, Optional[float]] = (0.0, None)
        self._t_proof = self._t_settle = self._t_recon = self._t_bank = self._t_persist = 0.0
        self._kill_handled = False
        self.kill_requested: Optional[str] = None
        self.api_positions: dict[str, float] = {}
        b, bts = ledger.get_meta("basis"), ledger.get_meta("basis_ts")
        if b and bts:
            self.basis, self.basis_ts = float(b), float(bts)
        self._persisted_upto = 0
        self.restore_series(time.time())
        self.refresh_proof()

    # ------------------------------------------------------------------ helpers
    def restore_series(self, now: float) -> int:
        """Reload recent composite history so a restart does not reset the 30-minute warm-up.
        Downtime shows up as missing seconds, so gap_pct stays honest."""
        old = self.ledger.load_series(int(now) - 7200)
        for sec, px in old.items():
            self.book.series.setdefault(sec, px)
        self._persisted_upto = max(old, default=0)
        return len(old)

    def persist_series(self, now: float) -> None:
        new = [(s_, p_) for s_, p_ in self.book.series.items() if s_ > self._persisted_upto]
        if new:
            self.ledger.save_series(new, int(now) - 7200)
            self._persisted_upto = max(s_ for s_, _ in new)

    def readiness(self, now: float, predicting: bool) -> dict:
        """Why predictions are (not) trustworthy yet, with an ETA for the data-window warm-up."""
        sec = int(now)
        present = sum(1 for s_ in range(sec - 1800, sec) if s_ in self.book.series)
        need = int(1800 * (1 - self.cfg.MAX_GAP_PCT))
        _, n_fresh, _ = self.book.composite(now)
        checks = [
            {"name": "feeds", "ok": n_fresh >= (3 if self.cfg.REQUIRE_3_FEEDS else 1), "detail": f"{n_fresh} fresh independent feeds"},
            {"name": "history", "ok": present >= need, "detail": f"{present}/{need}s of the 1800s window"},
            {"name": "volatility", "ok": self._vol[1] is not None, "detail": "realized vol available" if self._vol[1] is not None else "needs >= 60 clean 15s blocks"},
            {"name": "basis", "ok": self._basis_value(now) is not None, "detail": "basis EWMA available" if self._basis_value(now) is not None else "waiting for the next market open"},
            {"name": "kalshi quote", "ok": self.market is not None and self.market.yes_ask is not None, "detail": self.market.ticker if self.market else "no open market"},
            {"name": "not halted", "ok": not self.halt.reason, "detail": self.halt.reason or ""},
        ]
        eta = max(0, need - present) if not checks[1]["ok"] else 0
        return {"ready": all(c["ok"] for c in checks), "checks": checks, "history_eta_sec": eta,
                "predicting": predicting}
    def refresh_proof(self) -> None:
        self.proof = proofmod.current(self.ledger, self.cfg.params_version())

    def request_kill(self, who: str) -> None:
        self.kill_requested = who
        try:
            open(self.cfg.KILL_FILE, "a").close()
        except OSError:
            pass

    async def kill(self, why: str) -> None:
        """Kill switch: stop entries FIRST (halt), then cancel every resting order."""
        self.halt.halt(f"KILL: {why}")
        n = await self.executor.cancel_all_resting()
        self.ledger.log_event("KILL", f"{why}; cancelled {n} resting orders")
        self._kill_handled = True

    def _basis_value(self, now: float) -> Optional[float]:
        if self.basis is None or self.basis_ts is None or now - self.basis_ts > BASIS_MAX_AGE:
            return None
        return self.basis

    def _sample_basis(self, m: Market, now: float) -> None:
        """At each new market: basisSample = composite(at open) - official strike; EWMA with half-life in markets."""
        if m.strike is None or self.ledger.has_basis_sample(m.ticker):
            return
        if self.cfg.BASIS_METHOD == "avg60":
            comp = self.book.window_mean(int(m.open_ts), 60)       # same definition as the official strike
        else:
            comp = self.book.series.get(int(m.open_ts))
        if comp is None:
            return
        sample = comp - m.strike
        if abs(sample) > 0.02 * m.strike:                          # >2% is a bad sample, not a basis
            self.ledger.log_event("BASIS_REJECT", f"{m.ticker} sample={sample:.2f}")
            return
        self.basis = model.ewma_update(self._basis_value(now), sample, self.cfg.BASIS_HALF_LIFE_MARKETS)
        self.basis_ts = now
        self.ledger.set_meta("basis", repr(self.basis))
        self.ledger.set_meta("basis_ts", repr(now))
        self.ledger.log_basis(m.ticker, strike=m.strike, composite_open=comp, sample=sample, ewma=self.basis)

    def _realized_vol(self, now: float) -> tuple[Optional[float], float]:
        sec = int(now)
        if sec - int(self._vol[0]) >= 2:
            self._vol = (now, model.realized_vol(self.book.series, sec))
        return self._vol[1], model.gap_pct(self.book.series, sec)

    # ------------------------------------------------------------------ market discovery
    async def _ensure_market(self, now: float) -> None:
        if self.market and now < self.market.close_ts:
            return
        old = self.market
        ms = [m for m in await self.client.open_markets()
              if m.ticker.startswith("KXBTC15M") and m.open_ts <= now < m.close_ts]
        if not ms:
            self.market = None
            return
        m = min(ms, key=lambda x: x.close_ts)
        if old is None or m.ticker != old.ticker:
            log.info("market %s strike=%s", m.ticker, m.strike)
        self.market = m

    async def _poll_quote(self, now: float) -> None:
        fresh = await self.client.get_market(self.market.ticker)
        if fresh.strike is None and self.market.strike is not None:
            fresh.strike = self.market.strike
        if fresh.status not in ("active", "open"):          # closed/determined: no tradable quotes
            fresh.yes_bid = fresh.yes_ask = fresh.no_bid = fresh.no_ask = None
        self.market = fresh

    # ------------------------------------------------------------------ one tick
    async def tick(self, now: Optional[float] = None) -> Optional[Decision]:
        now = time.time() if now is None else now
        cfg = self.cfg
        # --- kill switch / halt handling (a present KILL file ALWAYS keeps the system halted)
        if self.kill_requested or os.path.exists(cfg.KILL_FILE):
            if not self.halt.reason:
                self._kill_handled = False
            if not self._kill_handled:
                await self.kill(self.kill_requested or "KILL file present")
            self.kill_requested = None
        elif not self.halt.reason:
            self._kill_handled = False
        if self.client is not None and self.client.consecutive_errors >= cfg.MAX_API_ERRORS:
            self.halt.halt(f"API_ERRORS: {self.client.consecutive_errors} consecutive")

        # --- market + quote (public endpoints)
        try:
            await self._ensure_market(now)
            if self.market:
                await self._poll_quote(now)
        except KalshiError as e:
            log.warning("market data error: %s", e)
        m = self.market
        if m is None:
            self._publish(now, None)
            return None
        self._sample_basis(m, now)

        # --- bankroll
        if cfg.MODE == "PAPER":
            self.executor.bankroll = cfg.PAPER_BANKROLL
        elif now - self._t_bank > 15 and self.client is not None:
            self._t_bank = now
            try:
                self.executor.bankroll = await self.client.get_balance()
            except KalshiError as e:
                log.warning("balance error: %s", e)
        if now - self._t_proof > 60:
            self._t_proof = now
            self.refresh_proof()

        comp, n_fresh, ref_ts = self.book.composite(now)
        rv, gp = self._realized_vol(now)
        day0 = utc_day_start(now)
        st = State(
            now=now, ticker=m.ticker, strike=m.strike, open_ts=m.open_ts, close_ts=m.close_ts,
            yes_bid=m.yes_bid, yes_ask=m.yes_ask, no_bid=m.no_bid, no_ask=m.no_ask, quote_ts=m.recv_ts,
            composite=comp, ref_ts=ref_ts, n_fresh_feeds=n_fresh, basis=self._basis_value(now),
            realized_vol=rv, gap_pct=gp, bankroll=self.executor.bankroll or 0.0,
            daily_pnl=self.ledger.pnl_since(cfg.MODE, day0), total_pnl=self.ledger.pnl_since(cfg.MODE),
            open_exposure=self.ledger.open_exposure(cfg.MODE),
            has_position_or_order=(m.ticker in self.acted or self.ledger.has_order_for(m.ticker, cfg.MODE)
                                   or m.ticker in self.api_positions),
            halted=self.halt.reason, proof_status=self.proof.status, mode=cfg.MODE)
        d = trade_caller(st, cfg)
        dd = d.to_dict()

        # --- act ONLY on the SKIP -> BUY transition, once per market
        prev = self.prev_action.get(m.ticker)
        elapsed = now - m.open_ts
        if d.action != "SKIP" and prev == "SKIP" and m.ticker not in self.acted:
            self.acted.add(m.ticker)
            self.ledger.record_decision(dd, "BUY", m.open_ts, m.close_ts)
            res = await self.executor.execute(d, m)
            log.info("EXEC %s %s -> ok=%s %s", d.action, m.ticker, res.ok, res.note)
        elif elapsed >= SHADOW_AT_SEC and m.ticker not in self.acted:
            shadow = dd if d.action == "SKIP" else {**dd, "action": "SKIP", "reason": "NO_SKIP_TO_BUY_TRANSITION"}
            self.ledger.record_decision(shadow, "SHADOW", m.open_ts, m.close_ts)
        self.prev_action[m.ticker] = d.action

        # --- housekeeping
        if now - self._t_persist > 10:
            self._t_persist = now
            self.persist_series(now)
        if now - self._t_settle > 5:
            self._t_settle = now
            await self.settle_pending(now)
        if cfg.MODE != "PAPER" and now - self._t_recon > 30:
            self._t_recon = now
            await self.executor.reconcile_positions(now)
            if self.client is not None:
                try:
                    self.api_positions = await self.client.get_positions()
                except KalshiError:
                    pass
        self._publish(now, dd)
        return d

    # ------------------------------------------------------------------ settlement
    async def settle_pending(self, now: float) -> int:
        n = 0
        for row in self.ledger.unsettled_rows(self.cfg.MODE, now - 5):
            try:
                m = await self.client.get_market(row["ticker"])
            except KalshiError:
                continue
            if m.result in ("yes", "no"):
                self.ledger.settle_row(row["ticker"], self.cfg.MODE, m.result, "kalshi_market", m.expiration_value)
                f60 = self.book.window_mean(int(row["close_ts"]), 60)
                if f60 is not None and m.expiration_value is not None:
                    diff = f60 - m.expiration_value
                    self.ledger.update_row(row["ticker"], self.cfg.MODE, final60_composite=f60, final60_vs_official=diff)
                    self.ledger.log_basis(row["ticker"], final60_composite=f60, official=m.expiration_value, diff=diff)
                n += 1
        return n

    # ------------------------------------------------------------------ dashboard snapshot
    def _publish(self, now: float, dd: Optional[dict]) -> None:
        a, m = self.app, self.market
        a.mode, a.ts, a.decision = self.cfg.MODE, now, dd
        a.readiness = self.readiness(now, bool(dd and dd.get("p") is not None))
        a.market = None if m is None else {
            "ticker": m.ticker, "strike": m.strike, "open_ts": m.open_ts, "close_ts": m.close_ts,
            "yes_bid": m.yes_bid, "yes_ask": m.yes_ask, "no_bid": m.no_bid, "no_ask": m.no_ask}
        a.feeds = self.book.snapshot(now)
        a.clone_suspect = self.book.clone_suspect
        a.daily_pnl = self.ledger.pnl_since(self.cfg.MODE, utc_day_start(now))
        a.total_pnl = self.ledger.pnl_since(self.cfg.MODE)
        a.halted = self.halt.reason
        a.proof = self.proof.to_dict()
        a.basis, a.composite = self._basis_value(now), self.book.composite(now)[0]
        a.bankroll = self.executor.bankroll or 0.0
        opn = self.ledger.unsettled_trades(self.cfg.MODE)
        a.open_position = opn[0] if opn else None

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            t0 = time.time()
            try:
                await self.tick(t0)
            except Exception:                                  # a bug must never kill the loop silently
                log.exception("tick failed")
                self.ledger.log_event("TICK_ERROR", "see logs")
            await asyncio.sleep(max(0.05, 1.0 - (time.time() - t0)))


async def backfill_results(ledger: Ledger, client: KalshiClient, mode: str, now: Optional[float] = None) -> int:
    """Fetch official results for every unsettled ledger row whose market has closed."""
    now = time.time() if now is None else now
    n = 0
    for row in ledger.unsettled_rows(mode, now - 5):
        try:
            m = await client.get_market(row["ticker"])
        except KalshiError:
            continue
        if m.result in ("yes", "no"):
            ledger.settle_row(row["ticker"], mode, m.result, "kalshi_market", m.expiration_value)
            n += 1
    return n
