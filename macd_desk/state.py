"""Desk state: instrument configuration, the trade blotter, and the rate card.

State is a single JSON document on disk, so a desk's setup survives a restart
and can be version-controlled or handed to someone else. This is a
single-operator local tool — there is no per-user session.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping

from . import charges

DEFAULT_STATE_PATH = Path("desk-state.json")

EXECUTION_MODES = ("live", "paper")
TIMEFRAMES = ("1m", "5m")
SIDES = ("CE", "PE")
EXIT_REASONS = ("Target", "Reversal", "EOD close")
BOOKS = ("paper", "live")

INSTRUMENT_NUMERIC_FIELDS = ("lots", "targetPoints", "lotSize")
TRADE_NUMERIC_FIELDS = ("entryPrice", "exitPrice", "lots", "lotSize")


def _instrument(symbol, kind, lot_size, lots, target_points, side, mode):
    """Configuration only. Prices are never stored here — they come from the market.

    There is no timeframe field: every symbol runs the 1-minute and 5-minute
    engines at once.
    """
    return {
        "symbol": symbol, "kind": kind, "lotSize": lot_size, "lots": lots,
        "targetPoints": target_points, "side": side, "mode": mode,
    }


def default_instruments() -> List[Dict[str, Any]]:
    lot = charges.DEFAULT_LOT_SIZES
    return [
        _instrument("NIFTY", "Index", lot["NIFTY"], 1, 20, "CE", "paper"),
        _instrument("BANKNIFTY", "Index", lot["BANKNIFTY"], 1, 35, "PE", "paper"),
        _instrument("FINNIFTY", "Index", lot["FINNIFTY"], 1, 25, "CE", "paper"),
        _instrument("RELIANCE", "Momentum", 500, 1, 12, "CE", "paper"),
        _instrument("HDFCBANK", "Momentum", 550, 2, 10, "PE", "paper"),
        _instrument("TATAMOTORS", "Momentum", 800, 1, 8, "CE", "paper"),
    ]


def default_state() -> Dict[str, Any]:
    """A fresh desk: instruments configured, blotter empty.

    The blotter only ever fills with trades the engine actually executed —
    there is no sample or demo data anywhere in this app.
    """
    return {
        "rates": dict(charges.DEFAULT_RATES),
        "instruments": default_instruments(),
        "trades": [],
    }


# --------------------------------------------------------------- validation

def _choice(value: Any, allowed, fallback: str) -> str:
    return value if value in allowed else fallback


def clean_instrument(raw: Mapping[str, Any], fallback: Mapping[str, Any]) -> Dict[str, Any]:
    out = {
        "symbol": str(raw.get("symbol") or fallback["symbol"])[:24],
        "kind": str(raw.get("kind") or fallback.get("kind") or "Index")[:16],
        "side": _choice(raw.get("side"), SIDES, fallback.get("side", "CE")),
        "mode": _choice(raw.get("mode"), EXECUTION_MODES, fallback.get("mode", "paper")),
    }
    for field in INSTRUMENT_NUMERIC_FIELDS:
        out[field] = max(0.0, charges._num(raw.get(field), float(fallback.get(field, 0))))
    return out


def clean_trade(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Normalise one executed round trip. Only the engine writes these."""
    out = {
        "symbol": str(raw.get("symbol") or "NIFTY")[:24],
        "side": _choice(raw.get("side"), SIDES, "CE"),
        "reason": _choice(raw.get("reason"), EXIT_REASONS, "Target"),
        "timeframe": _choice(raw.get("timeframe"), TIMEFRAMES, "5m"),
        # Which book the fill belongs to — simulated against live quotes, or real.
        "mode": _choice(raw.get("mode"), BOOKS, "paper"),
        "contract": str(raw.get("contract") or "")[:48],
        "strike": charges._num(raw.get("strike")),
        # Both legs are timestamped. `at` is the older single-field form and is
        # read as the sell time so existing books keep working.
        "entryAt": str(raw.get("entryAt") or "")[:19],
        "exitAt": str(raw.get("exitAt") or raw.get("at") or "")[:19],
    }
    for field in TRADE_NUMERIC_FIELDS:
        out[field] = max(0.0, charges._num(raw.get(field)))
    return out


def clean_state(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Normalise anything that arrives from a form, the API, or the state file."""
    base = default_state()
    if not isinstance(raw, Mapping):
        return base

    instruments = raw.get("instruments")
    if isinstance(instruments, list) and instruments:
        defaults = base["instruments"]
        cleaned = []
        for index, item in enumerate(instruments):
            if isinstance(item, Mapping):
                fallback = defaults[index] if index < len(defaults) else defaults[0]
                cleaned.append(clean_instrument(item, fallback))
        base["instruments"] = cleaned or base["instruments"]

    trades = raw.get("trades")
    if isinstance(trades, list):
        base["trades"] = [clean_trade(t) for t in trades if isinstance(t, Mapping)]

    base["rates"] = charges.resolve_rates(raw.get("rates") if isinstance(raw.get("rates"), Mapping) else None)
    return base


# --------------------------------------------------------------- persistence

def load(path: Path) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return clean_state(json.load(handle))
    except FileNotFoundError:
        return default_state()
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        # A corrupt state file must not take the desk down.
        return default_state()


def save(path: Path, state: Mapping[str, Any]) -> None:
    """Write atomically — a half-written state file would be lost configuration."""
    path = Path(path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent or "."), delete=False, suffix=".tmp")
    try:
        json.dump(state, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, path)
