"""MACD, the way the SRS specifies it: fast 12, slow 26, signal 9.

Both an incremental form (one candle at a time, for the live loop) and a batch
form (a whole warmup window, for backfill and tests). Each EMA is seeded with a
simple average of its first `period` values, which is what charting platforms
do — seeding from a single value would leave the first hour of signals skewed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

FAST_PERIOD = 12
SLOW_PERIOD = 26
SIGNAL_PERIOD = 9


class Ema:
    """Exponential moving average, seeded from the SMA of the first `period`."""

    def __init__(self, period: int):
        if period < 1:
            raise ValueError("EMA period must be positive")
        self.period = period
        self.multiplier = 2.0 / (period + 1)
        self.value: Optional[float] = None
        self._seed: List[float] = []

    def update(self, price: float) -> Optional[float]:
        price = float(price)
        if self.value is None:
            self._seed.append(price)
            if len(self._seed) < self.period:
                return None
            self.value = sum(self._seed) / self.period
            return self.value
        self.value = (price - self.value) * self.multiplier + self.value
        return self.value

    @property
    def ready(self) -> bool:
        return self.value is not None


@dataclass(frozen=True)
class MacdPoint:
    macd: float
    signal: float
    histogram: float


class Macd:
    """Incremental MACD. `update` returns None until enough candles have arrived."""

    def __init__(self, fast: int = FAST_PERIOD, slow: int = SLOW_PERIOD,
                 signal: int = SIGNAL_PERIOD):
        if fast >= slow:
            raise ValueError("fast period must be shorter than slow period")
        self.fast = Ema(fast)
        self.slow = Ema(slow)
        self.signal = Ema(signal)
        self.last: Optional[MacdPoint] = None

    def update(self, close: float) -> Optional[MacdPoint]:
        fast = self.fast.update(close)
        slow = self.slow.update(close)
        if fast is None or slow is None:
            return None

        macd_line = fast - slow
        signal_line = self.signal.update(macd_line)
        if signal_line is None:
            return None

        self.last = MacdPoint(macd_line, signal_line, macd_line - signal_line)
        return self.last

    @property
    def ready(self) -> bool:
        return self.last is not None

    @property
    def warmup_candles(self) -> int:
        """Candles needed before the first value — why the SRS wants 3 days."""
        return self.slow.period + self.signal.period - 1


def macd_series(closes: Sequence[float], fast: int = FAST_PERIOD, slow: int = SLOW_PERIOD,
                signal: int = SIGNAL_PERIOD) -> List[Optional[MacdPoint]]:
    """MACD for a whole series — one entry per close, None while warming up."""
    indicator = Macd(fast, slow, signal)
    return [indicator.update(close) for close in closes]


def crossover(previous: Optional[MacdPoint], current: Optional[MacdPoint]) -> Optional[str]:
    """"BULLISH" when the MACD line crosses above the signal line, "BEARISH" below.

    A touch (histogram exactly zero) is not a crossing until it resolves to a
    side, so the engine cannot be whipsawed by a flat print.
    """
    if previous is None or current is None:
        return None
    if previous.histogram <= 0 < current.histogram:
        return "BULLISH"
    if previous.histogram >= 0 > current.histogram:
        return "BEARISH"
    return None
