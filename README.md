# btc15-trade-caller

Analyzes Kalshi **KXBTC15M** (BTC 15-minute up/down) markets on live data, decides `BUY_UP` / `BUY_DOWN` / `SKIP`
in **deterministic code** (`trade_caller`, a pure function), and can execute under strict risk controls.
Single asyncio process: 4 exchange feeds + Kalshi poller + 1-second decision loop + FastAPI dashboard + SQLite ledger.

> **Safety first.** Default mode is PAPER. LIVE refuses to start unless every guard is set. Nothing here is a
> promise of profit: the system tells you whether it is *proven* on your own settled data, and until it is,
> LIVE is capped at 1 contract per trade.

## What was verified against docs.kalshi.com (2026-10-03)

Every constant is in [`src/btc15/spec.py`](src/btc15/spec.py) with a link to its doc page.

| Item | Verified value |
|---|---|
| Base URLs | prod `https://external-api.kalshi.com/trade-api/v2`, demo `https://external-api.demo.kalshi.co/trade-api/v2` |
| Auth | headers `KALSHI-ACCESS-KEY/TIMESTAMP(ms)/SIGNATURE`; sign `ts + METHOD + path` (no host, no query); RSA-PSS SHA-256 (salt = 32) or Ed25519; base64 |
| Discovery | `GET /markets?series_ticker=KXBTC15M&status=open`; strike = `floor_strike` (double) |
| Orderbook | bids only (`orderbook_fp.yes_dollars/no_dollars`, best = last): `yes_ask = 1 - best_no_bid`, `no_ask = 1 - best_yes_bid` |
| Orders (V2) | `POST /portfolio/events/orders` (YES-book only: `bid`/`ask`), `DELETE /portfolio/events/orders/{id}?market_ticker=`, `GET /portfolio/orders/{id}`, `/fills`, `/positions`, `/balance` |
| Settlement | `result` = `yes`/`no`, `expiration_value`, status → `finalized` ≈ 7 s after close |
| Contract | YES if the 60-s average of CF Benchmarks BRTI before close ≥ the same average before open ⇒ **W = 60** |
| Strike | market N+1's `floor_strike` == market N's `expiration_value` (an official 60-s average, **not** spot at open) |
| Fees | series API: `fee_type=quadratic`, `fee_multiplier=1` (no maker fee) |

### Not verified from a primary source: read this before DEMO/LIVE
1. **Fee constant 0.07.** The official fee PDF is behind a bot checkpoint; 0.07 is confirmed by secondary sources only.
   The executor compares every fill's `fee_cost` to the formula and **halts** on a > 1.01¢ per-order mismatch.
2. **BUY_DOWN.** The V2 order endpoint is YES-book only. Buying NO at `no_ask` is sent as an `ask` at `1 - no_ask`
   ("selling YES is economically equivalent to buying NO at 1 − price"). The executor then requires the API position to be
   **negative** (NO) with the right size, and **halts** otherwise. Confirm this first in DEMO with 1 contract.
3. **Basis definition.** Because the strike is a 60-s average, `basisSample` uses the composite's trailing 60-s mean at open
   (`BASIS_METHOD=avg60`, default) rather than spot (`spot` is available). Final-60-s composite vs official settlement
   is logged for every market in `basis_log`.

