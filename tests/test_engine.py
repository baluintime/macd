"""The autotrade loop, driven end to end against a fake broker."""

import unittest
from datetime import date, datetime, timedelta
from typing import List

from macd_desk import charges, state as state_module
from macd_desk.config import UpstoxSettings
from macd_desk.engine import runner as runner_module
from macd_desk.engine.execution import LiveExecutor, PaperExecutor, executor_for
from macd_desk.engine.indicators import Macd, crossover, macd_series
import math

from macd_desk.engine.runner import EngineRunner, in_session
from macd_desk.engine.selection import ContractSelector, SelectedContract, SelectionError
from macd_desk.engine.strategy import InstrumentStrategy

MARKET_MOMENT = datetime(2026, 9, 2, 10, 30)      # a Wednesday, mid-session


def ramp(start, step, count):
    return [start + step * i for i in range(count)]


SPOT = 25280.0


def leg(key, premium, delta):
    return {"instrument_key": key, "trading_symbol": key,
            "option_greeks": {"delta": delta}, "market_data": {"ltp": premium}}


def chain_row(strike, call_premium, call_delta, put_premium, put_delta):
    return {"strike_price": strike, "expiry": "2026-09-10", "underlying_spot_price": SPOT,
            "lot_size": 75,
            "call_options": leg(f"NSE_FO|CE{strike}", call_premium, call_delta),
            "put_options": leg(f"NSE_FO|PE{strike}", put_premium, put_delta)}


def default_chain():
    """Deltas spanning the band, so selection has a real choice to make."""
    return [
        chain_row(25000, 330, 0.78, 60, -0.22),
        chain_row(25100, 255, 0.68, 85, -0.32),   # the CE the rules should pick
        chain_row(25300, 140, 0.49, 160, -0.51),
        chain_row(25500, 70, 0.30, 290, -0.70),   # the PE the rules should pick
    ]


class FakeClient:
    """Serves scripted candles, a live chain and quotes; records live orders."""

    def __init__(self, history, intraday, quotes, chain=None):
        self.history = history
        self.intraday = list(intraday)
        self.quotes = dict(quotes)
        self.chain = chain if chain is not None else default_chain()
        self.visible = len(self.intraday)
        self.orders: List[dict] = []

    def instruments(self):
        return []

    def option_contracts(self, underlying_key, expiry=None):
        return [{"expiry": "2026-09-10"}, {"expiry": "2026-09-24"}]

    def option_chain(self, underlying_key, expiry):
        return self.chain

    def historical_candles(self, key, timeframe, days=3):
        return [[f"2026-08-{28 + i // 100:02d}T09:{i % 60:02d}:00+05:30", 0, 0, 0, close, 0]
                for i, close in enumerate(self.history)]

    def intraday_candles(self, key, timeframe):
        # `visible` lets a test hold candles back, then release them, so a
        # crossover can arrive live rather than inside the startup backlog.
        return self.intraday[:self.visible]

    def ltp(self, keys):
        return {key: self.quotes[key] for key in keys if key in self.quotes}

    def place_order(self, **kwargs):
        self.orders.append(kwargs)
        return {"order_id": f"ORD{len(self.orders)}"}


class StateIO:
    def __init__(self, desk):
        self.desk = desk

    def load(self):
        return self.desk

    def save(self, desk):
        self.desk = desk


def one_instrument(mode="paper", target=20.0, symbol="NIFTY"):
    """Configuration only — every symbol runs both timeframes."""
    return {"symbol": symbol, "kind": "Index", "side": "CE", "mode": mode,
            "lots": 1.0, "targetPoints": target, "lotSize": 75.0}


def candles(closes, start_minute=0):
    return [[f"2026-09-02T10:{start_minute + i:02d}:00+05:30", 0, 0, 0, close, 0]
            for i, close in enumerate(closes)]


def build_runner(client, desk, **kwargs):
    settings = UpstoxSettings(api_key="k", api_secret="s", redirect_uri="r",
                              live_trading_enabled=kwargs.pop("live", False))
    return EngineRunner(settings=settings, client=client,
                        selector=ContractSelector(client), state_io=StateIO(desk),
                        require_session=False, clock=lambda: MARKET_MOMENT, **kwargs)


