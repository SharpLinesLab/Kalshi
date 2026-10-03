import pytest

from btc15.caller import GATE_ORDER, trade_caller
from conftest import base_state, make_cfg


def run(cfg=None, **kw):
    return trade_caller(base_state(**kw), cfg or make_cfg())


def test_baseline_buys_up_with_all_gates_passing():
    d = run()
    assert d.action == "BUY_UP" and d.reason == "ALL_GATES_PASSED"
    assert [g.name for g in d.gates] == list(GATE_ORDER)
    assert all(g.passed for g in d.gates)
    assert d.contracts >= 1 and d.price == 0.66 and d.side == "yes"
    assert d.max_loss == pytest.approx(d.contracts * 0.66 + d.order_fee)
    assert d.max_payout == d.contracts
    assert d.params_version == make_cfg().params_version()


def test_buys_down_when_the_down_edge_is_larger():
    d = run(composite=99_750.0, yes_bid=0.34, yes_ask=0.36, no_bid=0.64, no_ask=0.66)
    assert d.action == "BUY_DOWN" and d.side == "no" and d.price == 0.66
    assert d.edge_dn > d.edge_up


def test_sizing_is_quarter_kelly_capped_at_2pct_of_bankroll():
    d = run(bankroll=1000.0, composite=100_250.0)
    assert d.contracts == min(5, int(0.02 * 1000 // 0.66))                # stake capped at $20 -> 30 contracts; cfg cap 5
    big = run(make_cfg(MAX_CONTRACTS_PER_TRADE=1000), bankroll=1000.0)
    assert big.contracts == int(0.02 * 1000 // 0.66)


# every gate must block on exactly its own condition ------------------------------------------------
CASES = {
    "DATA_FRESH": [dict(ref_ts=1296.0), dict(quote_ts=1296.0), dict(strike=None), dict(basis=None), dict(composite=None),
                   dict(yes_ask=None)],
    "REF_DEGRADED": [dict(n_fresh_feeds=2), dict(n_fresh_feeds=0)],
    "DATA_GAPS": [dict(gap_pct=0.11)],
    "VOL_SANITY": [dict(realized_vol=0.02), dict(realized_vol=2.6), dict(realized_vol=None)],
    "SPREAD": [dict(yes_ask=0.60, no_ask=0.36), dict(yes_ask=0.80, no_ask=0.36)],
    "ENTRY_WINDOW": [dict(now=1100.0, quote_ts=1099.5, ref_ts=1099.8), dict(now=1700.0, quote_ts=1699.5, ref_ts=1699.8)],
    "MIN_EDGE": [dict(composite=100_150.0)],
    "DIVERGENCE": [dict(composite=100_300.0, yes_bid=0.64, yes_ask=0.66)],
    "EDGE_SUSPECT": [dict(composite=100_600.0, yes_bid=0.55, yes_ask=0.57, no_bid=0.43, no_ask=0.45)],
    "SIZE": [dict(bankroll=10.0)],
    "ONE_PER_MARKET": [dict(has_position_or_order=True)],
    "NO_COMBOS": [dict(ticker="KXMVECROSSCATEGORY-26OCT031200-AB"), dict(ticker="KXETH15M-26OCT031200-00")],
    "HALTED": [dict(halted="KILL: test")],
}


@pytest.mark.parametrize("gate,overrides", [(g, o) for g, lst in CASES.items() for o in lst])
def test_each_gate_blocks_on_its_condition(gate, overrides):
    d = run(**overrides)
    assert d.action == "SKIP"
    assert d.gate(gate).passed is False, d.gate(gate).detail
    others = [g.name for g in d.gates if not g.passed and g.name != gate]
    if gate in ("DATA_FRESH", "VOL_SANITY", "SIZE", "DIVERGENCE", "MIN_EDGE", "EDGE_SUSPECT", "SPREAD", "ENTRY_WINDOW"):
        return                       # these legitimately cascade into dependent gates (e.g. no edge without data)
    assert others == [], others      # no collateral failures: the condition is isolated


def test_daily_and_total_loss_gates_are_worst_case():
    cfg = make_cfg(DAILY_LOSS_LIMIT_USD=50.0, MAX_TOTAL_LOSS_USD=200.0)
    assert run(cfg, daily_pnl=-10.0).gate("DAILY_LOSS").passed
    d = run(cfg, daily_pnl=-47.0)                       # -47 and this trade could lose ~$3.4 more -> breaches $50
    assert not d.gate("DAILY_LOSS").passed and d.action == "SKIP" and d.reason == "DAILY_LOSS"
    assert not run(cfg, daily_pnl=-30.0, open_exposure=25.0).gate("DAILY_LOSS").passed     # open exposure counts
    assert run(cfg, total_pnl=-100.0).gate("TOTAL_LOSS").passed
    assert not run(cfg, total_pnl=-198.0).gate("TOTAL_LOSS").passed
    assert run(cfg, total_pnl=-198.0).reason == "TOTAL_LOSS"


def test_first_failing_gate_in_spec_order_is_the_reason():
    d = run(gap_pct=0.5, halted="X", ticker="KXMVE-1")
    assert d.reason == "DATA_GAPS"


def test_single_feed_allowed_only_when_configured_and_labelled():
    assert not run(n_fresh_feeds=1).gate("REF_DEGRADED").passed
    d = run(make_cfg(REQUIRE_3_FEEDS=False), n_fresh_feeds=1)
    assert d.gate("REF_DEGRADED").passed and d.ref_label == "SINGLE_FEED" and d.action == "BUY_UP"
    assert not run(make_cfg(REQUIRE_3_FEEDS=False), n_fresh_feeds=0).gate("REF_DEGRADED").passed


def test_no_combos_rejects_kxmve_and_accepts_the_series():
    assert not run(ticker="KXMVESPORTSMULTIGAMEEXTENDED-S2025-ABC").gate("NO_COMBOS").passed
    assert run(ticker="KXBTC15M-26OCT031200-00").gate("NO_COMBOS").passed


def test_unproven_live_is_forced_to_one_contract():
    cfg = make_cfg(MODE="LIVE", MAX_CONTRACTS_PER_TRADE=5, ALLOW_UNPROVEN="yes")
    d = run(cfg, mode="LIVE", proof_status="UNPROVEN")
    assert d.action == "BUY_UP" and d.contracts == 1
    p = run(cfg, mode="LIVE", proof_status="PROVEN")
    assert p.contracts == 5


def test_unproven_cap_does_not_apply_outside_live():
    d = run(make_cfg(MODE="PAPER", MAX_CONTRACTS_PER_TRADE=5), proof_status="UNPROVEN")
    assert d.contracts == 5


def test_live_unproven_without_allow_flag_is_halted_at_decision_time():
    cfg = make_cfg(MODE="LIVE", ALLOW_UNPROVEN=None)
    d = run(cfg, mode="LIVE", proof_status="UNPROVEN")
    assert d.action == "SKIP" and d.reason == "HALTED"


def test_decision_is_deterministic_and_pure():
    a, b = run(), run()
    assert a.to_dict() == b.to_dict()


def test_skip_still_carries_a_shadow_forecast():
    d = run(now=1100.0, quote_ts=1099.5, ref_ts=1099.8)   # outside the entry window
    assert d.action == "SKIP" and d.p is not None and d.p_mkt == pytest.approx(0.65) and d.edge_up is not None
