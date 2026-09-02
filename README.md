# Upstox MACD Options Desk

A Python web app for the automated multi-timeframe MACD options trading engine
described in the SRS (v1.0.0). It provides the per-instrument controls the spec
calls for, and — the part the spec leaves open — it **costs every trade and
reports the net profit after all broker and statutory charges**.

Every figure on the page is computed in Python and rendered server-side. The
only JavaScript is a thin layer that posts the form back for a live update; with
JavaScript off, the same form submits normally and the server re-renders. There
is no jQuery and no frontend framework.

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

```bash
MACD_DESK_PORT=9000 python -m macd_desk          # port from the environment
python -m macd_desk -H 0.0.0.0 -p 9000           # reachable on the LAN
python -m macd_desk -s ~/desks/friday.json       # a second, separate book
```

Desk state — instruments, blotter, rate card — is one JSON file written
atomically, so it survives a restart and can be committed, diffed, or handed to
someone else. Point `--state` at different files to keep separate books.

Run the tests with `python -m unittest discover -s tests -t .`.

## Routes

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | the desk, fully rendered server-side |
| `/` | POST | form submit — edits, add/delete trade, load sample, reset rates |
| `/api/book` | POST | cost the submitted form and return formatted figures as JSON (changes nothing) |
| `/api/state` | GET / POST | read or persist desk state |
| `/export.csv` | GET | the costed blotter as a CSV download |
| `/healthz` | GET | liveness check |

## What the page shows

| Block | Purpose |
|---|---|
| **Net profit — session book** | Gross P&L, total charges, **net profit**, and the break-even move in points |
| **Instrument desk** | One card per symbol with the SRS §5 controls, plus the projected net profit if that position exits exactly at its target |
| **Executed trades** | Every MACD-crossover / target exit, costed individually — editable inline |
| **Where the money goes** | Charge heads ranked by size, with each head's share of total charges and of gross profit |
| **Rate card** | Every rate is editable; the whole page recomputes live |

### SRS §5 controls, per instrument

| Parameter | Control | Implemented as |
|---|---|---|
| Execution mode | Toggle | Live / Paper, independent per symbol |
| Position size | Numeric | Lots per signal (× lot size → quantity) |
| Target points | Numeric | Drives the "Net @ target" projection on the card |
| Engine timeframe | Toggle | 1-min / 5-min, independent per symbol |

Option side (CE/PE), lot size and entry premium are editable too, since lot sizes
are revised by the exchange and the premium is what every charge is levied on.
The toggles are radio inputs, so they submit with the form and work without
JavaScript.

## The cost model

All maths lives in [`macd_desk/charges.py`](macd_desk/charges.py) — pure
functions over plain dicts, no Flask, no I/O. Nothing is levied on notional;
options charges apply to **premium turnover**.

For a round trip (buy leg + sell leg) with `qty = lots × lotSize`:

```
buyTurnover   = entryPrice × qty
sellTurnover  = exitPrice  × qty
turnover      = buyTurnover + sellTurnover

grossPnl      = sellTurnover − buyTurnover

brokerage     = min(₹20, 2.5% × legTurnover)   per leg, both legs
STT           = 0.1%     × sellTurnover        sell side only
exchange txn  = 0.03503% × turnover            NSE options
IPFT          = 0.0005%  × turnover
SEBI fee      = 0.0001%  × turnover            (₹10 per crore)
stamp duty    = 0.003%   × buyTurnover         buy side, rounded to the rupee
GST           = 18% × (brokerage + exchange txn + SEBI + IPFT)

totalCharges  = sum of the seven heads above
netPnl        = grossPnl − totalCharges
breakEven     = totalCharges ÷ qty             points the premium must travel
```

Rounding is half-away-from-zero, the way a contract note rounds — not Python's
default banker's rounding.

Rates are **indicative NSE/Upstox figures for equity and index options** and are
the defaults in `DEFAULT_RATES`; the exchange and the broker revise slabs, so the
rate card on the page overrides them at runtime and
`compute_trade(trade, rate_overrides)` overrides them in code. Re-check against
the live Upstox tariff before using any figure for accounting — these numbers are
a planning aid, not a contract note.

### Why break-even matters to this strategy

A MACD reversal engine flips position on every crossover, so on a choppy day it
pays the round-trip cost repeatedly. The break-even figure is the honest floor:
below that many points of premium movement, a "winning" trade still loses money.
Small lot sizes are where this bites hardest — the flat ₹20 per order does not
shrink with the position.

## API

```python
from macd_desk.charges import compute_trade, project_at_target, summarize

compute_trade({"entryPrice": 128.2, "exitPrice": 148.2, "lots": 1, "lotSize": 75})
# → {"grossPnl": …, "charges": {…}, "totalCharges": …, "netPnl": …, "breakEvenPoints": …}

project_at_target({"entryPrice": 142.5, "targetPoints": 20, "lots": 1, "lotSize": 75})
# net profit if the position exits exactly at its configured target

summarize(trades, rate_overrides)
# → {"rows": [costed trades], "totals": {"grossPnl": …, "totalCharges": …, "netPnl": …}}
```

## Scope

This is the **management and P&L surface**. It does not stream market data,
compute MACD, or place orders — the SRS requires those to run against live broker
WebSocket feeds with no synthetic data, which is the engine's job. The page is
the layer the desk reads: what is configured, what filled, and what was actually
kept after charges.

## Files

```
macd_desk/charges.py        the cost model — single source of truth
macd_desk/state.py          desk configuration, validation, atomic persistence
macd_desk/formatting.py     rupee formatting with Indian digit grouping
macd_desk/app.py            Flask routes and the view model
macd_desk/__main__.py       CLI — port, host, state file
macd_desk/templates/        server-rendered page
macd_desk/static/           stylesheet + the one small script
tests/                      unittest suite (no pytest needed)
preview/                    static bundle for the shareable page (see below)
build/artifact.py           assembles preview/ into one self-contained file
```

### About `preview/`

The shareable link is a static page with no server behind it, so it carries a
JavaScript port of the cost model. That is a drift risk, so
`tests/test_parity.py` runs both implementations over the same cases and fails if
they disagree by a paisa. The Python model is the product; the preview bundle
only exists to keep that link working.
