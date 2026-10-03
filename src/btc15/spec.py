"""Kalshi API facts verified against docs.kalshi.com on 2026-10-03.

Every constant cites the doc page it came from. Items marked UNVERIFIED have
no primary-source confirmation yet and must be resolved before DEMO/LIVE.
Prices are fixed-point dollar strings (`*_dollars`), counts are `*_fp` strings;
parse with Decimal, never float.
"""
from __future__ import annotations

# --- Environments -----------------------------------------------------------
# https://docs.kalshi.com/getting_started/api_environments
REST_BASE = {
    "PROD": "https://external-api.kalshi.com/trade-api/v2",
    "DEMO": "https://external-api.demo.kalshi.co/trade-api/v2",
}
WS_URL = {
    "PROD": "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
    "DEMO": "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
}
# Credentials are NOT shared between demo and prod.

# --- Auth -------------------------------------------------------------------
# https://docs.kalshi.com/getting_started/quick_start_authenticated_requests
HDR_KEY = "KALSHI-ACCESS-KEY"
HDR_TS = "KALSHI-ACCESS-TIMESTAMP"          # unix milliseconds, as string
HDR_SIG = "KALSHI-ACCESS-SIGNATURE"         # base64
# message = timestamp_ms + METHOD + full path from root WITHOUT query string,
# e.g. "1703123456789GET/trade-api/v2/portfolio/balance".
# RSA keys: RSA-PSS, MGF1(SHA256), salt_length = digest length (32), hash SHA256.
# Ed25519 keys sign the message directly (we support both).
API_PATH_PREFIX = "/trade-api/v2"

# --- Market data (public, no auth) -----------------------------------------
SERIES = "KXBTC15M"
# https://docs.kalshi.com/api-reference/market/get-markets
#   GET /markets?series_ticker=KXBTC15M&status=open
# https://docs.kalshi.com/api-reference/market/get-market   GET /markets/{ticker}
# Fields used: ticker, status (active|closed|determined|finalized...), open_time,
#   close_time, floor_strike (double; exact strike), yes_bid_dollars,
#   yes_ask_dollars, no_bid_dollars, no_ask_dollars, price_ranges (tick grid),
#   result ('yes'|'no'|''), expiration_value, settlement_value_dollars, settlement_ts.
# NOTE: status filter values on the list endpoint are unopened|open|closed|settled,
#   but the market object's own status is active|closed|determined|finalized.
# https://docs.kalshi.com/api-reference/market/get-market-orderbook
#   GET /markets/{ticker}/orderbook -> {"orderbook_fp": {"yes_dollars": [[price, count]...],
#   "no_dollars": [...]}}  BIDS ONLY, ascending, best bid = LAST element.
#   yes_ask = 1 - best_no_bid ; no_ask = 1 - best_yes_bid
#   https://docs.kalshi.com/getting_started/orderbook_responses
# https://docs.kalshi.com/getting_started/fixed_point_migration (valid tick = market.price_ranges)

# --- Contract rules (verified live via GET /series/KXBTC15M and market.rules_primary) ---
# Resolves YES if avg of 60 one-second CF Benchmarks BRTI values before close >=
# the same 60s average before open. => SETTLE_WINDOW_SEC = 60 is CONFIRMED.
# floor_strike of market N+1 == expiration_value of market N (strike is itself an
# official 60s BRTI average, NOT spot at the open instant).
SETTLE_WINDOW_SEC_DEFAULT = 60
SETTLEMENT_SOURCE = "CF Benchmarks BRTI (60s simple average)"

# --- Orders (V2; YES-book only) ---------------------------------------------
# https://docs.kalshi.com/api-reference/orders/create-order-v2
CREATE_ORDER = ("POST", "/portfolio/events/orders")
#   body: ticker, client_order_id, side ('bid'=buy YES | 'ask'=sell YES), count (str "10.00"),
#   price (str dollars), time_in_force (good_till_canceled|immediate_or_cancel|fill_or_kill),
#   self_trade_prevention_type (taker_at_cross|maker) [required], post_only, subaccount, exchange_index
#   resp: order_id, client_order_id, fill_count, remaining_count, average_fill_price, average_fee_paid, ts_ms
#   UNVERIFIED (needs DEMO test): BUY_DOWN at no_price N is expressed as side='ask',
#   price = 1 - N ("selling YES is economically equivalent to buying NO at 1 - price").
# https://docs.kalshi.com/api-reference/orders/cancel-order-v2
CANCEL_ORDER = ("DELETE", "/portfolio/events/orders/{order_id}")   # query: market_ticker
# https://docs.kalshi.com/api-reference/orders/get-order
GET_ORDER = ("GET", "/portfolio/orders/{order_id}")
#   order.status in resting|canceled|executed; fill_count_fp, remaining_count_fp,
#   taker_fees_dollars, maker_fees_dollars, outcome_side, book_side
GET_ORDERS = ("GET", "/portfolio/orders")     # ?ticker=&status=resting
# https://docs.kalshi.com/api-reference/portfolio/get-fills
GET_FILLS = ("GET", "/portfolio/fills")       # ?order_id=&ticker= ; fill: count_fp, yes_price_dollars,
#   no_price_dollars, fee_cost, outcome_side, is_taker, created_time
# https://docs.kalshi.com/api-reference/portfolio/get-positions
GET_POSITIONS = ("GET", "/portfolio/positions")   # ?ticker= ; market_positions[].position_fp,
#   market_exposure_dollars, fees_paid_dollars, realized_pnl_dollars
# https://docs.kalshi.com/api-reference/portfolio/get-balance
GET_BALANCE = ("GET", "/portfolio/balance")   # balance (int cents), balance_dollars, portfolio_value
# https://docs.kalshi.com/api-reference/portfolio/get-settlements
GET_SETTLEMENTS = ("GET", "/portfolio/settlements")   # market_result, revenue, fee_cost, settled_time

# --- Fees -------------------------------------------------------------------
# Series KXBTC15M (live GET /series/KXBTC15M): fee_type="quadratic", fee_multiplier=1
#   https://docs.kalshi.com/api-reference/market/get-series
# Taker fee = ceil_to_cent(FEE_RATE * C * P * (1-P)) * fee_multiplier; 'quadratic' has no maker fee.
# UNVERIFIED (primary): the 0.07 constant. The fee PDF
#   https://kalshi.com/docs/kalshi-fee-schedule.pdf is behind a bot checkpoint (HTTP 429);
#   0.07 is confirmed only by secondary sources. Executor must compare predicted fee to
#   fills[].fee_cost and HALT on a >1c mismatch.
# Fee accounting detail: https://docs.kalshi.com/getting_started/fee_rounding
FEE_RATE = 0.07

# --- Rate limits ------------------------------------------------------------
# https://docs.kalshi.com/getting_started/rate_limits : default 10 tokens/request,
# separate Read/Write buckets, 429 body {"error":"too many requests"}, no Retry-After.

# --- WebSocket (optional; polling is the baseline) ---------------------------
# https://docs.kalshi.com/getting_started/quick_start_websockets : the handshake needs the
# same 3 auth headers even for public channels (`ticker`, `trade`, `market_lifecycle_v2`).