class IndicatorTests(unittest.TestCase):
    def test_macd_needs_thirty_four_candles(self):
        indicator = Macd()
        self.assertEqual(indicator.warmup_candles, 34)
        points = macd_series(ramp(100, 0.5, 40))
        self.assertIsNone(points[32])
        self.assertIsNotNone(points[33])

    def test_ema_seeds_from_the_simple_average(self):
        from macd_desk.engine.indicators import Ema
        ema = Ema(3)
        self.assertIsNone(ema.update(10))
        self.assertIsNone(ema.update(20))
        self.assertEqual(ema.update(30), 20.0)          # SMA of the first three

    def test_crossover_detects_both_directions(self):
        from macd_desk.engine.indicators import MacdPoint
        below, above = MacdPoint(-1, 0, -1), MacdPoint(1, 0, 1)
        self.assertEqual(crossover(below, above), "BULLISH")
        self.assertEqual(crossover(above, below), "BEARISH")
        self.assertIsNone(crossover(above, above))
        self.assertIsNone(crossover(None, above))


class StrategyTests(unittest.TestCase):
    """Every decision comes from the underlying's MACD, never the premium."""

    def strategy(self, target=999):
        return InstrumentStrategy("NIFTY", "5m", target_points=target, lots=1, lot_size=75)

    def drive(self, strategy, series):
        """Feed candles the way the runner does, reconciling once per candle."""
        seen = []
        for close in series:
            strategy.feed(close)
            for decision in strategy.reconcile():
                seen.append((decision.kind, decision.side))
                if decision.kind == "ENTER":
                    strategy.open_position(decision.side, 100, spot=close)
                else:
                    strategy.close_position(100)
        return seen

    def test_the_stance_follows_the_histogram(self):
        # A ramp that turns up: the fast EMA pulls above the slow one.
        rising = self.strategy()
        rising.warmup(ramp(120, -0.8, 40) + ramp(88, 1.6, 30))
        self.assertEqual(rising.stance, "CE")
        self.assertGreater(rising.previous.histogram, 0)

        falling = self.strategy()
        falling.warmup(ramp(88, 1.6, 40) + ramp(152, -1.6, 30))
        self.assertEqual(falling.stance, "PE")
        self.assertLess(falling.previous.histogram, 0)

    def test_a_flat_histogram_expresses_no_opinion(self):
        # A perfectly linear series converges MACD onto its signal; with no
        # momentum change the engine holds whatever it has rather than churning.
        flat = self.strategy()
        flat.warmup(ramp(72, 1.5, 60))
        self.assertEqual(flat.previous.histogram, 0)
        self.assertIsNone(flat.stance)
        self.assertEqual(flat.reconcile(), [])

    def test_it_buys_a_call_on_a_bullish_cross_and_reverses_on_a_bearish_one(self):
        strategy = self.strategy()
        seen = self.drive(strategy, ramp(120, -0.8, 60) + ramp(72, 1.5, 50) + ramp(147, -1.5, 50))
        self.assertEqual(seen[0], ("ENTER", "CE"))       # the first live cross
        self.assertIn(("EXIT", "CE"), seen)
        self.assertIn(("ENTER", "PE"), seen)
        self.assertEqual(strategy.stance, "PE")
        self.assertEqual(strategy.position.side, "PE")

    def test_one_entry_per_crossover_and_no_re_entry_after_a_target_exit(self):
        strategy = self.strategy(target=20)
        strategy.warmup(ramp(120, -0.8, 40))
        for close in ramp(88, 1.6, 30):                  # rally → one bullish cross
            strategy.feed(close)
            for decision in strategy.reconcile():
                self.assertEqual(decision.kind, "ENTER")
                strategy.open_position(decision.side, 100, spot=25000)

        self.assertIsNotNone(strategy.position)
        self.assertEqual(strategy.reconcile(), [])       # the cross is spent

        # A target exit leaves it flat, and it stays flat until the next cross.
        strategy.on_underlying_price(25020)
        strategy.close_position(120)
        self.assertEqual(strategy.reconcile(), [])
        self.assertIsNone(strategy.position)

    def test_a_backlog_of_crossovers_is_primed_away_not_traded(self):
        strategy = self.strategy()
        crossings = sum(1 for close in
                        [700 + 6 * math.sin(i / 9) for i in range(300)] if strategy.feed(close))
        self.assertGreater(crossings, 4)
        strategy.prime()
        self.assertEqual(strategy.reconcile(), [])
        self.assertIsNone(strategy.position)

    def test_a_position_already_matching_the_stance_is_left_alone(self):
        strategy = self.strategy()
        strategy.warmup(ramp(72, 1.5, 60))
        strategy.open_position("CE", 100, spot=25000)
        self.assertEqual(strategy.reconcile(), [])

    def test_an_edited_target_reaches_the_open_position(self):
        strategy = self.strategy(target=0)
        strategy.warmup(ramp(120, -0.8, 40) + ramp(88, 1.6, 30))
        strategy.open_position("CE", 210, spot=26150)
        self.assertEqual(strategy.position.target_points, 0)

        strategy.sync_target(5)                          # the card is edited
        self.assertEqual(strategy.position.target_points, 5)
        self.assertEqual(strategy.position.target_spot, 26155)

    def test_the_target_is_measured_on_the_underlying_not_the_premium(self):
        strategy = self.strategy(target=20)
        strategy.warmup(ramp(72, 1.5, 60))
        strategy.open_position("CE", 100, spot=25000)

        # The premium moving is not a target hit; the underlying moving is.
        self.assertEqual(strategy.on_underlying_price(25019.9), [])
        exits = strategy.on_underlying_price(25020.0)
        self.assertEqual((exits[0].kind, exits[0].reason), ("EXIT", "Target"))

    def test_a_put_targets_a_fall_in_the_underlying(self):
        strategy = self.strategy(target=20)
        strategy.warmup(ramp(160, -1.5, 60))
        strategy.open_position("PE", 100, spot=25000)
        self.assertEqual(strategy.position.target_spot, 24980)
        self.assertEqual(strategy.on_underlying_price(25020), [])       # wrong way
        self.assertTrue(strategy.on_underlying_price(24980))


