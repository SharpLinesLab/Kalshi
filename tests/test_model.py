import math
import random
import statistics

import pytest

from btc15 import model


def gbm_series(vol_annual, seconds=1800, start=100000.0, seed=1, t0=1_700_000_000):
    rng = random.Random(seed)
    sig = vol_annual / model.SQRT_YEAR
    px, out = start, {}
    for i in range(seconds + 5):
        out[t0 + i] = px
        px *= math.exp(rng.gauss(-0.5 * sig * sig, sig))
    return out, t0 + seconds


def test_p_is_half_at_strike():
    for tau in (30, 200, 600):
        assert model.p_up(0.5, 100000.0, 100000.0, tau, 60) == pytest.approx(0.5)


def test_p_monotonic_in_price():
    ps = [model.p_up(0.5, 100000.0 + d, 100000.0, 400, 60) for d in range(-500, 501, 50)]
    assert all(a < b for a, b in zip(ps, ps[1:]))
    assert 0 < ps[0] < 0.5 < ps[-1] < 1


def test_eff_time_is_continuous_at_W():
    W = 60
    assert model.eff_time(W, W) == pytest.approx(W - 2 * W / 3)
    assert model.eff_time(W - 1e-9, W) == pytest.approx(model.eff_time(W, W), rel=1e-6)
    assert model.eff_time(0, W) == 0


def test_p_degenerates_to_step_when_no_time_left():
    assert model.p_up(0.5, 100001, 100000, 0, 60) == 1.0
    assert model.p_up(0.5, 99999, 100000, 0, 60) == 0.0


@pytest.mark.parametrize("vol", [0.05, 0.50])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_realized_vol_recovers_gbm(vol, seed):
    series, now = gbm_series(vol, seed=seed)
    est = model.realized_vol(series, now)
    assert est is not None and abs(est / vol - 1) <= 0.15


def test_realized_vol_is_robust_to_outliers():
    vol = 0.5
    series, now = gbm_series(vol, seed=7)
    shifted, level = {}, 1.0
    jumps = {now - 1500, now - 1100, now - 700, now - 330, now - 90}      # persistent 1% level shifts
    for s in sorted(series):
        if s in jumps:
            level *= 1.01
        shifted[s] = series[s] * level
    est = model.realized_vol(shifted, now)
    assert abs(est / vol - 1) <= 0.15
    # without the MAD filter the same data is badly inflated -> the filter is doing real work
    px = [math.log(shifted[s]) for s in range(now - 1800, now)]
    rets = [b - a for a, b in zip(px, px[1:])]
    sums = [sum(rets[i:i + 15]) for i in range(0, len(rets) - 14, 15)]
    naive = statistics.stdev(sums) / math.sqrt(15) * model.SQRT_YEAR
    assert naive > 1.5 * vol


def test_gaps_up_to_2s_are_filled_longer_dropped():
    s = {10: 1.0, 11: 1.0, 14: 1.0, 15: 1.0, 19: 1.0}                  # gap of 2 (12,13) and gap of 3 (16,17,18)
    out = model.fill_gaps(s, 10, 20)
    assert out[2:4] == [1.0, 1.0]                                      # 12,13 filled
    assert out[6:9] == [None, None, None]                              # 16,17,18 dropped


def test_gap_pct():
    s = {t: 1.0 for t in range(0, 1800) if t % 10}                     # 10% missing
    assert model.gap_pct(s, 1800) == pytest.approx(0.1, abs=0.001)


def test_realized_vol_none_without_data():
    assert model.realized_vol({}, 5000) is None


@pytest.mark.parametrize("price,count,expected", [
    (0.50, 1, 0.02), (0.50, 100, 1.75), (0.99, 1, 0.01), (0.01, 1, 0.01), (0.73, 1, 0.02),
    (0.50, 10, 0.18),
    (0.90, 100, 0.63),        # 0.07*100*0.9*0.1 is exactly 0.63; naive float math rounds this UP to 0.64
])
def test_fee_formula(price, count, expected):
    assert model.fee(price, count) == pytest.approx(expected, abs=1e-9)


def test_fee_is_always_rounded_up_to_the_cent():
    for p in (0.13, 0.27, 0.41, 0.58, 0.77, 0.91):
        for c in (1, 3, 7, 25):
            raw = 0.07 * c * p * (1 - p)
            f = model.fee(p, c)
            assert f >= raw - 1e-12 and f - raw < 0.01 + 1e-9
            assert round(f * 100) == pytest.approx(f * 100, abs=1e-6)


@pytest.mark.parametrize("vol,ref", [(0.1, 99_980.0), (0.1, 100_030.0), (0.5, 99_900.0), (0.5, 100_100.0),
                                     (1.5, 99_700.0), (1.5, 100_250.0), (0.05, 100_010.0)])
def test_implied_vol_solver_within_1pct(vol, ref):
    p = model.p_up(vol, ref, 100000.0, 420, 60)
    assert 1e-6 < p < 1 - 1e-6
    iv = model.implied_vol(p, ref, 100000.0, 420, 60)
    assert iv is not None and abs(iv / vol - 1) <= 0.01


def test_implied_vol_none_when_direction_disagrees():
    assert model.implied_vol(0.30, 100200.0, 100000.0, 400, 60) is None   # ref above strike, market says <50%
    assert model.implied_vol(0.70, 99800.0, 100000.0, 400, 60) is None


def test_ewma_half_life():
    a = model.ewma_alpha(8)
    assert (1 - a) ** 8 == pytest.approx(0.5)
    assert model.ewma_update(None, 3.0, 8) == 3.0


def sticky(series, hold_prob=0.55, seed=11):
    """Like the live composite: the price is unchanged in a large share of seconds (zero 1s returns)."""
    rng = random.Random(seed)
    out, last = {}, None
    for s in sorted(series):
        if last is None or rng.random() > hold_prob:
            last = series[s]
        out[s] = last
    return out


@pytest.mark.parametrize("vol", [0.2, 0.5])
def test_realized_vol_is_not_understated_when_most_returns_are_exactly_zero(vol):
    series, now = gbm_series(vol, seed=5)
    held = sticky(series)
    rets = [math.log(held[s + 1] / held[s]) for s in range(now - 1800, now - 1)]
    assert sum(r == 0 for r in rets) / len(rets) > 0.5                        # MAD of all returns is 0 here
    est = model.realized_vol(held, now)
    assert est is not None and abs(est / vol - 1) <= 0.20                      # old fallback returned ~0.55x


def test_outlier_filter_still_removes_jumps_in_a_sticky_series():
    vol = 0.5
    series, now = gbm_series(vol, seed=9)
    level, shifted = 1.0, {}
    for s in sorted(series):
        if s in {now - 1400, now - 900, now - 400}:
            level *= 1.01
        shifted[s] = series[s] * level
    est = model.realized_vol(sticky(shifted), now)
    assert est is not None and abs(est / vol - 1) <= 0.25
