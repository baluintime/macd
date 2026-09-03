"""The trading rules, as pure decisions — no I/O, no broker.

Every decision comes from the MACD of the **underlying** index or stock. The
option is only the instrument the view is expressed through; its premium never
drives a signal.

  MACD line above the signal line  → hold a CE
  MACD line below the signal line  → hold a PE
  Underlying moves target points the right way → take the profit

The engine reconciles to the *current* stance rather than firing on each
crossover it happens to see. Replaying a day of candles therefore leaves one
position — the one the market is in now — instead of trading every crossover
that already happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from .indicators import Macd, MacdPoint, crossover

ENTER, EXIT = "ENTER", "EXIT"
BULLISH, BEARISH = "BULLISH", "BEARISH"
SIDE_FOR_SIGNAL = {BULLISH: "CE", BEARISH: "PE"}


@dataclass
class Decision:
    """One thing the engine wants done. `side` is the option leg, CE or PE."""
    kind: str            # ENTER or EXIT
    side: str            # CE or PE
    reason: str          # "Reversal", "Target", "EOD close"
    at: Optional[datetime] = None
    detail: str = ""


@dataclass
class Position:
    side: str
    lots: float
    lot_size: float
    entry_price: float
    target_points: float
    instrument_key: str = ""
    trading_symbol: str = ""
    # Carried from the selected contract so the desk can show what is held.
    symbol: str = ""
    strike: float = 0.0
    expiry: str = ""
    delta: float = 0.0
    entry_time: Optional[datetime] = None
    last_price: float = 0.0
    # The underlying's level when the position was opened — the target is
    # measured against this, in index/stock points, not in premium.
    entry_spot: float = 0.0
    last_spot: float = 0.0

    @property
    def label(self) -> str:
        if self.trading_symbol:
            return self.trading_symbol
        strike = f"{self.strike:g}" if self.strike else ""
        return " ".join(part for part in (self.symbol, strike, self.side) if part)

    @property
    def quantity(self) -> float:
        return self.lots * self.lot_size

    @property
    def target_spot(self) -> float:
        """The underlying level that closes this position in profit."""
        if self.side == "CE":
            return self.entry_spot + self.target_points
        return self.entry_spot - self.target_points

    def target_reached(self, spot: float) -> bool:
        if not self.target_points or not self.entry_spot:
            return False
        return spot >= self.target_spot if self.side == "CE" else spot <= self.target_spot

    def unrealised(self, price: Optional[float] = None) -> float:
        price = self.last_price if price is None else price
        return (price - self.entry_price) * self.quantity

    def public(self) -> dict:
        return {
            "side": self.side,
            "optionType": "Call" if self.side == "CE" else "Put",
            "tradingSymbol": self.trading_symbol,
            "label": self.label,
            "strike": self.strike,
            "expiry": self.expiry,
            "delta": round(self.delta, 4),
            "lots": self.lots,
            "quantity": self.quantity,
            "entryPrice": self.entry_price,
            "lastPrice": self.last_price,
            "entrySpot": self.entry_spot,
            "lastSpot": self.last_spot,
            "targetSpot": self.target_spot,
            "unrealised": self.unrealised(),
            "entryTime": self.entry_time.strftime("%H:%M:%S") if self.entry_time else "",
        }


@dataclass
class InstrumentStrategy:
    """MACD state and open position for one symbol on one timeframe."""

    symbol: str
    timeframe: str
    target_points: float
    lots: float
    lot_size: float
    macd: Macd = field(default_factory=Macd)
    position: Optional[Position] = None
    previous: Optional[MacdPoint] = None
    last_candle_at: Optional[datetime] = None
    last_signal: str = ""
    last_signal_at: Optional[datetime] = None
    warmed_up: bool = False

    # ------------------------------------------------------------- warmup

    def symbol_label(self) -> str:
        return self.symbol

    def warmup(self, closes) -> None:
        """Replay historical closes so the first live candle already has a MACD.

        The SRS requires 3 days of history before the 09:15 open for exactly
        this reason: MACD 12/26/9 needs 34 candles before its first value.
        """
        self.macd = Macd()
        for close in closes:
            point = self.macd.update(close)
            if point is not None:
                self.previous = point
        self.warmed_up = self.macd.ready

    @property
    def needs_candles(self) -> int:
        return self.macd.warmup_candles

    # ------------------------------------------------------------- signals

    def feed(self, close: float, at: Optional[datetime] = None) -> Optional[str]:
        """Take one *closed* underlying candle. Returns a crossover, if any.

        Feeding never trades — it only advances the indicator. What to hold is
        decided afterwards by `reconcile`, from the resulting stance.
        """
        point = self.macd.update(close)
        self.last_candle_at = at
        if point is None:
            return None

        signal = crossover(self.previous, point)
        self.previous = point
        self.warmed_up = True
        if signal:
            self.last_signal = signal
            self.last_signal_at = at
        return signal

    @property
    def stance(self) -> Optional[str]:
        """The side the underlying's MACD says to hold, or None while warming up."""
        if self.previous is None:
            return None
        if self.previous.histogram > 0:
            return "CE"
        if self.previous.histogram < 0:
            return "PE"
        return None

    def reconcile(self, at: Optional[datetime] = None) -> List[Decision]:
        """Bring the position in line with the current stance — at most one move."""
        wanted = self.stance
        if wanted is None:
            return []

        point = self.previous
        detail = f"MACD {point.macd:+.2f} vs signal {point.signal:+.2f}"

        if self.position is None:
            return [Decision(ENTER, wanted, "Reversal", at, detail)]
        if self.position.side == wanted:
            return []
        return [Decision(EXIT, self.position.side, "Reversal", at, detail),
                Decision(ENTER, wanted, "Reversal", at, detail)]

    def on_underlying_price(self, spot: float,
                            at: Optional[datetime] = None) -> List[Decision]:
        """Target exit, measured on the underlying in index/stock points."""
        if self.position is None:
            return []
        self.position.last_spot = float(spot)
        if self.position.target_reached(spot):
            return [Decision(EXIT, self.position.side, "Target", at,
                             f"{self.symbol} {spot:.2f} reached target "
                             f"{self.position.target_spot:.2f}")]
        return []

    def close_out(self, at: Optional[datetime] = None) -> List[Decision]:
        """Square off at the end of the session."""
        if self.position is None:
            return []
        return [Decision(EXIT, self.position.side, "EOD close", at)]

    # ------------------------------------------------------------ bookkeeping

    def open_position(self, side: str, price: float, instrument_key: str = "",
                      trading_symbol: str = "", at: Optional[datetime] = None,
                      contract=None, spot: float = 0.0) -> Position:
        """Open a position, carrying the contract's identity when one was selected."""
        self.position = Position(
            side=side, lots=self.lots, lot_size=self.lot_size, entry_price=float(price),
            target_points=self.target_points, entry_time=at, last_price=float(price),
            instrument_key=instrument_key or getattr(contract, "instrument_key", ""),
            trading_symbol=trading_symbol or getattr(contract, "trading_symbol", ""),
            symbol=getattr(contract, "symbol", self.symbol),
            strike=float(getattr(contract, "strike", 0.0) or 0.0),
            expiry=str(getattr(contract, "expiry", "") or ""),
            delta=float(getattr(contract, "delta", 0.0) or 0.0),
            entry_spot=float(spot or getattr(contract, "spot", 0.0) or 0.0),
            last_spot=float(spot or getattr(contract, "spot", 0.0) or 0.0))
        return self.position

    def close_position(self, price: float) -> Optional[Position]:
        closed, self.position = self.position, None
        if closed is not None:
            closed.last_price = float(price)
        return closed

    def status(self) -> dict:
        point = self.previous
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "warmedUp": self.warmed_up,
            "macd": round(point.macd, 3) if point else None,
            "signal": round(point.signal, 3) if point else None,
            "histogram": round(point.histogram, 3) if point else None,
            "stance": self.stance,
            "lastSignal": self.last_signal,
            "lastCandleAt": self.last_candle_at.strftime("%H:%M") if self.last_candle_at else "",
            "position": self.position.public() if self.position else None,
        }
