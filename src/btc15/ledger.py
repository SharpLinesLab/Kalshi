"""SQLite ledger: one row per (market, mode), plus meta (halt, basis) and audit events."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
  ticker TEXT NOT NULL, mode TEXT NOT NULL, kind TEXT NOT NULL,           -- BUY | SHADOW
  params_version TEXT NOT NULL, ts REAL NOT NULL, open_ts REAL, close_ts REAL, strike REAL,
  action TEXT, reason TEXT, side TEXT, contracts INTEGER, price REAL, order_fee REAL, max_loss REAL,
  max_payout REAL, p REAL, p_mkt REAL, realized_vol REAL, implied_vol REAL, vol_used REAL,
  edge_up REAL, edge_dn REAL, gates_json TEXT, proof_status TEXT, ref_price REAL,
  order_id TEXT, client_order_id TEXT, fill_price REAL, filled_contracts REAL DEFAULT 0, fee_paid REAL DEFAULT 0,
  result TEXT, pnl REAL, settle_source TEXT, expiration_value REAL,
  final60_composite REAL, final60_vs_official REAL, note TEXT,
  PRIMARY KEY (ticker, mode)
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, type TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS series (sec INTEGER PRIMARY KEY, px REAL NOT NULL);
CREATE TABLE IF NOT EXISTS basis_log (
  ticker TEXT PRIMARY KEY, ts REAL, strike REAL, composite_open REAL, sample REAL, ewma REAL,
  final60_composite REAL, official REAL, diff REAL
);
"""