class ExecutionGuardTests(unittest.TestCase):
    def test_a_real_order_needs_both_switches(self):
        client = FakeClient([], [], {})
        self.assertIsInstance(executor_for("live", False, client), PaperExecutor)
        self.assertIsInstance(executor_for("paper", True, client), PaperExecutor)
        self.assertIsInstance(executor_for("live", True, client), LiveExecutor)

    def test_paper_slippage_works_against_the_trader(self):
        executor = PaperExecutor(slippage_points=0.25)
        buy = executor.execute(symbol="NIFTY", trading_symbol="x", side="CE",
                               transaction_type="BUY", quantity=75, price=100)
        sell = executor.execute(symbol="NIFTY", trading_symbol="x", side="CE",
                                transaction_type="SELL", quantity=75, price=100)
        self.assertEqual((buy.price, sell.price), (100.25, 99.75))


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient([], [], {})
        self.selector = ContractSelector(self.client)

    def test_calls_are_picked_in_the_money_inside_the_delta_band(self):
        contract = self.selector.select("NIFTY", "CE", today=date(2026, 9, 2))
        self.assertEqual(contract.strike, 25100)          # below spot → in the money
        self.assertAlmostEqual(contract.delta, 0.68, places=2)
        self.assertLess(contract.strike, SPOT)
        self.assertTrue(contract.in_the_money)

    def test_puts_are_picked_in_the_money_on_the_other_side_of_spot(self):
        contract = self.selector.select("NIFTY", "PE", today=date(2026, 9, 2))
        self.assertEqual(contract.strike, 25500)          # above spot → in the money
        self.assertAlmostEqual(abs(contract.delta), 0.70, places=2)
        self.assertGreater(contract.strike, SPOT)

    def test_the_at_the_money_strike_is_rejected(self):
        # 25300 sits at delta 0.49 — the naive ATM pick, outside the band.
        for side in ("CE", "PE"):
            contract = self.selector.select("NIFTY", side, today=date(2026, 9, 2))
            self.assertNotEqual(contract.strike, 25300)

    def test_selection_fails_rather_than_substituting_a_strike(self):
        thin = ContractSelector(self.client, delta_min=0.95, delta_max=0.99)
        with self.assertRaises(SelectionError) as caught:
            thin.select("NIFTY", "CE", today=date(2026, 9, 2))
        self.assertIn("delta", str(caught.exception))

    def test_a_chain_without_greeks_is_refused(self):
        bare = FakeClient([], [], {}, chain=[{"strike_price": 25100, "expiry": "2026-09-10",
                                              "underlying_spot_price": SPOT,
                                              "call_options": {"instrument_key": "x"}}])
        with self.assertRaises(SelectionError):
            ContractSelector(bare).select("NIFTY", "CE", today=date(2026, 9, 2))

    def test_the_nearest_expiry_wins(self):
        self.assertEqual(self.selector.nearest_expiry("NSE_INDEX|Nifty 50", date(2026, 9, 2)),
                         date(2026, 9, 10))


