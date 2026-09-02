"""The trading rules from the SRS, as pure decisions — no I/O, no broker.

  Bullish crossover  → exit any PE position, buy CE
  Bearish crossover  → exit any CE position, buy PE
  Exit also fires when the option premium reaches the configured target points.

The MACD is computed on the *underlying* (index or stock), while the position
and its target are in the *option*. Both feeds arrive here separately.
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
    entry_time: Optional[datetime] = None
    last_price: float = 0.0

    @property
    def quantity(self) -> float:
        return self.lots * self.lot_size

    @property
    def target_price(self) -> float:
        return self.entry_price + self.target_points

    def unrealised(self, price: Optional[float] = None) -> float:
        price = self.last_price if price is None else price
        return (price - self.entry_price) * self.quantity

    def public(self) -> dict:
        return {
            "side": self.side,
            "tradingSymbol": self.trading_symbol,
            "lots": self.lots,
            "quantity": self.quantity,
            "entryPrice": self.entry_price,
            "lastPrice": self.last_price,
            "targetPrice": self.target_price,
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
    warmed_up: bool = False

    # ------------------------------------------------------------- warmup

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

    def on_closed_candle(self, close: float, at: Optional[datetime] = None) -> List[Decision]:
        """Feed one *closed* candle. Returns what should happen, in order."""
        point = self.macd.update(close)
        self.last_candle_at = at
        if point is None:
            return []

        signal = crossover(self.previous, point)
        self.previous = point
        self.warmed_up = True
        if signal is None:
            return []

        wanted = SIDE_FOR_SIGNAL[signal]
        decisions: List[Decision] = []
        detail = f"MACD {point.macd:+.2f} vs signal {point.signal:+.2f}"

        if self.position is not None:
            if self.position.side == wanted:
                # Already positioned the way the crossover points — nothing to do.
                return []
            decisions.append(Decision(EXIT, self.position.side, "Reversal", at, detail))

        decisions.append(Decision(ENTER, wanted, "Reversal", at, detail))
        return decisions

    def on_price(self, price: float, at: Optional[datetime] = None) -> List[Decision]:
        """Feed the option's live premium; fires the target exit when reached."""
        if self.position is None:
            return []
        self.position.last_price = float(price)
        if self.target_points > 0 and price >= self.position.target_price:
            return [Decision(EXIT, self.position.side, "Target", at,
                             f"{price:.2f} ≥ target {self.position.target_price:.2f}")]
        return []

    def close_out(self, at: Optional[datetime] = None) -> List[Decision]:
        """Square off at the end of the session."""
        if self.position is None:
            return []
        return [Decision(EXIT, self.position.side, "EOD close", at)]

    # ------------------------------------------------------------ bookkeeping

    def open_position(self, side: str, price: float, instrument_key: str = "",
                      trading_symbol: str = "", at: Optional[datetime] = None) -> Position:
        self.position = Position(
            side=side, lots=self.lots, lot_size=self.lot_size, entry_price=float(price),
            target_points=self.target_points, instrument_key=instrument_key,
            trading_symbol=trading_symbol, entry_time=at, last_price=float(price))
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
            "lastCandleAt": self.last_candle_at.strftime("%H:%M") if self.last_candle_at else "",
            "position": self.position.public() if self.position else None,
        }
