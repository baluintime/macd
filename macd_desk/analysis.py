"""The MACD readout: every intermediate value, plus the geometry to plot it.

This exists so the engine's arithmetic can be checked against a charting
platform. Nothing here decides a trade — it reports what the indicator saw.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from .engine.indicators import FAST_PERIOD, SIGNAL_PERIOD, SLOW_PERIOD, Macd, macd_rows

CHART_CANDLES = 120          # the tail that stays legible at a glance
CHART_WIDTH = 1200
CHART_HEIGHT = 220
CHART_PAD_LEFT = 46
CHART_PAD_RIGHT = 10
CHART_PAD_Y = 14


def build_rows(candles: Sequence[Sequence[Any]]) -> List[dict]:
    return macd_rows(candles)


def _nice_ticks(low: float, high: float, count: int = 4) -> List[float]:
    """Round tick values spanning the data, so every label names a real level."""
    if high <= low:
        return [low]
    raw = (high - low) / count
    magnitude = 10 ** (len(str(int(abs(raw)))) - 1) if abs(raw) >= 1 else 0.1
    while magnitude * 10 <= raw:
        magnitude *= 10
    step = magnitude
    for candidate in (magnitude, magnitude * 2, magnitude * 2.5, magnitude * 5, magnitude * 10):
        if candidate >= raw:
            step = candidate
            break
    start = step * int(low / step)
    ticks, value = [], start
    while value <= high + step / 2:
        if low - step / 2 <= value <= high + step / 2:
            ticks.append(round(value, 6))
        value += step
    return ticks or [low, high]


def build_chart(rows: Sequence[Mapping[str, Any]],
                limit: int = CHART_CANDLES) -> Optional[Dict[str, Any]]:
    """MACD line, signal line and histogram on one shared scale.

    Two lines and the bars are all in the same units, so they share one axis —
    a second scale would make the crossings meaningless.
    """
    plotted = [row for row in rows if row["macd"] is not None][-limit:]
    if len(plotted) < 2:
        return None

    values = [row["macd"] for row in plotted] + [row["signal"] for row in plotted] \
        + [row["histogram"] for row in plotted]
    low, high = min(values), max(values)
    if low == high:
        low, high = low - 1, high + 1
    span = high - low
    low -= span * 0.08
    high += span * 0.08

    inner_width = CHART_WIDTH - CHART_PAD_LEFT - CHART_PAD_RIGHT
    inner_height = CHART_HEIGHT - CHART_PAD_Y * 2
    step = inner_width / max(1, len(plotted) - 1)

    def x_at(index: int) -> float:
        return round(CHART_PAD_LEFT + index * step, 2)

    def y_at(value: float) -> float:
        return round(CHART_PAD_Y + (high - value) / (high - low) * inner_height, 2)

    bar_width = max(1.0, min(6.0, step * 0.55))
    zero_y = y_at(0)
    bars = []
    for index, row in enumerate(plotted):
        y = y_at(row["histogram"])
        bars.append({
            "x": round(x_at(index) - bar_width / 2, 2),
            "y": round(min(y, zero_y), 2),
            "height": round(max(1.0, abs(zero_y - y)), 2),
            "width": round(bar_width, 2),
            "positive": row["histogram"] >= 0,
            "at": row["at"],
            "value": round(row["histogram"], 3),
        })

    def path(key: str) -> str:
        return " ".join(
            f"{'M' if index == 0 else 'L'}{x_at(index)},{y_at(row[key])}"
            for index, row in enumerate(plotted))

    crossings = [
        {"x": x_at(index), "kind": row["cross"], "at": row["at"]}
        for index, row in enumerate(plotted) if row["cross"]
    ]

    return {
        "width": CHART_WIDTH,
        "height": CHART_HEIGHT,
        "zeroY": zero_y,
        "macdPath": path("macd"),
        "signalPath": path("signal"),
        "bars": bars,
        "crossings": crossings,
        "ticks": [{"value": round(tick, 3), "y": y_at(tick)} for tick in _nice_ticks(low, high)],
        "xLabels": [
            {"x": x_at(index), "label": plotted[index]["at"][11:16]}
            for index in (0, len(plotted) // 2, len(plotted) - 1)
        ],
        "first": plotted[0]["at"],
        "last": plotted[-1]["at"],
        "count": len(plotted),
        "lastMacd": plotted[-1]["macd"],
        "lastSignal": plotted[-1]["signal"],
    }


def summarise(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    ready = [row for row in rows if row["macd"] is not None]
    latest = ready[-1] if ready else None
    crossings = [row for row in rows if row["cross"]]
    return {
        "candles": len(rows),
        "warmupCandles": Macd().warmup_candles,
        "readyFrom": ready[0]["at"] if ready else "",
        "latest": latest,
        "stance": ("Bullish" if latest and latest["histogram"] > 0
                   else "Bearish" if latest else "—"),
        "crossings": crossings[-8:][::-1],
        "crossingCount": len(crossings),
        "periods": {"fast": FAST_PERIOD, "slow": SLOW_PERIOD, "signal": SIGNAL_PERIOD},
    }


CSV_COLUMNS = ("at", "open", "high", "low", "close", "emaFast", "emaSlow",
               "macd", "signal", "histogram", "cross")