class RunnerTests(unittest.TestCase):
    """The underlying falls (bearish cross), then rallies (bullish cross)."""

    CE_KEY = "NSE_FO|CE25100"
    CE_PREMIUM = 255.0
    BACKLOG = 20            # candles already closed when the engine starts

    def setUp(self):
        self.history = ramp(72, 1.5, 60)
        self.closes = ramp(162, -1.6, 40) + ramp(98, 1.8, 45)

    def _client(self):
        quotes = {"NSE_INDEX|Nifty 50": SPOT, self.CE_KEY: self.CE_PREMIUM,
                  "NSE_FO|PE25500": 290.0}
        # The last row is still forming and must be ignored.
        client = FakeClient(self.history, candles(self.closes) + [["forming", 0, 0, 0, 999, 0]],
                            quotes)
        client.visible = self.BACKLOG
        return client

    def _desk(self, *instruments):
        return {"instruments": list(instruments), "trades": [],
                "rates": dict(charges.DEFAULT_RATES)}

    def _run_to_the_bullish_cross(self, engine, client):
        """One cycle over the backlog, then release the rest."""
        engine.run_cycle(MARKET_MOMENT)
        client.visible = len(client.intraday)
        engine.run_cycle(MARKET_MOMENT)

    def test_the_startup_backlog_opens_nothing(self):
        client = self._client()
        engine = build_runner(client, self._desk(one_instrument()))
        engine.run_cycle(MARKET_MOMENT)

        self.assertEqual(engine.state_io.desk["trades"], [])
        for strategy in engine.strategies.values():
            self.assertIsNone(strategy.position)
        self.assertEqual([e for e in engine.events if "BUY" in e["message"]], [])

    def test_a_live_crossover_buys_the_delta_selected_call(self):
        client = self._client()
        engine = build_runner(client, self._desk(one_instrument()))
        self._run_to_the_bullish_cross(engine, client)

        position = engine.strategies["NIFTY:5m"].position
        self.assertIsNotNone(position)
        self.assertEqual(position.side, "CE")
        self.assertEqual(position.instrument_key, self.CE_KEY)
        self.assertEqual(position.entry_price, self.CE_PREMIUM)
        self.assertEqual(client.orders, [])                # paper places nothing

    def test_only_one_entry_comes_from_one_crossover(self):
        client = self._client()
        engine = build_runner(client, self._desk(one_instrument()))
        self._run_to_the_bullish_cross(engine, client)
        for _ in range(4):
            engine.run_cycle(MARKET_MOMENT)

        entries = [e for e in engine.events if "BUY" in e["message"]]
        self.assertEqual(len(entries), 2)                  # one per timeframe, no more

    def test_every_symbol_runs_both_timeframes(self):
        client = self._client()
        engine = build_runner(client, self._desk(one_instrument()))
        engine.run_cycle(MARKET_MOMENT)
        self.assertEqual(sorted(engine.strategies), ["NIFTY:1m", "NIFTY:5m"])

    def test_target_exit_writes_a_costed_trade_and_does_not_re_enter(self):
        client = self._client()
        engine = build_runner(client, self._desk(one_instrument(target=20)))
        self._run_to_the_bullish_cross(engine, client)

        # The target is on the underlying: the index moves 20 points.
        client.quotes["NSE_INDEX|Nifty 50"] = SPOT + 20
        client.quotes[self.CE_KEY] = self.CE_PREMIUM + 14
        engine.run_cycle(MARKET_MOMENT)

        trades = engine.state_io.desk["trades"]
        self.assertTrue(trades)
        self.assertEqual(trades[0]["reason"], "Target")
        self.assertEqual((trades[0]["entryPrice"], trades[0]["exitPrice"]),
                         (self.CE_PREMIUM, self.CE_PREMIUM + 14))
        totals = charges.summarize(trades)["totals"]
        self.assertAlmostEqual(totals["grossPnl"], 14 * 75 * len(trades), places=2)

        # Flat, and it stays flat: the crossover that opened it is spent.
        opened = len(trades)
        engine.run_cycle(MARKET_MOMENT)
        self.assertEqual(len(engine.state_io.desk["trades"]), opened)
        self.assertIsNone(engine.strategies["NIFTY:5m"].position)

    def test_a_target_exit_does_not_crash_the_cycle(self):
        """The reported crash: the premium was read off a position just closed."""
        client = self._client()
        engine = build_runner(client, self._desk(one_instrument(target=1)))
        self._run_to_the_bullish_cross(engine, client)
        client.quotes["NSE_INDEX|Nifty 50"] = SPOT + 50
        engine.run_cycle(MARKET_MOMENT)
        self.assertEqual(engine.errors, {})

    def test_live_mode_with_the_flag_on_places_both_legs(self):
        client = self._client()
        engine = build_runner(client, self._desk(one_instrument(mode="live", target=20)),
                              live=True)
        self._run_to_the_bullish_cross(engine, client)
        client.quotes["NSE_INDEX|Nifty 50"] = SPOT + 20
        engine.run_cycle(MARKET_MOMENT)

        kinds = [order["transaction_type"] for order in client.orders]
        self.assertEqual(kinds[:2], ["BUY", "BUY"])        # one per timeframe
        self.assertIn("SELL", kinds)
        self.assertEqual(client.orders[0]["instrument_key"], self.CE_KEY)

    def test_a_forming_candle_is_never_acted_on(self):
        client = self._client()
        client.visible = len(client.intraday)
        engine = build_runner(client, self._desk(one_instrument()))
        engine.run_cycle(MARKET_MOMENT)
        self.assertEqual(engine._seen_candle["NIFTY:5m"], candles(self.closes)[-1][0])

    def test_one_bad_symbol_does_not_stop_the_others(self):
        client = self._client()
        engine = build_runner(client, self._desk(one_instrument(symbol="BROKEN"),
                                                 one_instrument()))
        self._run_to_the_bullish_cross(engine, client)

        self.assertIn("BROKEN:5m", engine.errors)
        self.assertIsNotNone(engine.strategies["NIFTY:5m"].position)

    def test_square_off_flattens_open_positions(self):
        client = self._client()
        engine = build_runner(client, self._desk(one_instrument(target=9999)))
        self._run_to_the_bullish_cross(engine, client)
        self.assertIsNotNone(engine.strategies["NIFTY:5m"].position)

        engine.square_off(MARKET_MOMENT)
        self.assertIsNone(engine.strategies["NIFTY:5m"].position)
        self.assertEqual(engine.state_io.desk["trades"][0]["reason"], "EOD close")


