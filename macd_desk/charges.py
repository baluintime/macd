"""Upstox (NSE F&O) cost model and net-profit calculator.

Pure functions over plain dicts — no Flask, no I/O. This module is the single
source of truth for the money maths; the web layer only formats what it returns.

Every charge below is levied on *premium* turnover, never on notional.
"""

from __future__ import annotations

import math
import sys
from typing import Any, Dict, Iterable, List, Mapping, Optional

# Indicative Upstox charges for NSE index/stock options, in percent of the
# turnover each one applies to. Verify against the live tariff sheet before
# trusting these for accounting.
DEFAULT_RATES: Dict[str, float] = {
    "brokeragePerOrder": 20.0,   # flat Rs per executed order
    "brokeragePctCap": 2.5,      # ...or % of premium turnover, whichever is lower
    "sttSellPct": 0.1,           # STT, sell side only, on premium
    "exchangeTxnPct": 0.03503,   # NSE options exchange transaction charge
    "ipftPct": 0.0005,           # NSE Investor Protection Fund Trust
    "sebiPct": 0.0001,           # SEBI turnover fee (Rs 10 per crore)
    "stampDutyBuyPct": 0.003,    # stamp duty, buy side only
    "gstPct": 18.0,              # GST on brokerage + exchange + SEBI + IPFT
}

# Exchange lot sizes are revised periodically — these are defaults, not law.
DEFAULT_LOT_SIZES: Dict[str, int] = {
    "NIFTY": 75,
    "BANKNIFTY": 35,
    "FINNIFTY": 65,
    "MIDCPNIFTY": 140,
    "NIFTYNXT50": 25,
}

CHARGE_HEADS = (
    {"key": "brokerage", "label": "Brokerage", "note": "Lower of flat fee or % cap, both legs"},
    {"key": "stt", "label": "STT", "note": "Sell-side premium only"},
    {"key": "exchangeTxn", "label": "Exchange txn", "note": "NSE, on premium turnover"},
    {"key": "gst", "label": "GST", "note": "On brokerage + exchange + SEBI + IPFT"},
    {"key": "stampDuty", "label": "Stamp duty", "note": "Buy side, rounded to the rupee"},
    {"key": "sebi", "label": "SEBI fee", "note": "Turnover fee"},
    {"key": "ipft", "label": "IPFT", "note": "Investor protection fund"},
)

HEAD_KEYS = tuple(h["key"] for h in CHARGE_HEADS)


def _num(value: Any, default: float = 0.0) -> float:
    """Coerce form/JSON input to a float without raising on junk."""
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def half_up(value: float) -> float:
    """Round half away from zero — what a contract note does, not banker's."""
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


# Binary floats land a hair below an exact half (1.005 is really 1.00499…),
# which would round a genuine half down. The nudge is the same one the
# preview bundle applies, so both models agree to the paisa.
_EPSILON = sys.float_info.epsilon


def round2(value: float) -> float:
    return half_up((value + _EPSILON) * 100) / 100


def _pct(value: float, rate: float) -> float:
    return value * (rate / 100.0)


def resolve_rates(overrides: Optional[Mapping[str, Any]] = None) -> Dict[str, float]:
    rates = dict(DEFAULT_RATES)
    for key, value in (overrides or {}).items():
        if key in DEFAULT_RATES:
            rates[key] = _num(value, DEFAULT_RATES[key])
    return rates


def _brokerage_for_order(turnover: float, rates: Mapping[str, float]) -> float:
    """Upstox bills the lower of the flat per-order fee and the % cap."""
    return min(rates["brokeragePerOrder"], _pct(turnover, rates["brokeragePctCap"]))


