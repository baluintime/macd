# Upstox MACD Options Desk

Management webpage for the automated multi-timeframe MACD options trading engine
described in the SRS (v1.0.0). It provides the per-instrument controls the spec
calls for, and — the part the spec leaves open — it **costs every trade and
reports the net profit after all broker and statutory charges**.

Open `index.html` in a browser. No build step, no dependencies, no network calls.

```
npm test          # cost-model unit tests (node --test)
npm run serve     # optional: serve on http://localhost:8080
```

## What the page shows

| Block | Purpose |
|---|---|
| **Net profit — session book** | Gross P&L, total charges, **net profit**, and the break-even move in points |
| **Instrument desk** | One card per symbol with the SRS §5 controls, plus the projected net profit if that position exits exactly at its target |
| **Executed trades** | Every MACD-crossover / target exit, costed individually — editable inline |
| **Where the money goes** | Charge heads ranked by size, with each head's share of total charges and of gross profit |
| **Rate card** | Every rate is editable; the whole page recomputes live |

State persists in `localStorage`, so a desk's instrument setup survives a reload.
"Copy CSV" puts the costed blotter on the clipboard for the day book.

### SRS §5 controls, per instrument

| Parameter | Control | Implemented as |
|---|---|---|
| Execution mode | Toggle | Live / Paper segmented control, independent per symbol |
| Position size | Numeric | Lots per signal (× lot size → quantity) |
| Target points | Numeric | Drives the "Net @ target" projection on the card |
| Engine timeframe | Toggle | 1-min / 5-min, independent per symbol |

Option side (CE/PE), lot size and entry premium are also editable, since lot
sizes are revised by the exchange and the premium is what every charge is
levied on.

## The cost model

All maths lives in [`assets/charges.js`](assets/charges.js) — pure functions, no
DOM, shared by the page and the tests. Nothing is levied on notional; options
charges apply to **premium turnover**.

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

Rates are **indicative NSE/Upstox figures for equity and index options** and are
the defaults in `DEFAULT_RATES`; the exchange and the broker revise slabs, so the
rate card on the page overrides them at runtime and
`computeTrade(trade, rateOverrides)` overrides them in code. Re-check against the
live Upstox tariff before using any figure for accounting — these numbers are a
planning aid, not a contract note.

### Why break-even matters to this strategy

A MACD reversal engine flips position on every crossover, so on a choppy day it
pays the round-trip cost repeatedly. The break-even figure is the honest floor:
below that many points of premium movement, a "winning" trade still loses money.
Small lot sizes are where this bites hardest — the flat ₹20 per order does not
shrink with the position.

## API

```js
const { computeTrade, projectAtTarget, summarize } = require('./assets/charges.js');

computeTrade({ entryPrice: 128.2, exitPrice: 148.2, lots: 1, lotSize: 75 });
// → { grossPnl, charges: {…}, totalCharges, netPnl, breakEvenPoints, … }

projectAtTarget({ entryPrice: 142.5, targetPoints: 20, lots: 1, lotSize: 75 });
// net profit if the position exits exactly at its configured target

summarize(trades, rateOverrides);
// → { rows: [costed trades], totals: { grossPnl, totalCharges, netPnl, … } }
```

## Scope

This is the **management and P&L surface**. It does not stream market data,
compute MACD, or place orders — the SRS requires those to run against live
broker WebSocket feeds with no synthetic data, which is the engine's job. The
page is the layer the desk reads: what is configured, what filled, and what was
actually kept after charges.

## Files

```
index.html            page structure
assets/styles.css     tokens + layout, light and dark
assets/charges.js     cost model (browser + node, no dependencies)
assets/app.js         state, rendering, persistence
tests/charges.test.js unit tests for every charge head
```
