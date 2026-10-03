"""Pure math: fee, probability model, implied vol, realized vol, basis EWMA. No I/O."""
from __future__ import annotations

import math
import statistics
from decimal import ROUND_CEILING, Decimal
from typing import Optional

SECONDS_PER_YEAR = 31_536_000.0
SQRT_YEAR = math.sqrt(SECONDS_PER_YEAR)
IV_LO, IV_HI = 0.001, 10.0          # 0.1% .. 1000% annualised


def phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def fee(price: float, contracts: float = 1, rate: float = 0.07, multiplier: float = 1.0) -> float:
    """Kalshi taker fee: ceil_to_cent(rate * C * P * (1-P)) * multiplier.
    Decimal arithmetic so float noise can never push an exact cent up by one."""
    p = Decimal(str(round(price, 6)))
    raw = Decimal(str(rate)) * Decimal(str(contracts)) * p * (Decimal(1) - p) * Decimal(str(multiplier))
    cents = (raw * 100).quantize(Decimal("0.000001")).to_integral_value(rounding=ROUND_CEILING)
    return float(cents / 100)


def eff_time(tau: float, W: float) -> float:
    """Effective variance time for a payoff on the mean of the last W seconds."""
    if tau >= W:
        return tau - 2.0 * W / 3.0
    return max(tau, 0.0) ** 3 / (3.0 * W * W)


def p_up(vol: float, ref: float, strike: float, tau: float, W: float) -> float:
    eff = eff_time(tau, W)
    sig = vol / SQRT_YEAR
    denom = sig * math.sqrt(eff) if eff > 0 else 0.0
    if denom <= 0:
        return 1.0 if ref >= strike else 0.0
    return phi(math.log(ref / strike) / denom)


def implied_vol(p_mkt: float, ref: float, strike: float, tau: float, W: float) -> Optional[float]:
    """Bisection on [0.1%, 1000%] so p_up(v) = p_mkt. None if no solution (e.g. the market
    disagrees with the sign of ln(ref/strike), or p_mkt is outside the reachable range)."""
    if not (0.0 < p_mkt < 1.0) or ref <= 0 or strike <= 0 or eff_time(tau, W) <= 0:
        return None
    f = lambda v: p_up(v, ref, strike, tau, W) - p_mkt  # noqa: E731
    lo, hi = IV_LO, IV_HI
    flo, fhi = f(lo), f(hi)
    if flo == 0:
        return lo
    if fhi == 0:
        return hi
    if (flo > 0) == (fhi > 0):
        return None
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        fm = f(mid)
        if (fm > 0) == (flo > 0):
            lo, flo = mid, fm
        else:
            hi = mid
        if hi - lo < 1e-10:
            break
    return 0.5 * (lo + hi)


# ---------------------------------------------------------------- realized volatility
def fill_gaps(series: dict[int, float], start: int, end: int, max_gap: int = 2) -> list[Optional[float]]:
    """Per-second prices for [start, end). Gaps (runs of missing seconds) of length <= max_gap are
    forward-filled; longer runs stay None (dropped)."""
    out: list[Optional[float]] = []
    last: Optional[float] = None
    run: list[int] = []
    for i, sec in enumerate(range(start, end)):
        v = series.get(sec)
        if v is not None:
            if run:
                fill = last is not None and len(run) <= max_gap
                for j in run:
                    out[j] = last if fill else None
                run = []
            out.append(v)
            last = v
        else:
            run.append(i)
            out.append(None)
    # trailing run that is still open: only fill if short and followed by data (it is not) -> leave None
    return out


def gap_pct(series: dict[int, float], now_sec: int, window: int = 1800) -> float:
    present = sum(1 for s in range(now_sec - window, now_sec) if s in series)
    return (window - present) / window


def realized_vol(series: dict[int, float], now_sec: int, window: int = 1800,
                 block: int = 15, min_blocks: int = 60) -> Optional[float]:
    """Annualised vol from non-overlapping `block`-second log returns of the 1s composite series.
    Gaps <= 2s are forward-filled, longer gaps are dropped. 1s returns whose deviation from the
    median exceeds 10x MAD are treated as outliers (jump removed, contributes 0)."""
    start = now_sec - window
    px = fill_gaps(series, start, now_sec)
    logp = [math.log(p) if p is not None and p > 0 else None for p in px]
    rets: list[Optional[float]] = [None]
    for a, b in zip(logp, logp[1:]):
        rets.append(None if a is None or b is None else b - a)
    valid = [r for r in rets if r is not None]
    if len(valid) < block * min_blocks // 2:
        return None
    med = statistics.median(valid)
    mad = statistics.median(abs(r - med) for r in valid)
    if mad == 0:
        # The composite is often unchanged for a second: when over half the returns are exactly zero the MAD
        # collapses to 0. Take it over the non-zero returns instead; a mean-based fallback clipped real moves
        # and understated vol by ~45% on live data.
        nz = [abs(r - med) for r in valid if r != med]
        mad = statistics.median(nz) if nz else 0.0
    if mad == 0:
        return None
    cut = 10.0 * mad
    cleaned = [None if r is None else (0.0 if abs(r - med) > cut else r) for r in rets]
    sums: list[float] = []
    for s in range(1, len(cleaned) - block + 1, block):
        w = cleaned[s:s + block]
        if all(x is not None for x in w):
            sums.append(sum(w))
    if len(sums) < min_blocks:
        return None
    sd = statistics.stdev(sums)
    return sd / math.sqrt(block) * SQRT_YEAR


def ewma_alpha(half_life_markets: float) -> float:
    return 1.0 - 0.5 ** (1.0 / half_life_markets)


def ewma_update(prev: Optional[float], sample: float, half_life_markets: float) -> float:
    if prev is None:
        return sample
    a = ewma_alpha(half_life_markets)
    return a * sample + (1.0 - a) * prev
