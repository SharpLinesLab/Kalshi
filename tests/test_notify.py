import asyncio
import json
import logging

import httpx
import pytest

import btc15.engine as eng
from btc15.caller import trade_caller
from btc15.config import ConfigError, validate_for_start
from btc15.engine import AppState, Engine
from btc15.executor import ExecResult, Halt
from btc15.feeds import FeedBook
from btc15.ledger import Ledger
from btc15.notify import Notifier, valid_webhook
from conftest import FakeKalshi, base_state, make_cfg, make_market

URL = "https://discord.com/api/webhooks/123456789012345678/SECRET-TOKEN-abcdefghijklmnop"


def capture(status=204):
    sent = []

    def handler(req: httpx.Request):
        sent.append((str(req.url), json.loads(req.content)))
        return httpx.Response(status)
    return sent, httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def drain(n: Notifier):
    if n._tasks:
        await asyncio.wait(n._tasks)


def decision(**kw):
    return trade_caller(base_state(**kw), make_cfg())


def test_only_discord_webhook_urls_are_accepted():
    assert valid_webhook(URL)
    for bad in ("http://discord.com/api/webhooks/1/x", "https://evil.example/api/webhooks/1/abcdefghij",
                "https://discord.com/", "", "ftp://x"):
        assert not valid_webhook(bad)
    assert not Notifier("https://evil.example/x").enabled and not Notifier(None).enabled


def test_invalid_webhook_blocks_startup_but_unset_is_fine():
    ok = dict(DASHBOARD_PASSWORD="longenough1")
    validate_for_start(make_cfg(**ok), "UNPROVEN")
    validate_for_start(make_cfg(DISCORD_WEBHOOK_URL=URL, **ok), "UNPROVEN")
    with pytest.raises(ConfigError, match="DISCORD_WEBHOOK_URL"):
        validate_for_start(make_cfg(DISCORD_WEBHOOK_URL="https://evil.example/hook", **ok), "UNPROVEN")


def test_webhook_url_does_not_change_params_version():
    assert make_cfg().params_version() == make_cfg(DISCORD_WEBHOOK_URL=URL).params_version()


async def test_buy_up_alert_has_the_call_size_risk_and_no_exit_instructions():
    sent, http = capture()
    n = Notifier(URL, http, mode="PAPER")
    d = decision()
    n.call(d, make_market())
    await drain(n)
    url, body = sent[0]
    e = body["embeds"][0]
    assert url == URL and body["allowed_mentions"] == {"parse": []}
    assert e["title"] == f"BUY UP · {d.contracts} @ 66¢"
    fields = {f["name"]: f["value"] for f in e["fields"]}
    assert fields["Side"] == "UP (YES)" and fields["Max loss"].startswith("$") and fields["Pays if right"] == f"${d.contracts:.2f}"
    assert "KXBTC15M-26OCT031200-00" in e["description"] and "Hold to settlement" in e["description"]
    assert "PAPER" in e["footer"]["text"]


async def test_buy_down_alert():
    sent, http = capture()
    n = Notifier(URL, http, mode="LIVE")
    d = decision(composite=99_750.0, yes_bid=0.34, yes_ask=0.36, no_bid=0.64, no_ask=0.66)
    assert d.action == "BUY_DOWN"
    n.call(d, make_market())
    await drain(n)
    e = sent[0][1]["embeds"][0]
    assert e["title"].startswith("BUY DOWN") and "66¢" in e["title"] and "LIVE (real money)" in e["footer"]["text"]


