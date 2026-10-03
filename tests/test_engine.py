import os

import pytest

import btc15.engine as eng
from btc15.caller import trade_caller
from btc15.engine import AppState, Engine
from btc15.executor import ExecResult, Executor, Halt
from btc15.feeds import FeedBook
from btc15.kalshi import KalshiError
from btc15.ledger import Ledger
from conftest import FakeKalshi, base_state, make_cfg, make_market

NOW = 1300.0


def build(tmp_path, mode="PAPER", fake=None, **cfg_kw):
    cfg = make_cfg(MODE=mode, KILL_FILE=str(tmp_path / "KILL"), **cfg_kw)
    led = Ledger(":memory:")
    fake = fake or FakeKalshi(market=make_market(NOW))
    halt = Halt(led)
    ex = Executor(cfg, fake, led, halt)
    e = Engine(cfg, led, fake, FeedBook(), ex, halt, AppState())
    return cfg, led, fake, ex, e, halt


def script(monkeypatch, actions):
    """Replace the decision function with a scripted sequence of actions (SKIP/BUY_UP)."""
    it = iter(actions)

    def fake_caller(st, cfg):
        d = trade_caller(base_state(ticker=st.ticker, now=st.now, mode=st.mode), cfg)
        d.action = next(it)
        d.reason = "ALL_GATES_PASSED" if d.action != "SKIP" else "MIN_EDGE"
        return d
    monkeypatch.setattr(eng, "trade_caller", fake_caller)


class CountingExecutor:
    def __init__(self):
        self.calls = 0
        self.bankroll = 1000.0

    async def execute(self, d, m):
        self.calls += 1
        return ExecResult(True, 1.0, d.price, 0.02, None, "x")

    async def reconcile_positions(self, now): return []
    async def cancel_all_resting(self): return 0


async def test_acts_only_on_skip_to_buy_transition_and_only_once_per_market(tmp_path, monkeypatch):
    cfg, led, fake, ex, e, halt = build(tmp_path)
    cx = CountingExecutor()
    e.executor = cx
    script(monkeypatch, ["SKIP", "BUY_UP", "BUY_UP", "SKIP", "BUY_UP", "BUY_UP"])
    for i in range(6):
        await e.tick(NOW + i)
    assert cx.calls == 1                                                        # second SKIP->BUY on same market ignored
    assert led.get_row("KXBTC15M-26OCT031200-00", "PAPER")["kind"] == "BUY"


async def test_first_ever_decision_being_buy_is_not_a_transition(tmp_path, monkeypatch):
    cfg, led, fake, ex, e, halt = build(tmp_path)
    cx = CountingExecutor()
    e.executor = cx
    script(monkeypatch, ["BUY_UP", "BUY_UP", "BUY_UP"])
    for i in range(3):
        await e.tick(NOW + i)
    assert cx.calls == 0


async def test_shadow_forecast_row_is_snapshotted_at_300s_when_there_is_no_buy(tmp_path, monkeypatch):
    cfg, led, fake, ex, e, halt = build(tmp_path)
    e.executor = CountingExecutor()
    script(monkeypatch, ["SKIP"] * 3)
    await e.tick(NOW - 100)            # elapsed 200: too early
    assert led.get_row("KXBTC15M-26OCT031200-00", "PAPER") is None
    await e.tick(NOW)                  # elapsed 300
    await e.tick(NOW + 1)
    row = led.get_row("KXBTC15M-26OCT031200-00", "PAPER")
    assert row["kind"] == "SHADOW" and row["action"] == "SKIP" and row["p"] is not None


async def test_kill_switch_cancels_resting_orders_and_halts(tmp_path):
    fake = FakeKalshi(fill="none", market=make_market(NOW))
    cfg, led, fake, ex, e, halt = build(tmp_path, mode="DEMO", fake=fake)
    await fake.place_order("KXBTC15M-26OCT031200-00", "bid", 3, 0.66, "c1")
    await fake.place_order("KXBTC15M-26OCT031200-00", "ask", 2, 0.30, "c2")
    assert len([o for o in fake.orders.values() if o["status"] == "resting"]) == 2
    await e.kill("test")
    assert sorted(fake.cancelled) == ["ord1", "ord2"]
    assert halt.reason.startswith("KILL")
    assert all(o["status"] == "canceled" for o in fake.orders.values())


