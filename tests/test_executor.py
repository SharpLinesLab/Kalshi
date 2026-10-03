import pytest

from btc15 import model
from btc15.caller import trade_caller
from btc15.executor import Executor, Halt
from btc15.kalshi import KalshiError
from btc15.ledger import Ledger
from conftest import FakeKalshi, base_state, make_cfg, make_market


class Clock:
    def __init__(self):
        self.t = 1300.0

    def now(self):
        return self.t

    async def sleep(self, s):
        self.t += s


def setup(mode="DEMO", fill="full", **state_kw):
    cfg = make_cfg(MODE=mode)
    led = Ledger(":memory:")
    fake = FakeKalshi(fill=fill)
    clock = Clock()
    halt = Halt(led)
    ex = Executor(cfg, fake, led, halt, sleep=clock.sleep, clock=clock.now)
    d = trade_caller(base_state(mode=mode, **state_kw), cfg)
    assert d.action in ("BUY_UP", "BUY_DOWN"), d.reason
    led.record_decision(d.to_dict(), "BUY", 1000.0, 1900.0)
    return cfg, led, fake, ex, d, clock, halt


async def test_paper_simulates_a_full_fill_at_the_ask_and_sends_no_orders():
    cfg, led, fake, ex, d, *_ = setup(mode="PAPER")
    res = await ex.execute(d, make_market())
    assert res.ok and res.filled == d.contracts and res.fill_price == d.price and fake.placed == []
    row = led.get_row(d.ticker, "PAPER")
    assert row["filled_contracts"] == d.contracts and row["fee_paid"] == model.fee(d.price, d.contracts)


async def test_buy_up_is_a_limit_bid_at_the_ask_and_reconciles():
    cfg, led, fake, ex, d, *_ = setup()
    res = await ex.execute(d, make_market())
    assert res.ok and not led.halt_reason()
    assert fake.placed == [{"ticker": d.ticker, "book_side": "bid", "count": d.contracts, "price": 0.66}]
    row = led.get_row(d.ticker, "DEMO")
    assert row["order_id"] == "ord1" and row["filled_contracts"] == d.contracts and row["fill_price"] == pytest.approx(0.66)
    assert fake.positions[d.ticker] == d.contracts and ex.bankroll == pytest.approx(fake.balance)


async def test_buy_down_is_sell_yes_at_one_minus_no_ask_and_holds_a_no_position():
    cfg, led, fake, ex, d, *_ = setup(composite=99_750.0, yes_bid=0.34, yes_ask=0.36, no_bid=0.64, no_ask=0.66)
    assert d.action == "BUY_DOWN"
    res = await ex.execute(d, make_market())
    assert res.ok and not led.halt_reason()
    assert fake.placed[0]["book_side"] == "ask" and fake.placed[0]["price"] == pytest.approx(0.34)
    assert fake.positions[d.ticker] == -d.contracts                         # short YES == long NO
    assert led.get_row(d.ticker, "DEMO")["fill_price"] == pytest.approx(0.66)


async def test_unfilled_order_is_cancelled_after_10s_and_recorded_as_zero_fill():
    cfg, led, fake, ex, d, clock, halt = setup(fill="none")
    t0 = clock.t
    res = await ex.execute(d, make_market())
    assert fake.cancelled == ["ord1"] and clock.t - t0 >= 10.0
    assert res.ok and res.filled == 0 and not led.halt_reason()
    assert led.get_row(d.ticker, "DEMO")["filled_contracts"] == 0


async def test_partial_fill_is_recorded_and_remainder_cancelled():
    cfg, led, fake, ex, d, clock, halt = setup(fill="partial")
    assert d.contracts >= 2
    res = await ex.execute(d, make_market())
    assert fake.cancelled == ["ord1"] and not led.halt_reason()
    row = led.get_row(d.ticker, "DEMO")
    assert row["filled_contracts"] == d.contracts // 2 and res.filled == d.contracts // 2
    assert fake.positions[d.ticker] == d.contracts // 2


async def test_position_mismatch_after_an_order_halts():
    cfg, led, fake, ex, d, *_ = setup()
    fake.skew_position = 1.0                                                  # API shows a contract we did not record
    res = await ex.execute(d, make_market())
    assert not res.ok and "RECONCILIATION_MISMATCH" in led.halt_reason() and "position" in led.halt_reason()
    assert led.get_row(d.ticker, "DEMO")["filled_contracts"] == d.contracts   # ledger still records what the API did


async def test_balance_mismatch_halts():
    cfg, led, fake, ex, d, *_ = setup()
    orig = fake.get_balance
    calls = {"n": 0}

    async def shifty():
        calls["n"] += 1
        return await orig() - (5.0 if calls["n"] >= 2 else 0.0)            # money vanished between before/after
    fake.get_balance = shifty
    await ex.execute(d, make_market())
    assert "balance" in led.halt_reason()


