import random

import pytest

from btc15 import proof
from btc15.ledger import Ledger, compute_pnl, utc_day_start
from conftest import base_state, make_cfg
from btc15.caller import trade_caller

PV = "pv1"


def test_pnl_formula_matches_the_spec():
    assert compute_pnl("yes", "yes", 10, 0.60, 0.17) == pytest.approx(10 * 0.40 - 0.17)       # won
    assert compute_pnl("yes", "no", 10, 0.60, 0.17) == pytest.approx(-10 * 0.60 - 0.17)       # lost
    assert compute_pnl("no", "no", 4, 0.30, 0.08) == pytest.approx(4 * 0.70 - 0.08)
    assert compute_pnl("no", "yes", 4, 0.30, 0.08) == pytest.approx(-4 * 0.30 - 0.08)
    assert compute_pnl("yes", "yes", 0, 0.5, 0.0) == 0.0


def d(ticker, mode="PAPER", action="BUY_UP", **kw):
    x = trade_caller(base_state(ticker=ticker, mode=mode), make_cfg(MODE=mode)).to_dict()
    x["action"] = action
    x.update(kw)
    return x


def test_buy_row_beats_shadow_and_is_never_overwritten():
    led = Ledger(":memory:")
    t = "KXBTC15M-A-00"
    assert led.record_decision(d(t, action="SKIP"), "SHADOW", 0, 900)
    assert not led.record_decision(d(t, action="SKIP"), "SHADOW", 0, 900)       # one shadow only
    assert led.record_decision(d(t, action="BUY_UP"), "BUY", 0, 900)            # BUY replaces SHADOW
    assert not led.record_decision(d(t, action="BUY_DOWN"), "BUY", 0, 900)      # first BUY wins
    assert not led.record_decision(d(t, action="SKIP"), "SHADOW", 0, 900)
    row = led.get_row(t, "PAPER")
    assert row["kind"] == "BUY" and row["action"] == "BUY_UP"


def test_every_decision_field_is_stored_and_modes_are_separate():
    led = Ledger(":memory:")
    t = "KXBTC15M-A-00"
    dec = d(t)
    led.record_decision(dec, "BUY", 0, 900)
    led.record_decision(d(t, mode="DEMO"), "BUY", 0, 900)
    row = led.get_row(t, "PAPER")
    for k in ("action", "reason", "ticker", "contracts", "price", "order_fee", "max_loss", "max_payout", "p", "p_mkt",
              "realized_vol", "implied_vol", "vol_used", "edge_up", "edge_dn", "proof_status", "mode", "params_version", "ts"):
        assert row[k] == dec[k], k
    assert row["gates_json"] and led.get_row(t, "DEMO") is not None


def test_settle_row_and_aggregates():
    led = Ledger(":memory:")
    t = "KXBTC15M-A-00"
    led.record_decision(d(t), "BUY", 0, 900)
    led.update_row(t, "PAPER", filled_contracts=10.0, fill_price=0.60, fee_paid=0.17, order_id=None)
    assert led.open_exposure("PAPER") == pytest.approx(10 * 0.60 + 0.17)
    led.settle_row(t, "PAPER", "no", "kalshi_market", 1.0)
    assert led.pnl_since("PAPER") == pytest.approx(-6.17) and led.open_exposure("PAPER") == 0
    assert led.pnl_since("PAPER", since_ts=10_000) == 0.0
    assert utc_day_start(1_700_000_000) == 1_699_920_000


def synth(n, *, edge=True, win_bias=0.0, seed=0, pv=PV):
    """Synthetic settled rows. Outcome ~ Bernoulli(true_p); market is noisy around true_p, model less noisy."""
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        true_p = rng.uniform(0.15, 0.85)
        y = 1 if rng.random() < true_p else 0
        p_mkt = min(0.97, max(0.03, true_p + rng.gauss(0, 0.12)))
        p = min(0.97, max(0.03, true_p + rng.gauss(0, 0.02 if edge else 0.2)))
        side_yes = p >= p_mkt
        price = (p_mkt + 0.01) if side_yes else (1 - p_mkt + 0.01)
        won = (y == 1) == side_yes
        fee = 0.02
        pnl = (1 - price - fee) if won else (-price - fee)
        rows.append(dict(params_version=pv, result="yes" if y else "no", p=p, p_mkt=p_mkt, filled_contracts=1.0,
                         fill_price=price, pnl=pnl + win_bias))
    return rows