class SessionTests(unittest.TestCase):
    def test_session_window_matches_the_exchange_day(self):
        self.assertFalse(in_session(datetime(2026, 9, 2, 9, 0)))     # pre-open
        self.assertTrue(in_session(datetime(2026, 9, 2, 9, 15)))
        self.assertTrue(in_session(datetime(2026, 9, 2, 15, 30)))
        self.assertFalse(in_session(datetime(2026, 9, 2, 15, 31)))
        self.assertFalse(in_session(datetime(2026, 9, 5, 11, 0)))    # Saturday


if __name__ == "__main__":
    unittest.main()


class PositionIdentityTests(unittest.TestCase):
    """What is held has to be legible: which strike, and call or put."""

    def _open(self, side="CE"):
        client = FakeClient(ramp(120, -0.8, 60),
                            candles(ramp(72, 1.5, 60)) + [["forming", 0, 0, 0, 999, 0]],
                            {"NSE_INDEX|Nifty 50": SPOT,
                             "NSE_FO|CE25100": 255.0, "NSE_FO|PE25500": 290.0})
        contract = ContractSelector(client).select("NIFTY", side, today=date(2026, 9, 10))
        strategy = InstrumentStrategy("NIFTY", "5m", target_points=20, lots=1, lot_size=75)
        return strategy.open_position(side, contract.premium, contract=contract), contract

    def test_the_position_carries_the_strike_and_the_side(self):
        position, contract = self._open("CE")
        public = position.public()
        self.assertEqual(public["strike"], 25100)
        self.assertEqual(public["side"], "CE")
        self.assertEqual(public["optionType"], "Call")
        self.assertEqual(public["expiry"], "2026-09-10")
        self.assertAlmostEqual(public["delta"], 0.68, places=2)

    def test_a_put_is_labelled_a_put(self):
        position, _ = self._open("PE")
        self.assertEqual(position.public()["optionType"], "Put")
        self.assertEqual(position.public()["strike"], 25500)

    def test_a_contract_without_a_trading_symbol_still_gets_a_label(self):
        # The live chain does not always carry one — the blank column bug.
        position, contract = self._open("PE")
        self.assertEqual(contract.trading_symbol, "NSE_FO|PE25500")
        bare = SelectedContract("NSE_FO|PE1", "", "NIFTY", "PE", 24800,
                                date(2026, 9, 10), 75, 152.1, -0.67, 24750, True)
        self.assertEqual(bare.label, "NIFTY 24800 PE")
        strategy = InstrumentStrategy("NIFTY", "1m", target_points=20, lots=1, lot_size=75)
        self.assertEqual(strategy.open_position("PE", 152.1, contract=bare).label,
                         "NIFTY 24800 PE")

    def test_the_engine_log_names_the_contract_it_bought(self):
        client = FakeClient(ramp(72, 1.5, 60),
                            candles(ramp(162, -1.6, 40) + ramp(98, 1.8, 45))
                            + [["forming", 0, 0, 0, 999, 0]],
                            {"NSE_INDEX|Nifty 50": SPOT, "NSE_FO|CE25100": 255.0})
        client.visible = 20
        engine = build_runner(client, {"instruments": [one_instrument()], "trades": [],
                                       "rates": dict(charges.DEFAULT_RATES)})
        engine.run_cycle(MARKET_MOMENT)
        client.visible = len(client.intraday)
        engine.run_cycle(MARKET_MOMENT)

        entry = next(event for event in engine.events if "BUY" in event["message"])
        self.assertIn("25100", entry["message"])
        self.assertIn("CE", entry["message"])


class ErrorLogTests(unittest.TestCase):
    def test_the_same_failure_is_logged_once_not_every_cycle(self):
        client = FakeClient([], [], {})
        engine = build_runner(client, {"instruments": [one_instrument(symbol="NOSUCHSYMBOL")],
                                       "trades": [], "rates": dict(charges.DEFAULT_RATES)})
        for _ in range(5):
            engine.run_cycle(MARKET_MOMENT)

        logged = [event for event in engine.events if event["level"] == "error"]
        self.assertEqual(len(logged), 2)                 # once per timeframe, not per cycle
        self.assertIn("NOSUCHSYMBOL", engine.errors["NOSUCHSYMBOL:5m"])
        self.assertIn("not in the NSE equity master", logged[0]["message"])
