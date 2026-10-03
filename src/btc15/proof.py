"""PROVEN / UNPROVEN evaluation from settled ledger rows on the CURRENT params_version (PAPER + LIVE rows)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from .ledger import Ledger

MIN_SETTLED = 300
MIN_TRADES = 100
MIN_BUCKET_N = 20
BUCKET_TOL = 0.05
BUCKETS = [(0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.0001)]


@dataclass
class Bucket:
    label: str
    n: int
    avg_p_mkt: Optional[float]
    avg_p: Optional[float]
    win_rate: Optional[float]
    gap: Optional[float]          # |avg p - win%|
    evaluated: bool
    ok: Optional[bool]


@dataclass
class ProofReport:
    status: str = "UNPROVEN"
    params_version: str = ""
    n_settled: int = 0
    n_trades: int = 0
    brier_p: Optional[float] = None
    brier_mkt: Optional[float] = None
    brier_const: Optional[float] = None     # baseline: constant forecast = realised YES rate ("p_realized")
    roi: Optional[float] = None
    pnl: float = 0.0
    staked: float = 0.0
    buckets: list[Bucket] = field(default_factory=list)
    failing: list[str] = field(default_factory=list)
    headline: str = ""
    fav_avg_priced: Optional[float] = None
    fav_win_rate: Optional[float] = None
    fav_n: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate(rows: list[dict], params_version: str) -> ProofReport:
    r = ProofReport(params_version=params_version)
    # DEMO rows are excluded: the demo exchange has its own order books, so its "market favourites" say
    # nothing about production calibration. Evidence = PAPER (production data) + LIVE.
    rows = [x for x in rows if x["params_version"] == params_version and x["result"] in ("yes", "no")
            and x.get("mode", "PAPER") != "DEMO" and x["p"] is not None and x["p_mkt"] is not None]
    r.n_settled = len(rows)
    ys = [1.0 if x["result"] == "yes" else 0.0 for x in rows]
    if rows:
        r.brier_p = sum((x["p"] - y) ** 2 for x, y in zip(rows, ys)) / len(rows)
        r.brier_mkt = sum((x["p_mkt"] - y) ** 2 for x, y in zip(rows, ys)) / len(rows)
        base = sum(ys) / len(ys)
        r.brier_const = sum((base - y) ** 2 for y in ys) / len(ys)

    # reliability, viewed from the market FAVOURITE's side
    fav = []
    for x, y in zip(rows, ys):
        if x["p_mkt"] >= 0.5:
            fav.append((x["p_mkt"], x["p"], y))
        else:
            fav.append((1 - x["p_mkt"], 1 - x["p"], 1 - y))
    for lo, hi in BUCKETS:
        sel = [f for f in fav if lo <= f[0] < hi]
        label = f"{int(lo * 100)}-{min(int(hi * 100), 100)}"
        if not sel:
            r.buckets.append(Bucket(label, 0, None, None, None, None, False, None))
            continue
        n = len(sel)
        avg_m, avg_p, win = (sum(f[i] for f in sel) / n for i in (0, 1, 2))
        gap = abs(avg_p - win)
        ev = n >= MIN_BUCKET_N
        r.buckets.append(Bucket(label, n, avg_m, avg_p, win, gap, ev, (gap <= BUCKET_TOL) if ev else None))
    if fav:
        r.fav_n = len(fav)
        r.fav_avg_priced = sum(f[0] for f in fav) / len(fav)
        r.fav_win_rate = sum(f[2] for f in fav) / len(fav)
        r.headline = (f"Market favorites priced at {r.fav_avg_priced * 100:.1f}% won "
                      f"{r.fav_win_rate * 100:.1f}% (n={r.fav_n})")
    else:
        r.headline = "No settled markets yet on this params_version"

    trades = [x for x in rows if (x["filled_contracts"] or 0) > 0 and x["pnl"] is not None]
    r.n_trades = len(trades)
    r.pnl = sum(x["pnl"] for x in trades)
    r.staked = sum(x["filled_contracts"] * x["fill_price"] for x in trades)
    r.roi = (r.pnl / r.staked) if r.staked > 0 else None

    if r.n_settled < MIN_SETTLED:
        r.failing.append(f"settled rows {r.n_settled} < {MIN_SETTLED}")
    if r.brier_p is None or r.brier_p >= r.brier_mkt:
        r.failing.append("Brier(p) is not below Brier(p_mkt)")
    bad = [b.label for b in r.buckets if b.evaluated and not b.ok]
    if bad:
        r.failing.append("calibration gap > 5pts in buckets " + ",".join(bad))
    if r.n_trades < MIN_TRADES:
        r.failing.append(f"trades {r.n_trades} < {MIN_TRADES}")
    if r.roi is None or r.roi <= 0:
        r.failing.append("ROI after fees is not > 0")
    r.status = "PROVEN" if not r.failing else "UNPROVEN"
    return r


def current(ledger: Ledger, params_version: str) -> ProofReport:
    return evaluate(ledger.settled_rows(params_version), params_version)
