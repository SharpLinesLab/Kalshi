"""CLI: run | report | kill | reset-halt | backfill-results"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from . import proof as proofmod
from .config import ConfigError, Settings, validate_for_start
from .kalshi import KalshiClient, Signer
from .ledger import Ledger

log = logging.getLogger("btc15")


def _client(cfg: Settings) -> KalshiClient:
    signer = Signer(cfg.KALSHI_KEY_ID, cfg.KALSHI_PRIVATE_KEY_PATH) \
        if cfg.MODE in ("DEMO", "LIVE") and cfg.KALSHI_KEY_ID and cfg.KALSHI_PRIVATE_KEY_PATH else None
    return KalshiClient(cfg.rest_base, signer)


async def _run(cfg: Settings) -> None:
    import uvicorn
    from .dashboard import create_app
    from .engine import AppState, Engine
    from .executor import Executor, Halt
    from .feeds import FEEDS, FeedBook, run_feed

    ledger = Ledger(cfg.DB_PATH)
    pv = cfg.params_version()
    status = proofmod.current(ledger, pv).status
    validate_for_start(cfg, status)                               # raises ConfigError -> refuse to start
    if cfg.MODE == "LIVE":
        log.warning("=" * 60 + "\n  LIVE MODE — REAL MONEY. proof=%s max_contracts=%s\n" + "=" * 60,
                    status, cfg.effective_max_contracts(status))
    client = _client(cfg)
    halt, book, app_state = Halt(ledger), FeedBook(), AppState(mode=cfg.MODE)
    if os.path.exists(cfg.KILL_FILE):
        halt.halt("KILL file present at startup")
    executor = Executor(cfg, client, ledger, halt)
    engine = Engine(cfg, ledger, client, book, executor, halt, app_state)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    app = create_app(cfg, app_state, ledger, engine)
    server = uvicorn.Server(uvicorn.Config(app, host=cfg.DASHBOARD_HOST, port=cfg.DASHBOARD_PORT, log_level="warning"))
    log.info("btc15 starting mode=%s params_version=%s proof=%s dashboard=:%s", cfg.MODE, pv, status, cfg.DASHBOARD_PORT)
    tasks = [asyncio.create_task(run_feed(n, book, stop)) for n in FEEDS]
    tasks.append(asyncio.create_task(engine.run(stop)))
    web = asyncio.create_task(server.serve())
    waiter = asyncio.create_task(stop.wait())
    await asyncio.wait([waiter, web], return_when=asyncio.FIRST_COMPLETED)   # uvicorn may own SIGTERM
    stop.set()
    server.should_exit = True                                     # graceful web shutdown first
    await asyncio.wait([web], timeout=5)
    waiter.cancel()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await client.aclose()


def cmd_run(cfg: Settings) -> int:
    try:
        asyncio.run(_run(cfg))
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return 2
    return 0


def cmd_report(cfg: Settings) -> int:
    ledger = Ledger(cfg.DB_PATH)
    rep = proofmod.current(ledger, cfg.params_version())
    print(f"params_version {rep.params_version}  status {rep.status}")
    print(rep.headline)
    print(f"settled={rep.n_settled} trades={rep.n_trades} brier_p={rep.brier_p} brier_mkt={rep.brier_mkt} "
          f"brier_const={rep.brier_const} roi={rep.roi} pnl={rep.pnl:.2f}")
    for b in rep.buckets:
        print(f"  {b.label:>7}  n={b.n:<4} p_mkt={b.avg_p_mkt} p={b.avg_p} win={b.win_rate} gap={b.gap} ok={b.ok}")
    for f in rep.failing:
        print("  - " + f)
    return 0


def cmd_kill(cfg: Settings) -> int:
    open(cfg.KILL_FILE, "a").close()
    Ledger(cfg.DB_PATH).set_halt("KILL: cli")
    print("KILL file written and HALT set; a running service cancels resting orders within ~1s.")
    return 0


def cmd_reset_halt(cfg: Settings) -> int:
    ledger = Ledger(cfg.DB_PATH)
    was = ledger.halt_reason()
    if os.path.exists(cfg.KILL_FILE):
        os.remove(cfg.KILL_FILE)
    ledger.clear_halt()
    print(f"halt cleared (was: {was})" if was else "no halt was set")
    return 0


def cmd_backfill(cfg: Settings) -> int:
    from .engine import backfill_results

    async def go() -> int:
        c = _client(cfg)
        try:
            return await backfill_results(Ledger(cfg.DB_PATH), c, cfg.MODE)
        finally:
            await c.aclose()
    print(f"settled {asyncio.run(go())} rows")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="btc15")
    ap.add_argument("command", choices=["run", "report", "kill", "reset-halt", "backfill-results"])
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)       # httpx INFO lines would print full URLs
    cfg = Settings()
    return {"run": cmd_run, "report": cmd_report, "kill": cmd_kill, "reset-halt": cmd_reset_halt,
            "backfill-results": cmd_backfill}[a.command](cfg)


if __name__ == "__main__":
    sys.exit(main())