async def test_fee_that_disagrees_with_the_formula_halts():
    cfg, led, fake, ex, d, *_ = setup()
    orig = fake.get_fills

    async def bad_fees(oid):
        fs = await orig(oid)
        for f in fs:
            f["fee_cost"] = "0.2500"
        return fs
    fake.get_fills = bad_fees
    await ex.execute(d, make_market())
    assert "fee" in led.halt_reason()


async def test_existing_api_position_blocks_a_second_order():
    cfg, led, fake, ex, d, *_ = setup()
    fake.positions[d.ticker] = 1.0
    res = await ex.execute(d, make_market())
    assert not res.ok and fake.placed == [] and not led.halt_reason()


async def test_definite_rejection_places_nothing_and_does_not_halt():
    cfg, led, fake, ex, d, *_ = setup()
    fake.fail_place = KalshiError("HTTP 400", 400, definite=True)
    res = await ex.execute(d, make_market())
    assert not res.ok and not led.halt_reason() and led.get_row(d.ticker, "DEMO")["filled_contracts"] == 0


async def test_ambiguous_timeout_recovers_the_order_by_client_order_id():
    cfg, led, fake, ex, d, *_ = setup()
    fake.fail_place = KalshiError("timeout", None, definite=False)           # order DID land (see FakeKalshi)
    res = await ex.execute(d, make_market())
    assert res.ok and res.order_id == "ord1" and not led.halt_reason()
    assert fake.positions[d.ticker] == d.contracts


async def test_ambiguous_timeout_with_unfindable_order_halts():
    cfg, led, fake, ex, d, *_ = setup()
    fake.fail_place = KalshiError("timeout", None, definite=False)
    fake.hide_orders = True                                                   # cannot find it -> must not guess
    res = await ex.execute(d, make_market())
    assert not res.ok and "UNKNOWN" in led.halt_reason()      # unknown order/position: never guess, halt


async def test_off_grid_price_is_never_sent():
    cfg, led, fake, ex, d, *_ = setup()
    d.price = 0.665
    res = await ex.execute(d, make_market())
    assert not res.ok and fake.placed == []


async def test_a_halted_system_places_nothing():
    cfg, led, fake, ex, d, clock, halt = setup()
    halt.halt("KILL: test")
    res = await ex.execute(d, make_market())
    assert not res.ok and fake.placed == []


async def test_non_series_ticker_is_refused_by_the_executor_too():
    cfg, led, fake, ex, d, *_ = setup()
    d.ticker = "KXMVESPORTS-1"
    res = await ex.execute(d, make_market())
    assert not res.ok and fake.placed == []


# ------------------------------------------------------------ periodic reconciliation
def open_trade(led, ticker="KXBTC15M-26OCT031200-00", side="yes", n=3, close=5000.0):
    d = trade_caller(base_state(ticker=ticker), make_cfg(MODE="DEMO")).to_dict()
    d["mode"], d["side"] = "DEMO", side
    led.record_decision(d, "BUY", 1000.0, close)
    led.update_row(ticker, "DEMO", filled_contracts=float(n), fill_price=0.66, fee_paid=0.05, order_id="o")


async def test_reconcile_passes_when_api_matches_ledger():
    cfg, led, fake, ex, *_ = setup()
    led.db.execute("DELETE FROM markets")
    open_trade(led)
    fake.positions["KXBTC15M-26OCT031200-00"] = 3.0
    assert await ex.reconcile_positions(1500.0) == [] and not led.halt_reason()


async def test_reconcile_halts_when_api_has_an_unknown_position():
    cfg, led, fake, ex, *_ = setup()
    led.db.execute("DELETE FROM markets")
    fake.positions["KXBTC15M-OTHER"] = 2.0
    assert await ex.reconcile_positions(1500.0)
    assert "RECONCILIATION_MISMATCH" in led.halt_reason()


async def test_reconcile_halts_when_ledger_trade_is_missing_from_api():
    cfg, led, fake, ex, *_ = setup()
    led.db.execute("DELETE FROM markets")
    open_trade(led)
    assert await ex.reconcile_positions(1500.0)
    assert "RECONCILIATION_MISMATCH" in led.halt_reason()


async def test_reconcile_halts_on_side_or_size_mismatch():
    cfg, led, fake, ex, *_ = setup()
    led.db.execute("DELETE FROM markets")
    open_trade(led, side="no", n=3)
    fake.positions["KXBTC15M-26OCT031200-00"] = 3.0                          # ledger says NO (-3)
    assert await ex.reconcile_positions(1500.0)


async def test_reconcile_ignores_markets_already_closed_and_settling():
    cfg, led, fake, ex, *_ = setup()
    led.db.execute("DELETE FROM markets")
    open_trade(led, close=1400.0)
    assert await ex.reconcile_positions(1500.0) == [] and not led.halt_reason()
