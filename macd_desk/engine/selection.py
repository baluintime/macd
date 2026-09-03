"""Contract selection: in-the-money, delta 0.60–0.70, from the live option chain.

An at-the-money option sits near 0.50 delta; asking for 0.60–0.70 means one or
two strikes in the money, which moves closer to the underlying and decays more
slowly than the ATM strike the naive choice would pick.

Delta is read from the live chain (`option_greeks.delta`), never modelled here.
When the chain has no strike inside the band, selection fails loudly and the
engine skips that entry rather than trading a contract nobody asked for.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Mapping, Optional

DEFAULT_DELTA_MIN = 0.60
DEFAULT_DELTA_MAX = 0.70

# Last-resort fallbacks. The instrument master is asked first, so a renamed or
# re-keyed index resolves without a code change.
INDEX_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
    "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT",
    "NIFTYNXT50": "NSE_INDEX|Nifty Next 50",
}


class SelectionError(RuntimeError):
    """No contract met the rules — the caller must not substitute one."""


@dataclass(frozen=True)
class SelectedContract:
    instrument_key: str
    trading_symbol: str
    symbol: str
    side: str
    strike: float
    expiry: Optional[date]
    lot_size: int
    premium: float          # live last traded premium
    delta: float            # live delta from the chain
    spot: float
    in_the_money: bool
    at: Optional[datetime] = None

    @property
    def label(self) -> str:
        """The chain does not always carry a trading symbol — build a readable one."""
        if self.trading_symbol:
            return self.trading_symbol
        strike = f"{self.strike:g}" if self.strike else ""
        return " ".join(part for part in (self.symbol, strike, self.side) if part)

    def public(self) -> dict:
        return {
            "instrumentKey": self.instrument_key,
            "tradingSymbol": self.trading_symbol,
            "label": self.label,
            "side": self.side,
            "strike": self.strike,
            "expiry": self.expiry.isoformat() if self.expiry else "",
            "lotSize": self.lot_size,
            "premium": self.premium,
            "delta": round(self.delta, 4),
            "spot": self.spot,
            "inTheMoney": self.in_the_money,
            "at": self.at.strftime("%H:%M:%S") if self.at else "",
        }


def _get(row: Mapping[str, Any], *names, default=None):
    for name in names:
        if isinstance(row, Mapping) and row.get(name) not in (None, ""):
            return row[name]
    return default


def _as_date(value: Any) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def is_in_the_money(side: str, strike: float, spot: float) -> bool:
    return strike < spot if side == "CE" else strike > spot


class ContractSelector:
    """Resolves a symbol to the option contract the strategy should trade."""

    def __init__(self, client, delta_min: float = DEFAULT_DELTA_MIN,
                 delta_max: float = DEFAULT_DELTA_MAX):
        self.client = client
        self.delta_min = delta_min
        self.delta_max = delta_max
        self._underlying_cache: Dict[str, str] = {}
        self._expiry_cache: Dict[str, tuple] = {}

    # --------------------------------------------------------- underlying

    def underlying_key(self, symbol: str) -> Optional[str]:
        """Resolve a symbol to its Upstox instrument key, from Upstox itself.

        The instrument master is authoritative: an index that is re-keyed or a
        stock that is renamed resolves without editing this file. The built-in
        index map is only a fallback for when the master cannot be reached.
        """
        symbol = symbol.upper().strip()
        if symbol in self._underlying_cache:
            return self._underlying_cache[symbol]

        try:
            key = self._search_master(symbol)
        except Exception:
            key = None                      # fall through to the built-in map
        if key is None:
            key = INDEX_KEYS.get(symbol)
        if key:
            self._underlying_cache[symbol] = key
        return key

    def _search_master(self, symbol: str) -> Optional[str]:
        """An index or equity row whose trading symbol or name matches exactly."""
        index_hit = None
        for row in self.client.instruments():
            segment = str(_get(row, "segment", "exchange", default="")).upper()
            if not (segment.startswith("NSE_EQ") or segment.startswith("NSE_INDEX")):
                continue
            key = str(_get(row, "instrument_key", "instrumentKey", default=""))
            if not key:
                continue

            trading = str(_get(row, "trading_symbol", "tradingsymbol", default="")).upper()
            name = str(_get(row, "name", default="")).upper()
            if segment.startswith("NSE_EQ") and trading == symbol:
                return key
            if segment.startswith("NSE_INDEX") and index_hit is None:
                # Index names carry spaces ("Nifty Bank" for BANKNIFTY), so
                # compare with those removed.
                squashed = {trading.replace(" ", ""), name.replace(" ", "")}
                if symbol in squashed or INDEX_KEYS.get(symbol) == key:
                    index_hit = key
        return index_hit

    def nearest_expiry(self, underlying_key: str, today: Optional[date] = None) -> Optional[date]:
        today = today or date.today()
        cached = self._expiry_cache.get(underlying_key)
        if cached and cached[0] == today:
            return cached[1]

        expiries = sorted({
            expiry for expiry in (
                _as_date(_get(row, "expiry", "expiry_date"))
                for row in self.client.option_contracts(underlying_key))
            if expiry and expiry >= today
        })
        expiry = expiries[0] if expiries else None
        self._expiry_cache[underlying_key] = (today, expiry)
        return expiry

    # ---------------------------------------------------------- selection

    def chain_rows(self, underlying_key: str, expiry: date) -> List[Dict[str, Any]]:
        return self.client.option_chain(underlying_key, expiry)

    def candidates(self, rows, side: str) -> List[Dict[str, Any]]:
        """Flatten the chain into one row per strike on the requested side."""
        leg_key = "call_options" if side == "CE" else "put_options"
        out: List[Dict[str, Any]] = []
        for row in rows:
            leg = row.get(leg_key) or {}
            greeks = leg.get("option_greeks") or {}
            market = leg.get("market_data") or {}
            delta = greeks.get("delta")
            premium = _get(market, "ltp", "last_price", "close_price")
            if delta is None or premium in (None, 0):
                continue
            out.append({
                "instrument_key": _get(leg, "instrument_key", "instrumentKey", default=""),
                "trading_symbol": _get(leg, "trading_symbol", "tradingsymbol", default=""),
                "strike": float(_get(row, "strike_price", "strike", default=0) or 0),
                "expiry": _as_date(_get(row, "expiry")),
                "spot": float(_get(row, "underlying_spot_price", "spot_price", default=0) or 0),
                "lot_size": int(float(_get(row, "lot_size", default=0)
                                      or _get(leg, "lot_size", default=0) or 0)),
                "premium": float(premium),
                "delta": float(delta),
            })
        return out

    def select(self, symbol: str, side: str, today: Optional[date] = None,
               now: Optional[datetime] = None) -> SelectedContract:
        side = side.upper()
        underlying = self.underlying_key(symbol)
        if not underlying:
            raise SelectionError(f"No underlying instrument key for {symbol}.")

        expiry = self.nearest_expiry(underlying, today)
        if expiry is None:
            raise SelectionError(f"No live expiry found for {symbol}.")

        rows = self.chain_rows(underlying, expiry)
        candidates = self.candidates(rows, side)
        if not candidates:
            raise SelectionError(
                f"The {symbol} {expiry} chain carried no {side} greeks — cannot select a strike.")

        spot = next((c["spot"] for c in candidates if c["spot"]), 0.0)
        # Delta is negative for puts; the band is on its magnitude.
        in_band = [c for c in candidates
                   if self.delta_min <= abs(c["delta"]) <= self.delta_max
                   and is_in_the_money(side, c["strike"], spot or c["spot"])]
        if not in_band:
            observed = sorted(abs(c["delta"]) for c in candidates)
            raise SelectionError(
                f"No in-the-money {symbol} {side} strike with delta "
                f"{self.delta_min:.2f}–{self.delta_max:.2f} "
                f"(chain deltas {observed[0]:.2f}–{observed[-1]:.2f}).")

        target = (self.delta_min + self.delta_max) / 2
        best = min(in_band, key=lambda c: abs(abs(c["delta"]) - target))
        return SelectedContract(
            instrument_key=best["instrument_key"],
            trading_symbol=best["trading_symbol"],
            symbol=symbol.upper(),
            side=side,
            strike=best["strike"],
            expiry=best["expiry"] or expiry,
            lot_size=best["lot_size"],
            premium=best["premium"],
            delta=best["delta"],
            spot=spot or best["spot"],
            in_the_money=True,
            at=now or datetime.now(),
        )
