import pytest

from btc15 import feeds
from btc15.feeds import FeedBook

CB = {"type": "ticker", "product_id": "BTC-USD", "price": "84892.84", "best_bid": "84892.83", "best_ask": "84892.84"}
KR = {"channel": "ticker", "type": "snapshot", "data": [{"symbol": "BTC/USD", "bid": 84894.0, "ask": 84894.1, "last": 84894.0}]}
BS = {"event": "data", "channel": "order_book_btcusd",
      "data": {"bids": [["84892.64", "0.3"], ["84889.90", "0.02"]], "asks": [["84893.10", "0.1"], ["84895.00", "1"]]}}


def test_parsers_on_real_message_shapes():
    assert feeds.parse_coinbase(CB, {}) == pytest.approx(84892.835)
    assert feeds.parse_coinbase({"type": "subscriptions"}, {}) is None
    assert feeds.parse_kraken(KR, {}) == pytest.approx(84894.05)
    assert feeds.parse_kraken({"channel": "heartbeat"}, {}) is None
    assert feeds.parse_bitstamp(BS, {}) == pytest.approx((84892.64 + 84893.10) / 2)
    assert feeds.parse_bitstamp({"event": "bts:subscription_succeeded"}, {}) is None


def test_gemini_top_of_book_needs_both_sides_and_ignores_crossed_books():
    st: dict = {}
    init = {"type": "update", "events": [{"type": "change", "side": "ask", "price": "84896.22", "remaining": "0.03", "reason": "initial"},
                                         {"type": "change", "side": "bid", "price": "84896.21", "remaining": "0.04", "reason": "initial"}]}
    assert feeds.parse_gemini(init, st) == pytest.approx(84896.215)
    one_side = {"type": "update", "events": [{"type": "change", "side": "bid", "price": "84900.00", "remaining": "0.2"}]}
    assert feeds.parse_gemini(one_side, st) is None                    # bid 84900 > ask 84896.22 -> crossed -> ignored
    assert feeds.parse_gemini({"type": "heartbeat"}, st) is None


def test_garbage_is_ignored():
    assert feeds.parse_coinbase({"type": "ticker", "best_bid": "x", "best_ask": None}, {}) is None
    assert feeds.parse_coinbase({"type": "ticker", "best_bid": "100", "best_ask": "90"}, {}) is None


def test_each_feed_keeps_its_own_mid_and_time():
    b = FeedBook()
    b.on_message("coinbase", 100.0, now=1000.0)
    b.on_message("kraken", 200.0, now=1000.5)
    assert b.feeds["coinbase"].mid == 100.0 and b.feeds["kraken"].mid == 200.0
    assert b.feeds["coinbase"].ts == 1000.0 and b.feeds["kraken"].ts == 1000.5
    assert b.feeds["bitstamp"].mid is None and b.feeds["gemini"].ts is None


def test_fresh_means_own_message_under_2s_and_composite_is_median_of_fresh():
    b = FeedBook()
    for name, mid in (("coinbase", 100.0), ("kraken", 102.0), ("bitstamp", 101.0)):
        b.on_message(name, mid, now=1000.0)
    b.on_message("gemini", 500.0, now=996.0)                           # stale outlier must not count
    comp, n, newest = b.composite(1001.0)
    assert (comp, n, newest) == (101.0, 3, 1000.0)
    comp, n, _ = b.composite(1002.5)                                   # now all are stale (>= 2s)
    assert comp is None and n == 0
    snap = {r["feed"]: r["status"] for r in b.snapshot(1001.0)}
    assert snap == {"coinbase": "FRESH", "kraken": "FRESH", "bitstamp": "FRESH", "gemini": "STALE"}


def test_series_is_sampled_once_per_whole_utc_second_from_message_handlers():
    b = FeedBook()
    for i in range(10):
        b.on_message("coinbase", 100.0 + i, now=2000.0 + i * 0.1)      # 10 msgs inside one second
    assert list(b.series) == [2000] and b.series[2000] == 100.0
    b.on_message("coinbase", 105.0, now=2001.2)
    assert sorted(b.series) == [2000, 2001]


def test_window_mean_requires_coverage():
    b = FeedBook()
    b.series = {s: 100.0 for s in range(940, 1000)}
    assert b.window_mean(1000) == 100.0
    b.series = {s: 100.0 for s in range(980, 1000)}
    assert b.window_mean(1000) is None


def drive(b, seconds, mids, t0=1000.0):
    """mids: dict feed -> function(second)->mid ; all feeds message every 0.5s."""
    for i in range(seconds * 2):
        t = t0 + i * 0.5
        for name, fn in mids.items():
            b.on_message(name, fn(i), now=t)


def test_feed_clone_detected_after_30s_and_counted_once():
    b = FeedBook()
    wobble = lambda k: (lambda i: 84000.0 + k + (i % 7) * 0.37)       # noqa: E731  independent feeds differ
    clone = lambda i: 84000.0 + (i % 5) * 0.53                         # noqa: E731  exactly the same series ...
    drive(b, 20, {"coinbase": clone, "kraken": clone, "bitstamp": wobble(3.1), "gemini": wobble(7.7)})
    assert not b.clone_suspect and b.composite(1020.0)[1] == 4         # matched only 20s so far: not yet suspect
    drive(b, 20, {"coinbase": clone, "kraken": clone, "bitstamp": wobble(3.1), "gemini": wobble(7.7)}, t0=1020.0)
    assert b.clone_suspect and b.clones == {"kraken"}
    _, n, _ = b.composite(1040.0)
    assert n == 3                                                      # the clone is counted only once
    assert {r["feed"]: r["status"] for r in b.snapshot(1040.0)}["kraken"] == "CLONE"


def test_clone_flag_clears_when_the_feeds_diverge_again():
    b = FeedBook()
    same = lambda i: 84000.0 + (i % 5) * 0.53                          # noqa: E731
    drive(b, 35, {"coinbase": same, "kraken": same})
    assert b.clone_suspect
    drive(b, 3, {"coinbase": same, "kraken": lambda i: 84000.0 + (i % 5) * 0.53 + 5.0}, t0=1035.0)
    assert not b.clone_suspect


def test_independent_feeds_are_never_flagged():
    b = FeedBook()
    drive(b, 60, {"coinbase": lambda i: 84000 + (i % 7) * 0.31, "kraken": lambda i: 84001 + (i % 5) * 0.29,
                  "bitstamp": lambda i: 83999 + (i % 3) * 0.47, "gemini": lambda i: 84002 + (i % 11) * 0.13})
    assert not b.clone_suspect and b.composite(1060.0)[1] == 4