## Quick start

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest                                   # all tests must pass before DEMO
cp .env.example .env                     # then edit
btc15 run                                # PAPER by default; dashboard on :8080
```

### 1. Create Kalshi API keys (demo and production are separate)
* **Demo**: sign up at <https://demo.kalshi.co> → *Account & security → API Keys → Create Key*.
* **Production**: <https://kalshi.com> → *Account & security → API Keys → Create Key*.
* Save the **Key ID** and download the private-key PEM immediately (it cannot be retrieved later).
  `chmod 600` the PEM. Never commit it (`.gitignore` already excludes `*.pem`, `*.key`, `.env`).

### 2. Fill `.env`
`DASHBOARD_PASSWORD` (≥ 8 chars) is required in every mode. For DEMO/LIVE add `KALSHI_KEY_ID` and
`KALSHI_PRIVATE_KEY_PATH`. See `.env.example`.

### 3. PAPER (default)
`MODE=PAPER btc15 run`. Live market data and live exchange feeds; fills are simulated **at the ask** (optimistic: no depth,
no queue). No orders are ever sent and no Kalshi credentials are needed. Warm-up is ~30 min after start (the 1800-s
data-gap and volatility windows must fill, and one market boundary must pass to seed the basis) — until then the gates SKIP.
Let it settle ≥ 300 markets; check `btc15 report` or `/calibration`.

### Collateral lives on an exchange shard (important for DEMO and LIVE)
Kalshi checks collateral inside each matching engine ("shard"), and programmatic traders must **pre-allocate funds to the
shard that trades the market** before an order can be placed. KXBTC15M is a Crypto series on **shard 2**
([docs](https://docs.kalshi.com/getting_started/exchange_sharding)). Funds sitting on shard 0 cannot fund it: an order is
rejected with `insufficient_shard_balance`. Move collateral to shard 2 in the Kalshi UI (*Account → Exchange Indexes*) or with
the Intra Account Transfer API. The bankroll the model sizes against, and every pre/post-order balance check, uses the
market's own shard, and the dashboard's Readiness panel shows a **collateral** check until it is funded.

### 4. DEMO
Put your **demo** key in `.env`, set `MODE=DEMO`. Real orders go to Kalshi's demo exchange (quotes also come from demo).
Use it to confirm auth, order placement/cancel, fills, positions (including the BUY_DOWN sign) and the fee check. DEMO rows
are shown separately and are **not** counted as proof evidence.

### 5. LIVE
Only after PAPER and DEMO behave. Set a **production** key and **all** of:
```
MODE=LIVE
LIVE_CONFIRM=yes
DAILY_LOSS_LIMIT_USD=20
MAX_TOTAL_LOSS_USD=100
MAX_CONTRACTS_PER_TRADE=3
ALLOW_UNPROVEN=yes        # required while proof status is UNPROVEN; then forced to 1 contract per trade
```
The process refuses to start (exit 2) if any is missing. A persistent red **LIVE — REAL MONEY** banner is shown on every page.

## Operating

| Command | Effect |
|---|---|
| `btc15 run` | start everything |
| `btc15 report` | proof status, Brier scores, reliability table |
| `btc15 kill` | write `./KILL` + set HALT (a running service cancels resting orders within ~1 s) |
| `btc15 reset-halt` | remove `KILL`, clear HALT (resume) |
| `btc15 backfill-results` | fetch official results for unsettled ledger rows |

HALT triggers: kill file / `POST /kill` / dashboard button, 3 consecutive API errors, any reconciliation mismatch (order vs
fills vs position vs balance, or API positions vs ledger), ambiguous order outcome. HALT is **persistent** across restarts.

## Discord alerts
Set `DISCORD_WEBHOOK_URL` (Discord: *Server Settings → Integrations → Webhooks → New Webhook → Copy Webhook URL*).
You get a message for each **BUY UP / BUY DOWN** call (size, price, max loss, payout, model p vs market, edge, proof status,
and the mode: PAPER / DEMO / LIVE), the fill result, the **settlement result** (won/lost and PnL) and any **HALT**.
Alerts are informational and fire-and-forget: a failing webhook never affects trading, and the URL is never logged.
There is deliberately **no sell / take-profit / stop-loss**: the strategy holds to settlement, and a binary contract's
worst case is already the stake (shown as "max loss" in every alert).

## Design notes
* **One decision per market**, only on the `SKIP → BUY` transition (never on the first observation), limit buy at the ask,
  cancelled if not fully filled in 10 s, never a market order, never sells, holds to settlement.
* **Loss gates are worst-case**: realised PnL − open exposure − this trade's max loss must stay inside the limit (UTC day).
* **params_version** hashes the strategy settings (not secrets, paths, mode or risk limits), so changing the strategy
  resets the proof record. **PROVEN** needs ≥ 300 settled rows, Brier(p) < Brier(p_mkt), every p_mkt bucket with n ≥ 20 within
  5 pts, and ROI after fees > 0 over ≥ 100 trades, on the current params_version (PAPER + LIVE rows).
* The dashboard is plain HTTP basic auth. **Put it behind TLS / VPN / SSH tunnel** before exposing it publicly.

## Deploy
* Ubuntu 22.04/24.04 VPS: `sudo bash scripts/setup.sh` (installs Python, venv, app, hardened systemd unit with
  `Restart=always`, `.env` template with a random dashboard password, port 8080). Use `btc15ctl report|kill|reset-halt`.
* Docker: `docker build -t btc15 . && docker run -p 8080:8080 -v btc15data:/data --env-file .env btc15`.
