import base64

import pytest
from fastapi.testclient import TestClient

from btc15.dashboard import create_app
from btc15.engine import AppState, Engine
from btc15.executor import Executor, Halt
from btc15.feeds import FeedBook
from btc15.ledger import Ledger
from conftest import FakeKalshi, make_cfg

PW = "correct-horse-battery"


def make(tmp_path, mode="PAPER"):
    cfg = make_cfg(MODE=mode, DASHBOARD_PASSWORD=PW, KILL_FILE=str(tmp_path / "KILL"))
    led = Ledger(":memory:")
    halt = Halt(led)
    eng = Engine(cfg, led, FakeKalshi(), FeedBook(), Executor(cfg, FakeKalshi(), led, halt), halt, AppState())
    return TestClient(create_app(cfg, eng.app, led, eng)), eng, cfg


def auth(pw=PW, user="admin"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


ROUTES = [("GET", "/"), ("GET", "/api/state"), ("GET", "/calibration"), ("GET", "/export.csv"), ("GET", "/healthz"),
          ("POST", "/kill"), ("GET", "/docs"), ("GET", "/openapi.json"), ("GET", "/does-not-exist")]


@pytest.mark.parametrize("method,path", ROUTES)
def test_every_route_requires_basic_auth(tmp_path, method, path):
    c, eng, _ = make(tmp_path)
    assert c.request(method, path).status_code == 401
    assert c.request(method, path, headers=auth("wrong")).status_code == 401
    assert c.request(method, path, headers=auth(user="root")).status_code == 401
    assert not eng.kill_requested                                             # unauthenticated /kill did nothing


def test_authenticated_pages_render_with_a_mode_banner(tmp_path):
    for mode, text in (("PAPER", "PAPER"), ("DEMO", "DEMO"), ("LIVE", "LIVE — REAL MONEY")):
        c, _, _ = make(tmp_path, mode)
        r = c.get("/", headers=auth())
        assert r.status_code == 200 and text in r.text and f'class="banner {mode.lower()}"' in r.text
        assert c.get("/calibration", headers=auth()).status_code == 200
        assert c.get("/export.csv", headers=auth()).headers["content-type"].startswith("text/csv")
        st = c.get("/api/state", headers=auth()).json()
        assert st["mode"] == mode and "feeds" in st and "params_version" in st


def test_authenticated_kill_requests_the_kill_switch(tmp_path):
    c, eng, cfg = make(tmp_path)
    r = c.post("/kill", headers=auth())
    assert r.status_code == 200 and eng.kill_requested == "dashboard"


def test_cross_origin_kill_is_refused(tmp_path):
    c, eng, _ = make(tmp_path)
    r = c.post("/kill", headers={**auth(), "Origin": "https://evil.example", "Host": "dash.local"})
    assert r.status_code == 403 and not eng.kill_requested


def test_secrets_never_appear_in_state(tmp_path):
    c, _, _ = make(tmp_path)
    body = c.get("/api/state", headers=auth()).text + c.get("/", headers=auth()).text
    assert PW not in body and "KALSHI_PRIVATE_KEY" not in body