def compute_trade(trade: Mapping[str, Any],
                  rate_overrides: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Cost one round trip (a buy order and a sell order) on an option.

    Returns gross P&L, each charge head, the total, the net, and the break-even
    move in points.
    """
    rates = resolve_rates(rate_overrides)

    qty = _num(trade.get("lots")) * _num(trade.get("lotSize"))
    entry = _num(trade.get("entryPrice"))
    exit_price = _num(trade.get("exitPrice"))

    buy_turnover = entry * qty
    sell_turnover = exit_price * qty
    turnover = buy_turnover + sell_turnover

    gross = sell_turnover - buy_turnover

    brokerage = (_brokerage_for_order(buy_turnover, rates)
                 + _brokerage_for_order(sell_turnover, rates))
    stt = _pct(sell_turnover, rates["sttSellPct"])
    exchange_txn = _pct(turnover, rates["exchangeTxnPct"])
    ipft = _pct(turnover, rates["ipftPct"])
    sebi = _pct(turnover, rates["sebiPct"])
    # Stamp duty is levied on the buy leg and rounded to the nearest rupee.
    stamp_duty = half_up(_pct(buy_turnover, rates["stampDutyBuyPct"]))
    gst = _pct(brokerage + exchange_txn + sebi + ipft, rates["gstPct"])

    charges = {
        "brokerage": round2(brokerage),
        "stt": round2(stt),
        "exchangeTxn": round2(exchange_txn),
        "gst": round2(gst),
        "sebi": round2(sebi),
        "ipft": round2(ipft),
        "stampDuty": round2(stamp_duty),
    }
    total_charges = round2(sum(charges.values()))

    return {
        "qty": qty,
        "buyTurnover": round2(buy_turnover),
        "sellTurnover": round2(sell_turnover),
        "turnover": round2(turnover),
        "grossPnl": round2(gross),
        "charges": charges,
        "totalCharges": total_charges,
        "netPnl": round2(gross - total_charges),
        # Points the option must move just to cover costs.
        "breakEvenPoints": round2(total_charges / qty) if qty > 0 else 0.0,
    }


def project_at_target(config: Mapping[str, Any],
                      rate_overrides: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Net profit if a position is closed exactly at its configured target."""
    entry = _num(config.get("entryPrice"))
    return compute_trade(
        {
            "entryPrice": entry,
            "exitPrice": entry + _num(config.get("targetPoints")),
            "lots": config.get("lots"),
            "lotSize": config.get("lotSize"),
        },
        rate_overrides,
    )


def summarize(trades: Iterable[Mapping[str, Any]],
              rate_overrides: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Roll a list of trades into one book-level P&L with per-head charge totals."""
    rows: List[Dict[str, Any]] = []
    totals: Dict[str, Any] = {
        "trades": 0, "wins": 0, "losses": 0,
        "qty": 0.0, "turnover": 0.0, "grossPnl": 0.0,
        "totalCharges": 0.0, "netPnl": 0.0,
        "charges": {key: 0.0 for key in HEAD_KEYS},
    }

    for trade in trades or []:
        costed = compute_trade(trade, rate_overrides)
        row = dict(trade)
        row.update(costed)
        rows.append(row)

        totals["trades"] += 1
        if costed["grossPnl"] >= 0:
            totals["wins"] += 1
        else:
            totals["losses"] += 1
        totals["qty"] += costed["qty"]
        for key in ("turnover", "grossPnl", "totalCharges", "netPnl"):
            totals[key] += costed[key]
        for key in HEAD_KEYS:
            totals["charges"][key] += costed["charges"][key]

    for key in ("turnover", "grossPnl", "totalCharges", "netPnl"):
        totals[key] = round2(totals[key])
    for key in HEAD_KEYS:
        totals["charges"][key] = round2(totals["charges"][key])

    totals["chargeRatioPct"] = (round2(totals["totalCharges"] / totals["grossPnl"] * 100)
                                if totals["grossPnl"] > 0 else 0.0)
    totals["winRatePct"] = (round2(totals["wins"] / totals["trades"] * 100)
                            if totals["trades"] else 0.0)
    totals["avgBreakEvenPoints"] = (round2(totals["totalCharges"] / totals["qty"])
                                    if totals["qty"] > 0 else 0.0)

    return {"rows": rows, "totals": totals}


def split_by_book(trades: Iterable[Mapping[str, Any]],
                  rate_overrides: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Paper and live are separate books — a simulated fill is not a real one.

    Returns each book costed on its own, plus the combined view, so the page
    never adds hypothetical money to real money without saying so.
    """
    trades = list(trades or [])
    books = {}
    for name in ("paper", "live"):
        books[name] = summarize([t for t in trades if t.get("mode", "paper") == name],
                                rate_overrides)
    books["all"] = summarize(trades, rate_overrides)
    return books


def charge_breakdown(totals: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Charge heads ranked by size, with each head's share of the total and of gross."""
    total = totals.get("totalCharges") or 0.0
    gross = totals.get("grossPnl") or 0.0
    heads = [
        {
            "key": head["key"],
            "label": head["label"],
            "note": head["note"],
            "amount": totals["charges"].get(head["key"], 0.0),
        }
        for head in CHARGE_HEADS
    ]
    heads.sort(key=lambda h: h["amount"], reverse=True)

    largest = max((h["amount"] for h in heads), default=0.0) or 1.0
    for head in heads:
        head["sharePct"] = round2(head["amount"] / total * 100) if total else 0.0
        head["grossPct"] = round2(head["amount"] / gross * 100) if gross > 0 else None
        # Bar width, floored so a near-zero head still shows a tick.
        head["barPct"] = max(0.6, head["amount"] / largest * 100)
    return heads
