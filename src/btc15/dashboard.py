"""Password-protected FastAPI dashboard. HTTP basic auth on EVERY route."""
from __future__ import annotations

import base64
import csv
import io
import secrets
import time
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from .config import Settings
from .engine import AppState, Engine
from .ledger import Ledger
from . import proof as proofmod

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def create_app(cfg: Settings, app_state: AppState, ledger: Ledger, engine: Engine) -> FastAPI:
    def authorised(header: str | None) -> bool:
        if not header or not header.lower().startswith("basic ") or cfg.DASHBOARD_PASSWORD is None:
            return False
        try:
            user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
        except Exception:
            return False
        return secrets.compare_digest(user.encode(), cfg.DASHBOARD_USER.encode()) and \
            secrets.compare_digest(pw.encode(), cfg.DASHBOARD_PASSWORD.encode())

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def basic_auth(request: Request, call_next):          # EVERY request, including unknown paths
        if not authorised(request.headers.get("authorization")):
            return PlainTextResponse("Unauthorized", status_code=401, headers={"WWW-Authenticate": 'Basic realm="btc15"'})
        return await call_next(request)

    def view() -> dict:
        a = app_state
        return {"mode": cfg.MODE, "now": time.time(), "decision": a.decision, "market": a.market, "feeds": a.feeds,
                "clone_suspect": a.clone_suspect, "daily_pnl": a.daily_pnl, "total_pnl": a.total_pnl,
                "open_position": a.open_position, "halted": a.halted, "proof": a.proof, "basis": a.basis,
                "composite": a.composite, "bankroll": a.bankroll, "params_version": cfg.params_version()}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return TEMPLATES.TemplateResponse(request, "index.html", {"mode": cfg.MODE})

    @app.get("/api/state")
    def api_state():
        return JSONResponse(view())

    @app.get("/healthz")
    def healthz():
        return PlainTextResponse("ok")

    @app.post("/kill")
    def kill(request: Request):
        origin = request.headers.get("origin")                      # CSRF guard: same-origin only
        if origin and request.headers.get("host") and origin.split("://", 1)[-1] != request.headers["host"]:
            raise HTTPException(403, "cross-origin request refused")
        engine.request_kill("dashboard")
        return {"ok": True, "message": "kill requested: entries stopped, resting orders cancelling, HALT"}

    @app.get("/calibration", response_class=HTMLResponse)
    def calibration(request: Request):
        rep = proofmod.current(ledger, cfg.params_version())
        all_rep = proofmod.evaluate(ledger.settled_rows(), cfg.params_version())
        by_mode = ledger.db.execute(
            "SELECT mode, COUNT(*) n, SUM(CASE WHEN filled_contracts>0 THEN 1 ELSE 0 END) trades, "
            "COALESCE(SUM(pnl),0) pnl, COALESCE(SUM(CASE WHEN filled_contracts>0 THEN filled_contracts*fill_price END),0) staked "
            "FROM markets WHERE result IN ('yes','no') GROUP BY mode").fetchall()
        modes = [{"mode": r["mode"], "n": r["n"], "trades": r["trades"], "pnl": r["pnl"],
                  "roi": (r["pnl"] / r["staked"]) if r["staked"] else None} for r in by_mode]
        return TEMPLATES.TemplateResponse(request, "calibration.html", {
            "mode": cfg.MODE, "rep": rep, "modes": modes, "all_n": all_rep.n_settled, "pv": cfg.params_version()})

    @app.get("/export.csv")
    def export_csv():
        rows = ledger.recent_rows(100000)
        buf = io.StringIO()
        if rows:
            w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                                 headers={"Content-Disposition": 'attachment; filename="btc15_ledger.csv"'})

    return app