def utc_day_start(ts: float) -> float:
    d = datetime.fromtimestamp(ts, tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return d.timestamp()


def compute_pnl(side: str, result: str, contracts: float, price: float, fee: float) -> float:
    """won: C*(1-price) - fee ; lost: -C*price - fee   (side 'yes'/'no' vs result 'yes'/'no')."""
    if contracts <= 0:
        return 0.0
    won = side == result
    return round(contracts * (1.0 - price) - fee, 6) if won else round(-contracts * price - fee, 6)


class Ledger:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._lock = threading.Lock()

    # ------------------------------------------------------------ meta / events
    def get_meta(self, k: str) -> Optional[str]:
        with self._lock:
            r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else None

    def set_meta(self, k: str, v: Optional[str]) -> None:
        with self._lock:
            if v is None:
                self.db.execute("DELETE FROM meta WHERE k=?", (k,))
            else:
                self.db.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    def log_event(self, type_: str, detail: str) -> None:
        with self._lock:
            self.db.execute("INSERT INTO events(ts,type,detail) VALUES(?,?,?)", (time.time(), type_, detail[:2000]))

    # ------------------------------------------------------------ rows
    def get_row(self, ticker: str, mode: str) -> Optional[dict]:
        with self._lock:
            r = self.db.execute("SELECT * FROM markets WHERE ticker=? AND mode=?", (ticker, mode)).fetchone()
        return dict(r) if r else None

    def record_decision(self, d: dict, kind: str, open_ts: float, close_ts: float) -> bool:
        """BUY rows are never overwritten by SHADOW rows; a BUY replaces a SHADOW; first BUY wins."""
        existing = self.get_row(d["ticker"], d["mode"])
        if existing and (existing["kind"] == "BUY" or kind == "SHADOW"):
            return False
        gates = json.dumps([{"name": g["name"], "passed": g["passed"], "detail": g["detail"]} for g in d["gates"]])
        vals = (
            d["ticker"], d["mode"], kind, d["params_version"], d["ts"], open_ts, close_ts, d.get("strike"),
            d["action"], d["reason"], d.get("side"), d["contracts"], d["price"], d["order_fee"], d["max_loss"],
            d["max_payout"], d["p"], d["p_mkt"], d["realized_vol"], d["implied_vol"], d["vol_used"],
            d["edge_up"], d["edge_dn"], gates, d["proof_status"], d.get("ref_price"),
        )
        with self._lock:
            self.db.execute("DELETE FROM markets WHERE ticker=? AND mode=?", (d["ticker"], d["mode"]))
            self.db.execute(
                "INSERT INTO markets(ticker,mode,kind,params_version,ts,open_ts,close_ts,strike,action,reason,side,contracts,"
                "price,order_fee,max_loss,max_payout,p,p_mkt,realized_vol,implied_vol,vol_used,edge_up,edge_dn,gates_json,"
                "proof_status,ref_price) VALUES (" + ",".join("?" * 26) + ")", vals)
        return True

    def update_row(self, ticker: str, mode: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self.db.execute(f"UPDATE markets SET {cols} WHERE ticker=? AND mode=?", (*fields.values(), ticker, mode))

    def has_order_for(self, ticker: str, mode: str) -> bool:
        r = self.get_row(ticker, mode)
        return bool(r and r["kind"] == "BUY" and (r["order_id"] or (r["filled_contracts"] or 0) > 0))

    def unsettled_trades(self, mode: str) -> list[dict]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM markets WHERE mode=? AND filled_contracts>0 AND result IS NULL", (mode,)).fetchall()
        return [dict(r) for r in rows]

    def unsettled_rows(self, mode: str, before_ts: float) -> list[dict]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM markets WHERE mode=? AND result IS NULL AND close_ts<?", (mode, before_ts)).fetchall()
        return [dict(r) for r in rows]

    def settle_row(self, ticker: str, mode: str, result: str, source: str, expiration_value: Optional[float]) -> None:
        r = self.get_row(ticker, mode)
        if not r:
            return
        pnl = None
        if (r["filled_contracts"] or 0) > 0:
            pnl = compute_pnl(r["side"], result, r["filled_contracts"], r["fill_price"], r["fee_paid"] or 0.0)
        self.update_row(ticker, mode, result=result, pnl=pnl, settle_source=source, expiration_value=expiration_value)

    # ------------------------------------------------------------ aggregates
    def pnl_since(self, mode: str, since_ts: float = 0.0) -> float:
        with self._lock:
            r = self.db.execute("SELECT COALESCE(SUM(pnl),0) s FROM markets WHERE mode=? AND pnl IS NOT NULL AND close_ts>=?", (mode, since_ts)).fetchone()
        return float(r["s"])

    def open_exposure(self, mode: str) -> float:
        total = 0.0
        for r in self.unsettled_trades(mode):
            total += r["filled_contracts"] * r["fill_price"] + (r["fee_paid"] or 0.0)
        return total

    def settled_rows(self, params_version: Optional[str] = None) -> list[dict]:
        q, a = "SELECT * FROM markets WHERE result IN ('yes','no')", []
        if params_version:
            q += " AND params_version=?"
            a.append(params_version)
        with self._lock:
            return [dict(r) for r in self.db.execute(q + " ORDER BY ts", a).fetchall()]

    def recent_rows(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM markets ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()]

    # ------------------------------------------------------------ basis log
    def log_basis(self, ticker: str, **f: Any) -> None:
        with self._lock:
            self.db.execute("INSERT OR IGNORE INTO basis_log(ticker,ts) VALUES(?,?)", (ticker, time.time()))
            cols = ", ".join(f"{k}=?" for k in f)
            self.db.execute(f"UPDATE basis_log SET {cols} WHERE ticker=?", (*f.values(), ticker))

    def has_basis_sample(self, ticker: str) -> bool:
        with self._lock:
            r = self.db.execute("SELECT sample FROM basis_log WHERE ticker=?", (ticker,)).fetchone()
        return bool(r and r["sample"] is not None)

    # ------------------------------------------------------------ composite price history (warm-up survives restarts)
    def save_series(self, items: list[tuple[int, float]], keep_after: int) -> None:
        with self._lock:
            self.db.execute("BEGIN")
            self.db.executemany("INSERT OR REPLACE INTO series(sec,px) VALUES(?,?)", items)
            self.db.execute("DELETE FROM series WHERE sec<?", (keep_after,))
            self.db.execute("COMMIT")

    def load_series(self, since_sec: int) -> dict[int, float]:
        with self._lock:
            rows = self.db.execute("SELECT sec,px FROM series WHERE sec>=?", (since_sec,)).fetchall()
        return {r["sec"]: r["px"] for r in rows}

    # ------------------------------------------------------------ halt
    def halt_reason(self) -> Optional[str]:
        return self.get_meta("halt")

    def set_halt(self, reason: str) -> None:
        if not self.get_meta("halt"):
            self.set_meta("halt", reason)
        self.log_event("HALT", reason)

    def clear_halt(self) -> None:
        self.set_meta("halt", None)
        self.log_event("RESET_HALT", "cleared by operator")