async def test_fill_settled_and_halt_messages():
    sent, http = capture()
    n = Notifier(URL, http)
    d = decision()
    n.fill(d, ExecResult(True, 3.0, 0.66, 0.05, "o1", "x"))
    n.fill(d, ExecResult(True, 0.0, None, 0.0, "o1", "x"))
    n.fill(d, ExecResult(False, note="rejected"))
    n.settled({"side": "yes", "result": "yes", "pnl": 1.0, "ticker": "T", "filled_contracts": 3.0, "fill_price": 0.66, "fee_paid": 0.05})
    n.settled({"side": "no", "result": "yes", "pnl": -2.0, "ticker": "T", "filled_contracts": 3.0, "fill_price": 0.66, "fee_paid": 0.05})
    n.halted("RECONCILIATION_MISMATCH x")
    await drain(n)
    titles = sorted(b["embeds"][0]["title"] for _, b in sent)
    assert titles == sorted(["Filled 3/5 @ 66¢" if d.contracts == 5 else f"Filled 3/{d.contracts} @ 66¢", "No fill",
                             "Order not placed", "WON $1.00 · YES", "LOST -$2.00 · YES", "HALTED: trading stopped"])


async def test_webhook_failures_never_raise_and_never_leak_the_url(caplog):
    caplog.set_level(logging.DEBUG)

    def boom(req):
        raise httpx.ConnectError(f"cannot connect to {req.url}")
    n = Notifier(URL, httpx.AsyncClient(transport=httpx.MockTransport(boom)))
    n.call(decision(), make_market())
    await drain(n)
    assert n.failed == 1 and n.sent == 0
    n2 = Notifier(URL, capture(500)[1])
    n2.call(decision(), make_market())
    await drain(n2)
    n3 = Notifier(URL, capture(429)[1])
    n3.call(decision(), make_market())
    await drain(n3)
    assert n2.failed == 1 and n3.failed == 1
    assert "SECRET-TOKEN" not in caplog.text and "SECRET-TOKEN" not in repr(n)


async def test_disabled_notifier_is_a_silent_noop():
    n = Notifier(None)
    n.call(decision(), make_market())
    n.halted("x")
    assert n.sent == 0 and not n._tasks


# ------------------------------------------------------------------ engine integration
NOW = 1300.0


def build(tmp_path, notifier):
    cfg = make_cfg(KILL_FILE=str(tmp_path / "KILL"))
    led = Ledger(":memory:")
    fake = FakeKalshi(market=make_market(NOW))
    halt = Halt(led)
    from btc15.executor import Executor
    e = Engine(cfg, led, fake, FeedBook(), Executor(cfg, fake, led, halt), halt, AppState(), notifier)
    return cfg, led, fake, e, halt


async def test_engine_alerts_once_per_call_on_the_transition_and_on_settlement(tmp_path, monkeypatch):
    sent, http = capture()
    n = Notifier(URL, http)
    cfg, led, fake, e, halt = build(tmp_path, n)
    it = iter(["SKIP", "BUY_UP", "BUY_UP", "BUY_UP"])

    def scripted(st, c):
        d = trade_caller(base_state(ticker=st.ticker, now=st.now), c)
        d.action = next(it)
        return d
    monkeypatch.setattr(eng, "trade_caller", scripted)
    for i in range(4):
        await e.tick(NOW + i)
    await drain(n)
    titles = [b["embeds"][0]["title"] for _, b in sent]
    assert sum(t.startswith("BUY UP") for t in titles) == 1                       # one alert, not one per second
    assert any(t.startswith("Filled") for t in titles)
    fake.market = make_market(NOW, status="finalized", result="yes", expiration_value=100010.0)
    await e.settle_pending(2000.0)
    await drain(n)
    assert any(b["embeds"][0]["title"].startswith("WON") for _, b in sent)


async def test_halt_alert_fires_once_and_keeps_the_first_reason(tmp_path):
    sent, http = capture()
    n = Notifier(URL, http)
    cfg, led, fake, e, halt = build(tmp_path, n)
    for _ in range(5):
        halt.halt("API_ERRORS: 3 consecutive")
    halt.halt("something else")
    await drain(n)
    assert len(sent) == 1 and halt.reason == "API_ERRORS: 3 consecutive"
