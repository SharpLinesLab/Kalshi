"""Independent BTC-USD reference feeds (Coinbase, Kraken, Bitstamp, Gemini) and the composite.

Each feed keeps ITS OWN last mid and ITS OWN last message time; nothing is ever copied between feeds.
Parsers are pure functions of (message, per-connection state) so they can be unit-tested.
"""
from __future__ import annotations

import asyncio
import json
import logging
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import websockets

log = logging.getLogger("btc15.feeds")
FEEDS = ("coinbase", "kraken", "bitstamp", "gemini")


def _sane_mid(bid: Any, ask: Any) -> Optional[float]:
    try:
        b, a = float(bid), float(ask)
    except (TypeError, ValueError):
        return None
    if b <= 0 or a <= 0 or a < b or (a - b) / a > 0.01:      # crossed or absurdly wide book -> ignore
        return None
    return (a + b) / 2.0


def parse_coinbase(msg: dict, state: dict) -> Optional[float]:
    if msg.get("type") != "ticker":
        return None
    return _sane_mid(msg.get("best_bid"), msg.get("best_ask"))


def parse_kraken(msg: dict, state: dict) -> Optional[float]:
    if msg.get("channel") != "ticker" or msg.get("type") not in ("snapshot", "update"):
        return None
    for row in msg.get("data") or []:
        m = _sane_mid(row.get("bid"), row.get("ask"))
        if m is not None:
            return m
    return None


def parse_bitstamp(msg: dict, state: dict) -> Optional[float]:
    if msg.get("event") != "data":
        return None
    d = msg.get("data") or {}
    try:
        bid = max(float(x[0]) for x in d.get("bids", []))
        ask = min(float(x[0]) for x in d.get("asks", []))
    except (ValueError, TypeError, IndexError):
        return None
    return _sane_mid(bid, ask)


def parse_gemini(msg: dict, state: dict) -> Optional[float]:
    """top_of_book stream: each 'change' event carries the new best price for one side."""
    if msg.get("type") != "update":
        return None
    for ev in msg.get("events", []):
        if ev.get("type") == "change" and ev.get("side") in ("bid", "ask"):
            try:
                if float(ev.get("remaining", 1)) > 0:
                    state[ev["side"]] = float(ev["price"])
            except (TypeError, ValueError):
                pass
    if "bid" in state and "ask" in state:
        return _sane_mid(state["bid"], state["ask"])
    return None


@dataclass
class FeedSpec:
    url: str
    subscribe: list
    parse: Callable[[dict, dict], Optional[float]]


