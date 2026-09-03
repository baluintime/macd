# Upstox MACD Options Desk

A Python trading application for the automated multi-timeframe MACD options engine
described in the SRS (v1.0.0). It connects to your Upstox account, runs the MACD
12/26/9 reversal strategy on 1-minute and 5-minute candles simultaneously, buys
in-the-money options on each crossover, exits on the opposite crossover or at your
target points, and reports **net profit after every broker and statutory charge**.

Everything on the page is computed in Python and rendered server-side. The only
JavaScript posts the form back for live updates — no jQuery, no frontend framework.
**There is no sample, demo, or simulated market data anywhere in this app.** Prices,
strikes and deltas come from the live Upstox option chain; the blotter holds only
trades that were actually executed.

## Run it

```bash
pip install -r requirements.txt

python -m macd_desk                 # http://localhost:8000
python -m macd_desk --port 9000     # any port you like
python run.py -p 9000               # same thing, shorter
```

| Flag | Default | Purpose |
|---|---|---|
| `-p, --port` | `8000`, or `$MACD_DESK_PORT` / `$PORT` | port to listen on |
| `-H, --host` | `127.0.0.1`, or `$MACD_DESK_HOST` | bind address — `0.0.0.0` to reach it from another machine |
| `-s, --state` | `desk-state.json`, or `$MACD_DESK_STATE` | where the desk configuration is stored |
| `--debug` | off | reload on code changes |

## Configuring Upstox

**Everything lives in `.env` in the project root** (or real environment variables,
which win over the file). Start from the template:

```bash
cp .env.example .env
```

| Variable | Required | What it is |
|---|---|---|
| `UPSTOX_API_KEY` | yes | "API Key" from the Upstox developer console — the OAuth `client_id` |
| `UPSTOX_API_SECRET` | yes | "API Secret" from the same app. Server-side only; it never reaches the page |
| `UPSTOX_REDIRECT_URI` | yes | Must match the app's registered redirect character for character. Locally: `http://127.0.0.1:8000/broker/callback` — but any path works (see below) |
| `UPSTOX_LIVE_TRADING` | no (`no`) | `yes` arms real order placement |
| `UPSTOX_TOKEN_FILE` | no | Where the daily token is cached (written `chmod 600`, gitignored) |
| `UPSTOX_API_BASE` / `UPSTOX_HFT_BASE` | no | Override only if Upstox moves a host |
| `UPSTOX_TIMEOUT_SECONDS` | no (`10`) | HTTP timeout |

Then open **`/broker`** in the app. That page is the control panel for all of it:

1. **Credentials** — shows which variables are set (key masked, secret never rendered)
   and names exactly what is missing.
2. **Connection** — *Connect to Upstox* runs the OAuth login and caches the token.
   Upstox tokens expire at **03:30 IST daily** with no refresh token, so reconnect
   each morning. *Test live data* proves the credentials reach your real account.
3. **Order routing** — shows whether live orders are armed.
4. **Autotrade engine** — start/stop, open positions, and the engine log.

### If the callback 404s

Upstox returns to the URI registered on **your** Upstox app, which may not be
`/broker/callback`. The desk serves the canonical path *and* whatever path
`UPSTOX_REDIRECT_URI` names, so a registration like
`http://localhost:8000/api/auth/callback` works without changing anything at Upstox.
The **Callback served at** row on `/broker` shows exactly which paths are live.

The host and port still have to match the registration exactly — `localhost` and
`127.0.0.1` are different strings to Upstox even though both reach the same server.
If the configured path collides with a route the desk already uses, it says so rather
than hijacking it.

Restart the desk after editing `.env`.

## Autotrading

Start the engine from `/broker`. Each cycle, for every configured instrument:

1. **Warm up** from 3 days of historical candles — MACD 12/26/9 needs 34 candles
   before its first value, which is exactly why the SRS mandates the warmup.
2. **Pull closed candles** for that instrument's own timeframe. 1-minute and 5-minute
   instruments run side by side in the same loop; the still-forming candle is ignored.
3. **Act on a crossover:**
   - MACD crosses **above** signal → exit any PE, **BUY a CE**
   - MACD crosses **below** signal → exit any CE, **BUY a PE**
4. **Check the target** — if the option premium reaches entry + target points, **SELL**.
5. **Square off** at 15:20 IST; the session window is 09:15–15:30, weekdays only.
6. **Record the round trip** in the blotter, where the cost model turns it into net
   profit after charges.

Both legs are real orders when armed: BUY to enter, SELL to exit.

### Contract selection — in the money, delta 0.60–0.70

The engine does **not** trade the at-the-money strike. On each entry it reads the live
option chain for the nearest expiry and picks the contract that is:

- **in the money** (call strike below spot; put strike above spot), and
- **delta between 0.60 and 0.70**, taken from `option_greeks.delta` on the live chain
  (magnitude, since put delta is negative), choosing the strike nearest 0.65.

An ATM option sits near 0.50 delta; 0.60–0.70 is one or two strikes in, which tracks
the underlying more closely and decays more slowly. If no strike on the live chain
meets both rules, **selection fails and the entry is skipped** — the engine never
substitutes a contract you did not ask for. The band is set in
`macd_desk/engine/selection.py` (`DEFAULT_DELTA_MIN` / `DEFAULT_DELTA_MAX`).

### Two switches guard a real order

An order only reaches the exchange when **both** are true:

1. `UPSTOX_LIVE_TRADING=yes` in the environment, and
2. that instrument's **Execution Mode** is set to **Live** on the desk page.

Otherwise the fill is simulated at the live quote — paper mode as the SRS defines it,
against the real order book, never against invented prices.

## Checking the MACD against the real market

