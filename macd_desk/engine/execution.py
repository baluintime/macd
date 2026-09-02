"""Turning a decision into a fill — simulated against live quotes, or real.

Two independent switches must both be on before a real order leaves the
building: `UPSTOX_LIVE_TRADING` in the environment, and the instrument's own
Execution Mode set to Live. Either one off means the order is paper-filled at
the live quote, exactly as the SRS describes paper mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

BUY, SELL = "BUY", "SELL"
PAPER, LIVE = "paper", "live"


@dataclass
class Fill:
    symbol: str
    trading_symbol: str
    side: str                # CE or PE
    transaction_type: str    # BUY or SELL
    quantity: float
    price: float
    mode: str                # paper or live
    at: datetime
    order_id: str = ""
    note: str = ""

    def public(self) -> dict:
        return {
            "symbol": self.symbol,
            "tradingSymbol": self.trading_symbol,
            "side": self.side,
            "transactionType": self.transaction_type,
            "quantity": self.quantity,
            "price": self.price,
            "mode": self.mode,
            "at": self.at.strftime("%H:%M:%S"),
            "orderId": self.order_id,
        }


class PaperExecutor:
    """Simulated fills against the real order book — no synthetic prices."""

    mode = PAPER

    def __init__(self, slippage_points: float = 0.0):
        self.slippage_points = float(slippage_points)

    def execute(self, *, symbol: str, trading_symbol: str, side: str,
                transaction_type: str, quantity: float, price: float,
                instrument_key: str = "", at: Optional[datetime] = None) -> Fill:
        # Slippage works against the trader on both legs.
        adjust = self.slippage_points if transaction_type == BUY else -self.slippage_points
        return Fill(symbol=symbol, trading_symbol=trading_symbol, side=side,
                    transaction_type=transaction_type, quantity=quantity,
                    price=round(float(price) + adjust, 2), mode=PAPER,
                    at=at or datetime.now(), note="simulated at live quote")


class LiveExecutor:
    """Routes the order to Upstox. Constructed only when both switches are on."""

    mode = LIVE

    def __init__(self, client, product: str = "I", order_type: str = "MARKET"):
        self.client = client
        self.product = product
        self.order_type = order_type

    def execute(self, *, symbol: str, trading_symbol: str, side: str,
                transaction_type: str, quantity: float, price: float,
                instrument_key: str = "", at: Optional[datetime] = None) -> Fill:
        response = self.client.place_order(
            instrument_key=instrument_key,
            quantity=int(quantity),
            transaction_type=transaction_type,
            product=self.product,
            order_type=self.order_type,
        )
        order_ids = response.get("order_ids") or []
        order_id = str(response.get("order_id") or (order_ids[0] if order_ids else ""))
        return Fill(symbol=symbol, trading_symbol=trading_symbol, side=side,
                    transaction_type=transaction_type, quantity=quantity,
                    price=float(price), mode=LIVE, at=at or datetime.now(),
                    order_id=order_id, note="market order")


def executor_for(instrument_mode: str, live_trading_enabled: bool, client,
                 slippage_points: float = 0.0):
    """Both switches on, and a broker to talk to — otherwise paper."""
    if instrument_mode == LIVE and live_trading_enabled and client is not None:
        return LiveExecutor(client)
    return PaperExecutor(slippage_points)
