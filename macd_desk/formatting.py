"""Rupee formatting with Indian digit grouping (lakh/crore), locale-free.

`locale` would need en_IN installed on the host, which is not a safe assumption
for a tool someone runs on their own laptop, so the grouping is done here.
"""

from __future__ import annotations

MINUS = "−"  # true minus sign, not a hyphen — it aligns with digits


def group_indian(digits: str) -> str:
    """12345678 -> 1,23,45,678 (last three digits, then pairs)."""
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


def number(value: float, places: int = 2) -> str:
    text = f"{abs(float(value)):.{places}f}"
    whole, _, frac = text.partition(".")
    grouped = group_indian(whole)
    return f"{grouped}.{frac}" if frac else grouped


def money(value: float) -> str:
    """Unsigned rupee amount — for charges, which are never negative."""
    return "₹" + number(value)


def signed(value: float) -> str:
    """Rupee amount that can be a loss — the sign leads, outside the symbol."""
    value = float(value)
    return (MINUS if value < 0 else "") + "₹" + number(value)


def pct(value: float) -> str:
    return number(value) + "%"


def points(value: float) -> str:
    return number(value) + " pts"


def qty(value: float) -> str:
    return group_indian(f"{int(round(float(value)))}")


def plain(value: float) -> str:
    """A number for an <input value>: 40 rather than 40.0, 0.03503 intact."""
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text or "0"


def sign_class(value: float) -> str:
    value = float(value)
    return "pos" if value > 0 else ("neg" if value < 0 else "")