SPECS: dict[str, FeedSpec] = {
    "coinbase": FeedSpec("wss://ws-feed.exchange.coinbase.com",
                         [{"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["ticker"]}], parse_coinbase),
    "kraken": FeedSpec("wss://ws.kraken.com/v2",
                       [{"method": "subscribe", "params": {"channel": "ticker", "symbol": ["BTC/USD"]}}], parse_kraken),
    "bitstamp": FeedSpec("wss://ws.bitstamp.net",
                         [{"event": "bts:subscribe", "data": {"channel": "order_book_btcusd"}}], parse_bitstamp),
    "gemini": FeedSpec("wss://api.gemini.com/v1/marketdata/BTCUSD?top_of_book=true&bids=true&offers=true", [], parse_gemini),
}


@dataclass
class FeedState:
    mid: Optional[float] = None
    ts: Optional[float] = None          # this feed's own last message time
    msgs: int = 0
    sec_mid: dict = field(default_factory=dict)   # whole second -> this feed's LAST mid in that second


class FeedBook:
    def __init__(self, fresh_sec: float = 2.0, clone_sec: float = 30.0, history_sec: int = 7200):
        self.fresh_sec, self.clone_sec, self.history_sec = fresh_sec, clone_sec, history_sec
        self.feeds: dict[str, FeedState] = {n: FeedState() for n in FEEDS}
        self.series: dict[int, float] = {}          # whole UTC second -> composite
        self._last_sec = 0
        self._match_since: dict[tuple[str, str], int] = {}
        self._match_last: dict[tuple[str, str], int] = {}
        self.clones: set[str] = set()
        self.clone_suspect = False

    # ---------------------------------------------------------------- ingestion
    def on_message(self, name: str, mid: float, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        st = self.feeds[name]
        st.mid, st.ts = mid, now
        st.msgs += 1
        sec = int(now)
        st.sec_mid[sec] = mid
        for k in [k for k in st.sec_mid if k < sec - 3]:
            del st.sec_mid[k]
        if sec > self._last_sec:              # first message in a new whole UTC second takes the sample
            self._last_sec = sec
            self._sample(now, sec)

    def _is_fresh(self, name: str, now: float) -> bool:
        s = self.feeds[name]
        return s.ts is not None and s.mid is not None and (now - s.ts) < self.fresh_sec

    def _update_clones(self, now: float, done_sec: int) -> None:
        """Compare feeds on the just-completed whole second (each feed's LAST mid in it), so that the
        order in which two feeds' messages happen to arrive cannot fake a mismatch. Two feeds that agree
        to the cent for >= clone_sec consecutive seconds are suspected clones; only the earlier one counts."""
        done = {n: self.feeds[n].sec_mid[done_sec] for n in FEEDS if done_sec in self.feeds[n].sec_mid}
        names = [n for n in FEEDS if n in done]
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                key = (a, b)
                if round(done[a], 2) == round(done[b], 2):
                    self._match_since.setdefault(key, done_sec)
                    self._match_last[key] = done_sec
                else:
                    self._match_since.pop(key, None)
        flagged: set[str] = set()
        for (a, b), since in list(self._match_since.items()):
            if not (self._is_fresh(a, now) and self._is_fresh(b, now)):
                self._match_since.pop((a, b))                 # a feed went quiet: restart the evidence
                continue
            if self._match_last.get((a, b), since) - since + 1 >= self.clone_sec and a not in flagged:
                flagged.add(b)
        self.clones = flagged
        self.clone_suspect = bool(flagged)

    def _sample(self, now: float, sec: int) -> None:
        self._update_clones(now, sec - 1)
        comp = self.composite(now)[0]
        if comp is not None:
            self.series[sec] = comp
        cut = sec - self.history_sec
        if len(self.series) > self.history_sec + 600:
            for k in [k for k in self.series if k < cut]:
                del self.series[k]

    # ---------------------------------------------------------------- queries
    def counted(self, now: float) -> dict[str, float]:
        return {n: self.feeds[n].mid for n in FEEDS if self._is_fresh(n, now) and n not in self.clones}

    def composite(self, now: float) -> tuple[Optional[float], int, Optional[float]]:
        """(median of FRESH counted mids, number of feeds counted, newest message time among them)."""
        c = self.counted(now)
        if not c:
            return None, 0, None
        newest = max(self.feeds[n].ts for n in c)
        return statistics.median(c.values()), len(c), newest

    def window_mean(self, end_sec: int, window: int = 60, min_cover: int = 45) -> Optional[float]:
        vals = [self.series[s] for s in range(end_sec - window, end_sec) if s in self.series]
        return statistics.fmean(vals) if len(vals) >= min_cover else None

    def snapshot(self, now: float) -> list[dict]:
        rows = []
        counted = self.counted(now)
        for n in FEEDS:
            s = self.feeds[n]
            age = None if s.ts is None else max(0.0, now - s.ts)
            status = "NO DATA" if s.ts is None else ("CLONE" if n in self.clones else ("FRESH" if n in counted else "STALE"))
            rows.append({"feed": n, "mid": s.mid, "age": age, "status": status})
        return rows


async def run_feed(name: str, book: FeedBook, stop: asyncio.Event, stall_sec: float = 15.0) -> None:
    spec = SPECS[name]
    backoff = 1.0
    while not stop.is_set():
        state: dict = {}
        try:
            async with websockets.connect(spec.url, open_timeout=10, max_size=2 ** 24, ping_interval=20) as ws:
                for s in spec.subscribe:
                    await ws.send(json.dumps(s))
                backoff = 1.0
                while not stop.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=stall_sec)
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    mid = spec.parse(msg, state)
                    if mid is not None:
                        book.on_message(name, mid)
        except asyncio.CancelledError:
            raise
        except Exception as e:                      # reconnect on anything, never crash the process
            log.warning("feed %s dropped: %s", name, type(e).__name__)
        if not stop.is_set():
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
