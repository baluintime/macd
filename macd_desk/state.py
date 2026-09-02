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

INSTRUMENT_NUMERIC_FIELDS = ("lots", "targetPoints", "lotSize", "entryPrice")
TRADE_NUMERIC_FIELDS = ("entryPrice", "exitPrice", "lots", "lotSize")


def _instrument(symbol, kind, lot_size, lots, target_points, side, mode, timeframe, entry_price):
    return {
        "symbol": symbol, "kind": kind, "lotSize": lot_size, "lots": lots,
        "targetPoints": target_points, "side": side, "mode": mode,
        "timeframe": timeframe, "entryPrice": entry_price,
    }


def default_instruments() -> List[Dict[str, Any]]:
    lot = charges.DEFAULT_LOT_SIZES
    return [
        _instrument("NIFTY", "Index", lot["NIFTY"], 1, 20, "CE", "live", "5m", 142.50),
        _instrument("BANKNIFTY", "Index", lot["BANKNIFTY"], 1, 35, "PE", "live", "5m", 268.00),
        _instrument("FINNIFTY", "Index", lot["FINNIFTY"], 1, 25, "CE", "paper", "1m", 96.75),
        _instrument("RELIANCE", "Momentum", 500, 1, 12, "CE", "paper", "5m", 38.40),
        _instrument("HDFCBANK", "Momentum", 550, 2, 10, "PE", "paper", "1m", 24.15),
        _instrument("TATAMOTORS", "Momentum", 800, 1, 8, "CE", "paper", "1m", 17.90),
    ]


def _trade(symbol, side, reason, entry, exit_price, lots, lot_size, timeframe):
    return {
        "symbol": symbol, "side": side, "reason": reason,
        "entryPrice": entry, "exitPrice": exit_price,
        "lots": lots, "lotSize": lot_size, "timeframe": timeframe,
    }


def sample_trades() -> List[Dict[str, Any]]:
    """A plausible session, so the page opens showing what it does."""
    return [
        _trade("NIFTY", "CE", "Target", 128.20, 148.20, 1, 75, "5m"),
        _trade("NIFTY", "PE", "Reversal", 112.60, 98.35, 1, 75, "5m"),
        _trade("BANKNIFTY", "PE", "Target", 245.00, 280.00, 1, 35, "5m"),
        _trade("BANKNIFTY", "CE", "Reversal", 262.40, 251.10, 1, 35, "5m"),
        _trade("FINNIFTY", "CE", "Target", 88.10, 113.10, 1, 65, "1m"),
        _trade("RELIANCE", "CE", "Reversal", 41.25, 37.80, 1, 500, "5m"),
        _trade("HDFCBANK", "PE", "Target", 22.40, 32.40, 2, 550, "1m"),
    ]


def default_state() -> Dict[str, Any]:
    return {
        "rates": dict(charges.DEFAULT_RATES),
        "instruments": default_instruments(),
        "trades": sample_trades(),
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
        "timeframe": _choice(raw.get("timeframe"), TIMEFRAMES, fallback.get("timeframe", "5m")),
    }
    for field in INSTRUMENT_NUMERIC_FIELDS:
        out[field] = max(0.0, charges._num(raw.get(field), float(fallback.get(field, 0))))
    return out


def clean_trade(raw: Mapping[str, Any]) -> Dict[str, Any]:
    out = {
        "symbol": str(raw.get("symbol") or "NIFTY")[:24],
        "side": _choice(raw.get("side"), SIDES, "CE"),
        "reason": _choice(raw.get("reason"), EXIT_REASONS, "Target"),
        "timeframe": _choice(raw.get("timeframe"), TIMEFRAMES, "5m"),
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