def test_proven_requires_every_condition():
    rows = synth(3000, edge=True, win_bias=0.05, seed=3)
    rep = proof.evaluate(rows, PV)
    assert rep.n_settled == 3000 and rep.brier_p < rep.brier_mkt
    assert rep.status == "PROVEN", rep.failing


def test_unproven_when_too_few_settled_rows():
    rep = proof.evaluate(synth(299, win_bias=0.05, seed=3), PV)
    assert rep.status == "UNPROVEN" and any("settled rows" in f for f in rep.failing)


def test_unproven_when_model_is_not_better_than_market_brier():
    rep = proof.evaluate(synth(600, edge=False, win_bias=0.05, seed=3), PV)
    assert rep.status == "UNPROVEN" and any("Brier" in f for f in rep.failing)


def test_unproven_when_roi_after_fees_is_not_positive():
    rows = synth(600, edge=True, win_bias=-0.5, seed=3)
    rep = proof.evaluate(rows, PV)
    assert rep.roi < 0 and rep.status == "UNPROVEN" and any("ROI" in f for f in rep.failing)


def test_unproven_when_fewer_than_100_trades():
    rows = synth(600, edge=True, win_bias=0.05, seed=3)
    for r in rows[100:]:
        r["filled_contracts"] = 0.0                                           # shadow-only rows
    assert proof.evaluate(rows, PV).n_trades == 100 and not any("trades" in f for f in proof.evaluate(rows, PV).failing)
    for r in rows[99:]:
        r["filled_contracts"] = 0.0
    rep = proof.evaluate(rows, PV)
    assert rep.n_trades == 99 and any("trades" in f for f in rep.failing)


def test_unproven_when_a_bucket_with_n_ge_20_is_miscalibrated_by_more_than_5pts():
    rows = synth(600, edge=True, win_bias=0.05, seed=3)
    for r in rows:
        if 0.6 <= max(r["p_mkt"], 1 - r["p_mkt"]) < 0.7:
            r["p"] = 0.99 if r["p_mkt"] >= 0.5 else 0.01                       # claim near-certainty on the favourite
    rep = proof.evaluate(rows, PV)
    b = next(b for b in rep.buckets if b.label == "60-70")
    assert b.n >= 20 and b.evaluated and b.ok is False
    assert rep.status == "UNPROVEN" and any("60-70" in f for f in rep.failing)


def test_small_buckets_are_not_evaluated():
    rows = synth(600, edge=True, win_bias=0.05, seed=3)
    rep = proof.evaluate(rows, PV)
    for b in rep.buckets:
        assert b.evaluated == (b.n >= 20)


def test_only_current_params_version_counts():
    rows = synth(600, edge=True, win_bias=0.05, seed=3, pv="old")
    rep = proof.evaluate(rows, PV)
    assert rep.n_settled == 0 and rep.status == "UNPROVEN"


def test_headline_reports_favourites():
    rep = proof.evaluate(synth(400, seed=1), PV)
    assert rep.headline.startswith("Market favorites priced at ") and f"(n={rep.fav_n})" in rep.headline
    assert rep.brier_const is not None


def test_proof_is_read_from_the_ledger_for_the_current_params(tmp_path):
    led = Ledger(":memory:")
    assert proof.current(led, PV).status == "UNPROVEN"


def test_demo_rows_never_count_as_proof_evidence():
    rows = synth(3000, edge=True, win_bias=0.05, seed=3)
    assert proof.evaluate(rows, PV).status == "PROVEN"
    for r in rows:
        r["mode"] = "DEMO"
    rep = proof.evaluate(rows, PV)
    assert rep.n_settled == 0 and rep.status == "UNPROVEN"