async def test_kill_file_is_honoured_by_the_loop_and_blocks_entries(tmp_path, monkeypatch):
    fake = FakeKalshi(fill="none", market=make_market(NOW))
    cfg, led, fake, ex, e, halt = build(tmp_path, mode="DEMO", fake=fake)
    await fake.place_order("KXBTC15M-26OCT031200-00", "bid", 3, 0.66, "c1")
    (tmp_path / "KILL").write_text("")
    await e.tick(NOW)
    assert fake.cancelled == ["ord1"] and halt.reason.startswith("KILL")
    n_placed = len(fake.placed)
    script(monkeypatch, ["SKIP", "BUY_UP"])
    for i in (1, 2):
        await e.tick(NOW + i)                    # even a (scripted) BUY transition reaches the executor ...
    assert len(fake.placed) == n_placed          # ... which refuses while halted: nothing new is placed


async def test_kill_request_from_dashboard_writes_file_and_halts(tmp_path):
    cfg, led, fake, ex, e, halt = build(tmp_path)
    e.request_kill("dashboard")
    assert os.path.exists(cfg.KILL_FILE)
    await e.tick(NOW)
    assert halt.reason and "dashboard" in halt.reason


async def test_kill_file_keeps_system_halted_even_if_db_halt_is_cleared(tmp_path):
    cfg, led, fake, ex, e, halt = build(tmp_path)
    (tmp_path / "KILL").write_text("")
    await e.tick(NOW)
    led.clear_halt()                                                            # operator forgot to delete the file
    await e.tick(NOW + 1)
    assert halt.reason


async def test_three_consecutive_api_errors_halt(tmp_path):
    cfg, led, fake, ex, e, halt = build(tmp_path)
    fake.consecutive_errors = 2
    await e.tick(NOW)
    assert not halt.reason
    fake.consecutive_errors = 3
    await e.tick(NOW + 1)
    assert halt.reason.startswith("API_ERRORS")


async def test_halt_survives_restart_until_reset(tmp_path):
    cfg, led, fake, ex, e, halt = build(tmp_path)
    halt.halt("RECONCILIATION_MISMATCH x")
    assert Halt(led).reason == "RECONCILIATION_MISMATCH x"                      # same DB, new object == restart
    led.clear_halt()
    assert Halt(led).reason is None


async def test_settlement_updates_ledger_with_official_result(tmp_path):
    cfg, led, fake, ex, e, halt = build(tmp_path)
    d = trade_caller(base_state(), cfg).to_dict()
    led.record_decision(d, "BUY", 1000.0, 1900.0)
    led.update_row(d["ticker"], "PAPER", filled_contracts=5.0, fill_price=0.66, fee_paid=0.08)
    fake.market = make_market(NOW, status="finalized", result="yes", expiration_value=100010.5)
    assert await e.settle_pending(2000.0) == 1
    row = led.get_row(d["ticker"], "PAPER")
    assert row["result"] == "yes" and row["settle_source"] == "kalshi_market"
    assert row["pnl"] == pytest.approx(5 * 0.34 - 0.08)
    assert await e.settle_pending(2001.0) == 0                                  # idempotent


async def test_market_data_errors_do_not_crash_the_tick(tmp_path):
    cfg, led, fake, ex, e, halt = build(tmp_path)

    async def boom():
        raise KalshiError("HTTP 500", 500, False)
    fake.open_markets = boom
    assert await e.tick(NOW) is None


async def test_composite_history_survives_a_restart_so_warmup_is_not_lost(tmp_path):
    cfg, led, fake, ex, e, halt = build(tmp_path)
    for s in range(1000, 1600):
        e.book.series[s] = 100000.0 + s % 7
    e.persist_series(1600.0)
    e2 = Engine(cfg, led, fake, FeedBook(), ex, halt, AppState())               # "restart": new process, same DB
    e2.restore_series(1600.0)
    assert len(e2.book.series) == 600 and e2.book.series[1234] == 100000.0 + 1234 % 7
    e2.persist_series(1601.0)                                                   # nothing new: no rewrite needed
    assert led.load_series(0).keys() == e.book.series.keys()


async def test_readiness_explains_what_is_missing_and_gives_an_eta(tmp_path):
    cfg, led, fake, ex, e, halt = build(tmp_path)
    await e.tick(NOW)
    r = e.app.readiness
    assert r["ready"] is False and r["predicting"] is False
    names = {c["name"]: c["ok"] for c in r["checks"]}
    assert names["history"] is False and names["feeds"] is False and names["not halted"] is True
    assert r["history_eta_sec"] == int(1800 * 0.9)
    for n in ("coinbase", "kraken", "bitstamp"):
        e.book.on_message(n, 100000.0, now=NOW)
    for s in range(int(NOW) - 1700, int(NOW)):
        e.book.series[s] = 100000.0
    assert e.readiness(NOW + 0.5, True)["checks"][1]["ok"] is True and e.readiness(NOW + 0.5, True)["history_eta_sec"] == 0
