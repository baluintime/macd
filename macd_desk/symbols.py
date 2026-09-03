"""Symbol lookup: `python -m macd_desk.symbols TATA`.

Tickers change — a demerger, a rename, a fresh listing — and the desk can only
trade a symbol the NSE equity master still carries. This searches that master so
a "No instrument key" error can be fixed without guessing.
"""

from __future__ import annotations

import sys
from typing import Any, List, Mapping, Optional

from .broker.upstox import UpstoxClient, UpstoxError
from .config import Settings, load_settings
from .engine.selection import INDEX_KEYS


def _field(row: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return str(value)
    return ""


def search(rows, query: str, limit: int = 25) -> List[dict]:
    """Equity rows whose symbol or name contains the query, best matches first."""
    query = query.upper().strip()
    hits = []
    for row in rows:
        segment = _field(row, "segment", "exchange").upper()
        if not segment.startswith("NSE_EQ"):
            continue
        symbol = _field(row, "trading_symbol", "tradingsymbol", "name").upper()
        name = _field(row, "name", "short_name")
        if query in symbol or query in name.upper():
            hits.append({
                "symbol": symbol,
                "name": name,
                "instrument_key": _field(row, "instrument_key", "instrumentKey"),
                # An exact hit should not be buried under partial ones.
                "rank": 0 if symbol == query else 1 if symbol.startswith(query) else 2,
            })
    hits.sort(key=lambda hit: (hit["rank"], hit["symbol"]))
    return hits[:limit]


def run(query: str, settings: Optional[Settings] = None) -> int:
    settings = settings or load_settings()

    matches = [name for name in INDEX_KEYS if query.upper() in name]
    if matches:
        print("Index symbols (built in, no lookup needed):")
        for name in matches:
            print(f"  {name:<12} {INDEX_KEYS[name]}")
        print()

    client = UpstoxClient(settings.upstox)
    if client.current_token() is None:
        print("Not connected to Upstox — connect from /broker first "
              "(the instrument master is fetched over the API).")
        return 1

    try:
        rows = client.instruments()
    except UpstoxError as error:
        print(f"Could not download the instrument master: {error}")
        return 1

    hits = search(rows, query)
    if not hits:
        print(f"No NSE equity symbol matches {query!r}.")
        print("Tickers change after a demerger or rename — try a shorter fragment.")
        return 1

    print(f"NSE equity symbols matching {query!r}:\n")
    print(f"  {'SYMBOL':<18} {'INSTRUMENT KEY':<22} NAME")
    for hit in hits:
        print(f"  {hit['symbol']:<18} {hit['instrument_key']:<22} {hit['name']}")
    print("\nPut the SYMBOL on the desk exactly as shown.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python -m macd_desk.symbols <part of a symbol or company name>")
        sys.exit(2)
    sys.exit(run(" ".join(sys.argv[1:])))