Every instrument card carries a **MACD** button. It opens a readout of the live series
for that index or stock at the chosen timeframe (switchable 1-min / 5-min):

- the latest MACD line, signal line and histogram, with the stance the engine derives
- a chart of both lines and the histogram on one shared scale, with each crossover marked
- recent crossovers, and the entry each one produces
- **every candle** with open/high/low/close, **both EMAs**, MACD, signal, histogram and
  any crossover flag

**Download CSV** exports the whole series — the same rows, nothing rounded away — so you
can put it beside your charting platform and reconcile row by row:

```
at,open,high,low,close,emaFast,emaSlow,macd,signal,histogram,cross
```

Warmup rows export as empty rather than zero, because there genuinely is no value yet.

If a platform disagrees, the EMA columns are what locate the cause. Two things account
for most mismatches: this engine seeds each EMA with a **simple average of its first
`period` closes** (the common charting convention) rather than the first close alone,
and it computes on **closed candles only** — the candle still forming never appears.
Find the first row where the EMAs part company and the difference is either the seeding,
the periods, or the candle data itself.

## The cost model

All maths lives in [`macd_desk/charges.py`](macd_desk/charges.py) — pure functions,
no Flask, no I/O. Charges apply to **premium turnover**, never notional. For a round
trip with `qty = lots × lotSize`:

```
grossPnl      = (exitPrice − entryPrice) × qty

brokerage     = min(₹20, 2.5% × legTurnover)   per leg, both legs
STT           = 0.1%     × sellTurnover        sell side only
exchange txn  = 0.03503% × turnover            NSE options
IPFT          = 0.0005%  × turnover
SEBI fee      = 0.0001%  × turnover            (₹10 per crore)
stamp duty    = 0.003%   × buyTurnover         buy side, rounded to the rupee
GST           = 18% × (brokerage + exchange txn + SEBI + IPFT)

netPnl        = grossPnl − totalCharges
breakEven     = totalCharges ÷ qty             points the premium must travel
```

Rounding is half-away-from-zero, the way a contract note rounds — not Python's
default banker's rounding. Rates are **indicative** and editable on the page; re-check
them against the live Upstox tariff before using any figure for accounting.

**Why break-even matters here:** a reversal engine flips position on every crossover,
so on a choppy day it pays the round trip repeatedly. Below that many points of
premium movement, a "winning" trade still loses money.

## Routes

| Route | Method | Purpose |
|---|---|---|
| `/` | GET / POST | the desk, rendered server-side; the form submits edits |
| `/broker` | GET | credentials, connection, order routing, engine |
| `/broker/connect`, `/broker/callback` | GET | the OAuth login round trip |
| `/broker/disconnect`, `/broker/test` | POST | drop the cached token / prove live access |
| `/engine/start`, `/engine/stop` | POST | run the autotrade loop |
| `/macd/<symbol>` | GET | the MACD readout for one instrument (`?tf=1m` or `?tf=5m`) |
| `/macd/<symbol>.csv` | GET | the full series as a CSV download |
| `/market/refresh` | POST | pull live chains now, without starting the engine |
| `/api/book` | POST | cost a submitted form, returning formatted figures (changes nothing) |
| `/api/state`, `/api/engine` | GET | desk state, engine status |
| `/export.csv` | GET | the costed blotter |
| `/healthz` | GET | liveness |

## Upstox endpoints used

| Purpose | Endpoint |
|---|---|
| OAuth dialog / token | `/v2/login/authorization/dialog`, `/v2/login/authorization/token` |
| Profile, funds | `/v2/user/profile`, `/v2/user/get-funds-and-margin` |
| Option expiries, chain + greeks | `/v2/option/contract`, `/v2/option/chain` |
| LTP | `/v3/market-quote/ltp` |
| Warmup / intraday candles | `/v3/historical-candle/…`, `/v3/historical-candle/intraday/…` |
| Place order | `/v3/order/place` on `api-hft.upstox.com` |

Each path is named once in `macd_desk/broker/upstox.py`; hosts are overridable by
environment variable, since Upstox versions these independently.

## Tests

```bash
python -m unittest discover -s tests -t .
```

113 cases: the charge model, the web layer including the no-JavaScript path, MACD and
crossover detection, the readout rows and CSV export, contract selection inside the
delta band, the execution guards, and the runner driven end to end against a fake
broker.

## Scope and cautions

The engine trades your real account when armed. Watch a full session in paper mode
first, and keep position sizes small.

Market data is polled over REST (candles and the option chain) rather than the
protobuf WebSocket feed — real, live, non-synthetic data, but not the streaming
transport the SRS names. `UpstoxClient.feed_authorize()` returns the authorised
`wss://` URL, which is the seam a streaming feed would attach to; it needs the
Upstox `.proto` definitions and a WebSocket client.

## Files

```
macd_desk/charges.py          the cost model — single source of truth
macd_desk/config.py           credentials and switches, from .env or the environment
macd_desk/state.py            desk configuration, validation, atomic persistence
macd_desk/formatting.py       rupee formatting with Indian digit grouping
macd_desk/app.py              Flask routes and the view model
macd_desk/broker/upstox.py    Upstox API client (stdlib HTTP)
macd_desk/broker/tokens.py    token cache, 03:30 IST expiry
macd_desk/engine/indicators.py  EMA and MACD
macd_desk/engine/strategy.py    the SRS rules as pure decisions
macd_desk/engine/selection.py   ITM delta 0.60–0.70 contract selection
macd_desk/engine/execution.py   paper and live executors, and the guard
macd_desk/engine/runner.py      the autotrade loop
macd_desk/analysis.py           the MACD readout: rows, chart geometry, CSV columns
```
