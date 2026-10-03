"""trade_caller(state, settings) -> Decision.  Pure and deterministic: no I/O, no clock reads."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from . import model
from .config import Settings

SERIES_PREFIX = "KXBTC15M"
GATE_ORDER = (
    "DATA_FRESH", "REF_DEGRADED", "DATA_GAPS", "VOL_SANITY", "SPREAD", "ENTRY_WINDOW",
    "DAILY_LOSS", "TOTAL_LOSS", "DIVERGENCE", "MIN_EDGE", "EDGE_SUSPECT", "SIZE",
    "ONE_PER_MARKET", "NO_COMBOS", "HALTED",
)


@dataclass
class State:
    now: float
    ticker: str
    strike: Optional[float]
    open_ts: float
    close_ts: float
    yes_bid: Optional[float]
    yes_ask: Optional[float]
    no_bid: Optional[float]
    no_ask: Optional[float]
    quote_ts: Optional[float]            # local receive time of the Kalshi quote
    composite: Optional[float]           # median of FRESH independent feeds
    ref_ts: Optional[float]              # newest message time among counted feeds
    n_fresh_feeds: int
    basis: Optional[float]               # basisEWMA
    realized_vol: Optional[float]
    gap_pct: float
    bankroll: float
    daily_pnl: float                     # realised PnL today (UTC) in this mode, USD
    total_pnl: float                     # realised PnL all-time in this mode, USD
    open_exposure: float                 # worst-case loss of unsettled filled trades, USD
    has_position_or_order: bool
    halted: Optional[str]                # reason string if halted
    proof_status: str                    # PROVEN | UNPROVEN
    mode: str


@dataclass
class Gate:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class Decision:
    action: str                          # BUY_UP | BUY_DOWN | SKIP
    reason: str
    ticker: str
    contracts: int
    price: Optional[float]
    order_fee: Optional[float]
    max_loss: Optional[float]
    max_payout: Optional[float]
    p: Optional[float]
    p_mkt: Optional[float]
    realized_vol: Optional[float]
    implied_vol: Optional[float]
    vol_used: Optional[float]
    edge_up: Optional[float]
    edge_dn: Optional[float]
    gates: list[Gate] = field(default_factory=list)
    proof_status: str = "UNPROVEN"
    mode: str = "PAPER"
    params_version: str = ""
    ts: float = 0.0
    # context useful for the dashboard / ledger (not part of the required field list)
    side: Optional[str] = None           # "yes" | "no"
    ref_price: Optional[float] = None
    strike: Optional[float] = None
    elapsed: Optional[float] = None
    tau: Optional[float] = None
    ref_label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def gate(self, name: str) -> Gate:
        return next(g for g in self.gates if g.name == name)


def _mid(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return None if a is None or b is None else (a + b) / 2.0


def trade_caller(st: State, cfg: Settings) -> Decision:
    W = float(cfg.SETTLE_WINDOW_SEC)
    elapsed = st.now - st.open_ts
    tau = st.close_ts - st.now
    quotes_ok = all(x is not None and 0.0 < x < 1.0 for x in (st.yes_bid, st.yes_ask, st.no_bid, st.no_ask)) \
        if None not in (st.yes_bid, st.yes_ask, st.no_bid, st.no_ask) else False
    ref = (st.composite - st.basis) if (st.composite is not None and st.basis is not None) else None
    strike_ok = st.strike is not None and st.strike > 0
    ref_ok = ref is not None and ref > 0

    # ---- model (computed whenever inputs allow, so SKIPs still carry a shadow forecast)
    p = p_mkt = iv = vol_used = edge_up = edge_dn = None
    side = price = p_side = None
    contracts = 0
    order_fee = max_loss = max_payout = None
    can_model = quotes_ok and strike_ok and ref_ok and st.realized_vol is not None \
        and st.realized_vol > 0 and tau > 0
    if can_model:
        p_mkt = _mid(st.yes_bid, st.yes_ask)
        iv = model.implied_vol(p_mkt, ref, st.strike, tau, W)
        if cfg.VOL_SOURCE == "blend" and iv is not None:
            vol_used = math.sqrt(st.realized_vol * iv)
        else:
            vol_used = st.realized_vol
        p = min(1.0, max(0.0, model.p_up(vol_used, ref, st.strike, tau, W)))
        fee_up = model.fee(st.yes_ask, 1, cfg.FEE_RATE)
        fee_dn = model.fee(st.no_ask, 1, cfg.FEE_RATE)
        edge_up = p - st.yes_ask - fee_up
        edge_dn = (1.0 - p) - st.no_ask - fee_dn
        if edge_up >= edge_dn:
            side, price, p_side = "yes", st.yes_ask, p
        else:
            side, price, p_side = "no", st.no_ask, 1.0 - p
        kelly = max(0.0, (p_side - price - model.fee(price, 1, cfg.FEE_RATE)) / (1.0 - price))
        stake = min(cfg.KELLY_FRACTION * kelly, cfg.MAX_STAKE_FRAC) * max(st.bankroll, 0.0)
        cap = cfg.effective_max_contracts(st.proof_status)
        contracts = int(min(cap, math.floor(stake / price + 1e-9)))
        if contracts >= 1:
            order_fee = model.fee(price, contracts, cfg.FEE_RATE)
            max_loss = round(contracts * price + order_fee, 6)
            max_payout = float(contracts)

    # ---- gates (all evaluated, in spec order; first failure is the SKIP reason)
    G: list[Gate] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        G.append(Gate(name, bool(ok), detail))

    ref_age = None if st.ref_ts is None else st.now - st.ref_ts
    quote_age = None if st.quote_ts is None else st.now - st.quote_ts
    fresh = (ref_age is not None and ref_age < cfg.REF_MAX_AGE_SEC and
             quote_age is not None and quote_age < cfg.QUOTE_MAX_AGE_SEC and
             quotes_ok and strike_ok and ref_ok)
    add("DATA_FRESH", fresh,
        f"ref_age={ref_age if ref_age is None else round(ref_age, 2)} quote_age="
        f"{quote_age if quote_age is None else round(quote_age, 2)} quotes_ok={quotes_ok} "
        f"strike_ok={strike_ok} ref_ok={ref_ok}")
    need = 3 if cfg.REQUIRE_3_FEEDS else 1
    ref_label = "SINGLE_FEED" if st.n_fresh_feeds == 1 else ""
    add("REF_DEGRADED", st.n_fresh_feeds >= need, f"fresh_feeds={st.n_fresh_feeds} need>={need} {ref_label}".strip())
    add("DATA_GAPS", st.gap_pct <= cfg.MAX_GAP_PCT, f"gap_pct={st.gap_pct:.3f}")
    rv = st.realized_vol
    add("VOL_SANITY", rv is not None and cfg.VOL_MIN <= rv <= cfg.VOL_MAX, f"realized_vol={rv}")
    spread = (st.yes_ask + st.no_ask) if (st.yes_ask is not None and st.no_ask is not None) else None
    add("SPREAD", spread is not None and cfg.SPREAD_MIN <= spread <= cfg.SPREAD_MAX, f"yes_ask+no_ask={spread}")
    add("ENTRY_WINDOW", cfg.ENTRY_MIN_SEC <= elapsed <= cfg.ENTRY_MAX_SEC, f"elapsed={elapsed:.0f}s")
    # Loss gates are worst-case: realised loss + open exposure + this trade's max loss must stay in limit.
    cand = max_loss or 0.0
    if cfg.DAILY_LOSS_LIMIT_USD is None:
        add("DAILY_LOSS", True, "limit unset")
    else:
        worst = st.daily_pnl - st.open_exposure - cand
        add("DAILY_LOSS", worst > -cfg.DAILY_LOSS_LIMIT_USD,
            f"daily_pnl={st.daily_pnl:.2f} open={st.open_exposure:.2f} cand={cand:.2f} limit={cfg.DAILY_LOSS_LIMIT_USD}")
    if cfg.MAX_TOTAL_LOSS_USD is None:
        add("TOTAL_LOSS", True, "limit unset")
    else:
        worst = st.total_pnl - st.open_exposure - cand
        add("TOTAL_LOSS", worst > -cfg.MAX_TOTAL_LOSS_USD,
            f"total_pnl={st.total_pnl:.2f} open={st.open_exposure:.2f} cand={cand:.2f} limit={cfg.MAX_TOTAL_LOSS_USD}")
    add("DIVERGENCE", p is not None and abs(p - p_mkt) <= cfg.MAX_DIVERGENCE,
        "n/a" if p is None else f"|p-p_mkt|={abs(p - p_mkt):.3f}")
    best = None if edge_up is None else max(edge_up, edge_dn)
    add("MIN_EDGE", best is not None and best >= cfg.MIN_EDGE, f"edge={best}")
    add("EDGE_SUSPECT", best is not None and best <= cfg.EDGE_SUSPECT, f"edge={best}")
    add("SIZE", contracts >= 1, f"contracts={contracts}")
    add("ONE_PER_MARKET", not st.has_position_or_order, f"existing={st.has_position_or_order}")
    add("NO_COMBOS", st.ticker.startswith(SERIES_PREFIX), st.ticker)
    halt_reason = st.halted
    if st.mode == "LIVE" and st.proof_status != "PROVEN" and cfg.ALLOW_UNPROVEN != "yes":
        halt_reason = halt_reason or "LIVE_UNPROVEN_WITHOUT_ALLOW_UNPROVEN"
    add("HALTED", halt_reason is None, halt_reason or "")

    failed = next((g for g in G if not g.passed), None)
    if failed is None:
        action, reason = ("BUY_UP" if side == "yes" else "BUY_DOWN"), "ALL_GATES_PASSED"
    else:
        action, reason = "SKIP", failed.name      # contracts stays as the hypothetical size (shadow forecast)

    return Decision(
        action=action, reason=reason, ticker=st.ticker, contracts=contracts,
        price=price, order_fee=order_fee, max_loss=max_loss, max_payout=max_payout,
        p=p, p_mkt=p_mkt, realized_vol=st.realized_vol, implied_vol=iv, vol_used=vol_used,
        edge_up=edge_up, edge_dn=edge_dn, gates=G, proof_status=st.proof_status, mode=st.mode,
        params_version=cfg.params_version(), ts=st.now, side=side, ref_price=ref, strike=st.strike,
        elapsed=elapsed, tau=tau, ref_label=ref_label,
    )
