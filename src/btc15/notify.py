"""Discord webhook alerts. Informational only: a failing or slow webhook can never affect trading.

The webhook URL is a secret (anyone holding it can post to the channel): it is read from the environment,
never logged, and never included in error messages.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

log = logging.getLogger("btc15.notify")
# httpx logs every request URL at INFO, and a Discord webhook URL contains its secret token.
# Enforce quiet HTTP client loggers here so no caller can accidentally re-enable that leak.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)
ALLOWED_PREFIXES = ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")
GREEN, RED, GREY, AMBER, BLUE = 0x2ECC71, 0xE74C3C, 0x95A5A6, 0xF1C40F, 0x3498DB


def valid_webhook(url: str) -> bool:
    return url.startswith(ALLOWED_PREFIXES) and len(url) > len(ALLOWED_PREFIXES[0]) + 8


def _c(x: Optional[float]) -> str:
    return "–" if x is None else f"{x * 100:.0f}¢"


def _usd(x: Optional[float]) -> str:
    return "–" if x is None else f"{'-' if x < 0 else ''}${abs(x):.2f}"


def _hhmm(ts: Optional[float]) -> str:
    return "–" if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M UTC")


class Notifier:
    def __init__(self, url: Optional[str], http: Optional[httpx.AsyncClient] = None, mode: str = "PAPER"):
        self._url = url if url and valid_webhook(url) else None
        self._http = http or (httpx.AsyncClient(timeout=5.0) if self._url else None)
        self.mode = mode
        self._tasks: set[asyncio.Task] = set()
        self.sent = 0
        self.failed = 0

    def __repr__(self) -> str:
        return f"<Notifier enabled={self.enabled}>"          # never print the URL

    @property
    def enabled(self) -> bool:
        return self._url is not None

    def _label(self) -> str:
        return {"PAPER": "PAPER (simulated, no real order)", "DEMO": "DEMO (Kalshi demo exchange)",
                "LIVE": "LIVE (real money)"}.get(self.mode, self.mode)

    # ------------------------------------------------------------------ transport
    async def _post(self, embed: dict[str, Any]) -> bool:
        if not self.enabled:
            return False
        embed.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        embed["footer"] = {"text": f"btc15 · {self._label()}"}
        body = {"username": "btc15", "allowed_mentions": {"parse": []}, "embeds": [embed]}
        try:
            r = await self._http.post(self._url, json=body)
            if r.status_code == 429:                                   # rate limited: skip, never queue up
                self.failed += 1
                return False
            if r.status_code >= 300:
                log.warning("discord webhook returned HTTP %s", r.status_code)
                self.failed += 1
                return False
            self.sent += 1
            return True
        except Exception as e:                                         # type only: exceptions can embed the URL
            log.warning("discord webhook failed: %s", type(e).__name__)
            self.failed += 1
            return False

    def fire(self, embed: dict[str, Any]) -> None:
        """Fire-and-forget; safe to call from sync code inside the running loop."""
        if not self.enabled:
            return
        try:
            t = asyncio.get_running_loop().create_task(self._post(embed))
        except RuntimeError:
            return
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=5)
        if self._http is not None:
            await self._http.aclose()

    # ------------------------------------------------------------------ messages
    def call(self, d, market) -> None:
        up = d.action == "BUY_UP"
        side = "UP (YES)" if up else "DOWN (NO)"
        self.fire({
            "title": f"BUY {'UP' if up else 'DOWN'} · {d.contracts} @ {_c(d.price)}",
            "description": f"`{d.ticker}`\nStrike {market.strike:,.2f} · closes {_hhmm(market.close_ts)}\n"
                           f"Hold to settlement. No stop-loss or take-profit: the most you can lose is the stake.",
            "color": GREEN if up else RED,
            "fields": [
                {"name": "Side", "value": side, "inline": True},
                {"name": "Max loss", "value": _usd(d.max_loss), "inline": True},
                {"name": "Pays if right", "value": _usd(d.max_payout), "inline": True},
                {"name": "Model p(up) vs market", "value": f"{d.p:.3f} vs {d.p_mkt:.3f}", "inline": True},
                {"name": "Edge after fee", "value": f"{max(d.edge_up, d.edge_dn):+.3f}", "inline": True},
                {"name": "Proof", "value": d.proof_status, "inline": True},
            ]})

    def fill(self, d, res) -> None:
        if res.ok and res.filled > 0:
            title, color, text = f"Filled {res.filled:g}/{d.contracts} @ {_c(res.fill_price)}", BLUE, f"Fee {_usd(res.fee)}"
        elif res.ok:
            title, color, text = "No fill", GREY, "Order cancelled after the 10-second window; nothing was bought."
        else:
            title, color, text = "Order not placed", AMBER, res.note or "see logs"
        self.fire({"title": title, "description": f"`{d.ticker}`\n{text}", "color": color})

    def settled(self, row: dict) -> None:
        won = row["side"] == row["result"]
        self.fire({
            "title": f"{'WON' if won else 'LOST'} {_usd(row['pnl'])} · {row['result'].upper()}",
            "description": f"`{row['ticker']}`\n{row['filled_contracts']:g} {'YES' if row['side'] == 'yes' else 'NO'} "
                           f"@ {_c(row['fill_price'])}, fee {_usd(row['fee_paid'])}",
            "color": GREEN if won else RED})

    def halted(self, reason: str) -> None:
        self.fire({"title": "HALTED: trading stopped", "description": reason[:1500] +
                   "\nNo new entries until `btc15 reset-halt` is run.", "color": AMBER})
