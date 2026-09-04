"""The autotrade loop.

One background thread drives every enabled instrument. Per cycle, for each:

  1. warm up once from historical candles (the SRS's 3-day requirement)
  2. pull the underlying's closed candles for **both** timeframes — every symbol
     runs a 1-minute and a 5-minute engine simultaneously, each with its own
     position
  3. feed every new candle into the indicator, then reconcile the position to
     the resulting stance — one move per cycle, never one per historical
     crossover
  4. check the option premium against the position's target, in option points
  5. execute: BUY to enter, SELL to exit, paper or live per the guard
  6. write completed round trips into the blotter, where the cost model turns
     them into net profit after charges

Failures are per-instrument: one symbol erroring never stops the others.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Deque, Dict, List, Optional

from ..broker.tokens import IST
from .execution import BUY, SELL, executor_for
from .selection import ContractSelector, SelectionError
from ..state import TARGET_FIELDS
from .strategy import ENTER, EXIT, Decision, InstrumentStrategy

# Every symbol runs both engines at once; they hold positions independently.
TIMEFRAMES = ("1m", "5m")

SNAPSHOT_SECONDS = 60.0        # how often the page's live premiums are refreshed
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
SQUARE_OFF = time(15, 20)      # flatten before the close, not at it
DEFAULT_POLL_SECONDS = 15.0
MAX_EVENTS = 300


def now_ist() -> datetime:
    return datetime.now(IST)


def in_session(moment: Optional[datetime] = None) -> bool:
    moment = moment or now_ist()
    if moment.weekday() >= 5:
        return False
    return MARKET_OPEN <= moment.time() <= MARKET_CLOSE


class EngineRunner:
    """Supervises one strategy per configured instrument."""

    def __init__(self, *, settings, client, selector, state_io,
                 poll_seconds: float = DEFAULT_POLL_SECONDS,
                 clock: Callable[[], datetime] = now_ist,
                 require_session: bool = True):
        self.settings = settings
        self.client = client
        self.selector = selector
        self.state_io = state_io          # (load, save) pair for the desk state
        self.poll_seconds = poll_seconds
        self.clock = clock
        self.require_session = require_session

        self.strategies: Dict[str, InstrumentStrategy] = {}
        # Last live contract seen per symbol — what the desk page renders.
        self.snapshots: Dict[str, dict] = {}
        self._snapshot_at: Dict[str, datetime] = {}
        self.events: Deque[dict] = deque(maxlen=MAX_EVENTS)
        self.errors: Dict[str, str] = {}
        self._logged_errors: Dict[str, str] = {}
        self._seen_candle: Dict[str, str] = {}

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.started_at: Optional[datetime] = None
        self.last_cycle_at: Optional[datetime] = None
        self.cycles = 0

    # ------------------------------------------------------------ lifecycle

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self.started_at = self.clock()
        self._thread = threading.Thread(target=self._run, name="macd-engine", daemon=True)
        self._thread.start()
        self.log("engine", "Engine started")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        self.log("engine", "Engine stopped")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_cycle()
            except Exception as error:                      # never kill the loop
                self.log("engine", f"Cycle failed: {error}", level="error")
            self._stop.wait(self.poll_seconds)

    # ---------------------------------------------------------------- cycle

    def run_cycle(self, moment: Optional[datetime] = None) -> None:
        moment = moment or self.clock()
        self.cycles += 1
        self.last_cycle_at = moment

        if self.require_session and not in_session(moment):
            return

        desk = self.state_io.load()
        configured = {c["symbol"] for c in desk.get("instruments", [])}
        for key in [k for k in self.strategies if k.split(":")[0] not in configured]:
            # Removed from the desk: stop running it. An open position is left
            # to the square-off rather than closed behind the operator's back.
            self.strategies.pop(key, None)
            self.errors.pop(key, None)

        for config in desk.get("instruments", []):
            for timeframe in TIMEFRAMES:
                self._step_safely(config, timeframe, moment)

        if self.require_session and moment.time() >= SQUARE_OFF:
            self.square_off(moment)

    def _step_safely(self, config: dict, timeframe: str, moment: datetime) -> None:
        key = f"{config['symbol']}:{timeframe}"
        try:
            self._step_instrument(key, config, timeframe, moment)
            self.errors.pop(key, None)
            self._logged_errors.pop(key, None)
        except Exception as error:
            message = str(error)
            self.errors[key] = message
            # The same failure every 15 seconds buries everything else.
            if self._logged_errors.get(key) != message:
                self._logged_errors[key] = message
                self.log(f"{config['symbol']} {timeframe}", message, level="error")

    def _strategy_for(self, key: str, config: dict, timeframe: str) -> InstrumentStrategy:
        # Each engine has its own target: a 1-minute move is not a 5-minute one.
        target = config.get(TARGET_FIELDS[timeframe], 0)
        strategy = self.strategies.get(key)
        if strategy is None:
            strategy = InstrumentStrategy(
                symbol=config["symbol"], timeframe=timeframe, target_points=target,
                lots=config["lots"], lot_size=config["lotSize"])
            self.strategies[key] = strategy
        # Live edits on the page take effect on the next cycle — including on a
        # position that is already open.
        strategy.sync_target(target)
        strategy.lots = config["lots"]
        strategy.lot_size = config["lotSize"]
        return strategy

    def _step_instrument(self, key: str, config: dict, timeframe: str,
                         moment: datetime) -> None:
        strategy = self._strategy_for(key, config, timeframe)
        underlying = self.selector.underlying_key(config["symbol"])
        if not underlying:
            raise RuntimeError(
                f"No instrument key for {config['symbol']} — the symbol is not in the NSE "
                f"equity master. Check the trading symbol (it changes after a demerger or "
                f"rename) or remove it from the desk.")

        first_pass = not strategy.warmed_up
        if first_pass:
            history = self.client.historical_candles(underlying, timeframe, days=3)
            strategy.warmup([float(candle[4]) for candle in history])
            self.log(f"{config['symbol']} {timeframe}",
                     f"Warmed up on {len(history)} historical {timeframe} candles"
                     + ("" if strategy.warmed_up else " — not enough history yet"))

        # Closed candles only: the last row of an intraday response is still forming.
        candles = self.client.intraday_candles(underlying, timeframe)
        fresh = 0
        for candle in candles[:-1] if len(candles) > 1 else []:
            stamp = str(candle[0])
            if self._seen_candle.get(key, "") >= stamp:
                continue
            self._seen_candle[key] = stamp
            # Feeding advances the indicator; it never trades. A backlog of
            # candles must not become a backlog of trades.
            strategy.feed(float(candle[4]), moment)
            fresh += 1

        if first_pass:
            # The backlog's crossovers already happened; do not trade them.
            strategy.prime()

        # At most one entry per crossover.
        for decision in strategy.reconcile(moment):
            self._act(strategy, config, decision, moment)

        position = strategy.position
        if position is not None:
            # The target lives on the premium; the spot is carried for context.
            prices = self.client.ltp([position.instrument_key, underlying])
            spot = prices.get(underlying)
            if spot is not None:
                strategy.on_underlying_price(spot, moment)

            premium = prices.get(position.instrument_key)
            if premium is not None:
                for decision in strategy.on_option_price(premium, moment):
                    self._act(strategy, config, decision, moment)
        else:
            self.refresh_snapshot(config, moment)

    # ------------------------------------------------------------ execution

    def _act(self, strategy: InstrumentStrategy, config: dict,
             decision: Decision, moment: datetime) -> None:
        executor = executor_for(config["mode"], self.settings.live_trading_enabled,
                                self.client)
        if decision.kind == ENTER:
            self._enter(strategy, config, decision, executor, moment)
        else:
            self._exit(strategy, config, decision, executor, moment)

    def _enter(self, strategy, config, decision, executor, moment) -> None:
        # In the money, delta 0.60-0.70, priced off the live chain.
        contract = self.selector.select(config["symbol"], decision.side, now=moment)
        self.snapshots[config["symbol"]] = contract.public()
        scope = f"{config['symbol']} {strategy.timeframe}"

        lot_size = contract.lot_size or config["lotSize"]
        quantity = config["lots"] * lot_size
        fill = executor.execute(symbol=config["symbol"], trading_symbol=contract.trading_symbol,
                                side=decision.side, transaction_type=BUY, quantity=quantity,
                                price=contract.premium, instrument_key=contract.instrument_key,
                                at=moment)

        strategy.lot_size = lot_size
        strategy.open_position(decision.side, fill.price, at=moment, contract=contract,
                               spot=contract.spot)
        self.log(scope,
                 f"BUY {contract.label} ×{quantity:.0f} @ {fill.price:.2f}"
                 f" — {config['symbol']} {contract.spot:.2f}, {decision.detail or decision.reason}"
                 f" ({fill.mode})", fill=fill.public())

    def _exit(self, strategy, config, decision, executor, moment) -> None:
        position = strategy.position
        if position is None:
            return
        price = self.client.ltp([position.instrument_key]).get(position.instrument_key,
                                                               position.last_price)
        fill = executor.execute(symbol=config["symbol"], trading_symbol=position.trading_symbol,
                                side=position.side, transaction_type=SELL,
                                quantity=position.quantity, price=price,
                                instrument_key=position.instrument_key, at=moment)
        closed = strategy.close_position(fill.price)
        self._record_trade(config, closed, fill, decision.reason, strategy.timeframe)
        self.log(f"{config['symbol']} {strategy.timeframe}",
                 f"SELL {position.label} ×{position.quantity:.0f} @ {fill.price:.2f}"
                 f" — {decision.reason.lower()}, {decision.detail or ''} ({fill.mode})".replace(
                     " ,", ","), fill=fill.public())

    def refresh_snapshot(self, config: dict, moment: Optional[datetime] = None,
                         force: bool = False) -> Optional[dict]:
        """Keep one live contract per symbol on hand for the desk page.

        Rate-limited: the chain is a real request, not something to hammer on
        every cycle when nothing is open.
        """
        moment = moment or self.clock()
        symbol = config["symbol"]
        last = self._snapshot_at.get(symbol)
        if not force and last and (moment - last).total_seconds() < SNAPSHOT_SECONDS:
            return self.snapshots.get(symbol)

        contract = self.selector.select(symbol, config.get("side", "CE"), now=moment)
        self._snapshot_at[symbol] = moment
        self.snapshots[symbol] = contract.public()
        return self.snapshots[symbol]

    def refresh_all_snapshots(self) -> Dict[str, str]:
        """One-shot live refresh for the page, without starting the loop."""
        problems: Dict[str, str] = {}
        desk = self.state_io.load()
        for config in desk.get("instruments", []):
            try:
                self.refresh_snapshot(config, force=True)
            except Exception as error:
                problems[config["symbol"]] = str(error)
                self.snapshots.pop(config["symbol"], None)
        return problems

    def _record_trade(self, config: dict, position, fill, reason: str,
                      timeframe: str = "") -> None:
        """Append the completed round trip to the blotter, under the state lock."""
        with self._lock:
            desk = self.state_io.load()
            desk.setdefault("trades", []).append({
                "symbol": config["symbol"],
                "side": position.side,
                "reason": reason if reason in ("Target", "Reversal", "EOD close") else "Target",
                "entryPrice": position.entry_price,
                "exitPrice": fill.price,
                "lots": position.lots,
                "lotSize": position.lot_size,
                "timeframe": timeframe or "1m",
                # Paper and live fills are kept in separate books.
                "mode": fill.mode,
                "contract": position.label,
                "strike": position.strike,
                "entryAt": (position.entry_time.strftime("%Y-%m-%d %H:%M:%S")
                            if position.entry_time else ""),
                "exitAt": fill.at.strftime("%Y-%m-%d %H:%M:%S"),
            })
            self.state_io.save(desk)

    def square_off(self, moment: Optional[datetime] = None) -> None:
        moment = moment or self.clock()
        desk = self.state_io.load()
        by_key = {f"{c['symbol']}:{timeframe}": c
                  for c in desk.get("instruments", []) for timeframe in TIMEFRAMES}
        for key, strategy in self.strategies.items():
            config = by_key.get(key)
            if config is None or strategy.position is None:
                continue
            executor = executor_for(config["mode"], self.settings.live_trading_enabled, self.client)
            for decision in strategy.close_out(moment):
                try:
                    self._exit(strategy, config, decision, executor, moment)
                except Exception as error:
                    self.log(config["symbol"], f"Square-off failed: {error}", level="error")

    # -------------------------------------------------------------- reporting

    def log(self, scope: str, message: str, level: str = "info", **extra) -> None:
        self.events.appendleft({
            "at": self.clock().strftime("%H:%M:%S"),
            "scope": scope,
            "message": message,
            "level": level,
            **extra,
        })

    def status(self) -> dict:
        moment = self.clock()
        positions = [s.status() for s in self.strategies.values() if s.position]
        return {
            "running": self.running,
            "inSession": in_session(moment),
            "liveTradingEnabled": self.settings.live_trading_enabled,
            "startedAt": self.started_at.strftime("%H:%M:%S") if self.started_at else "",
            "lastCycleAt": self.last_cycle_at.strftime("%H:%M:%S") if self.last_cycle_at else "",
            "cycles": self.cycles,
            "pollSeconds": self.poll_seconds,
            "instruments": [s.status() for s in self.strategies.values()],
            "openPositions": positions,
            "snapshots": dict(self.snapshots),
            "errors": dict(self.errors),
            "events": list(self.events)[:40],
        }
