"""The autotrade loop, driven end to end against a fake broker."""

import unittest
from datetime import date, datetime, timedelta
from typing import List

from macd_desk import charges, state as state_module
from macd_desk.config import UpstoxSettings
from macd_desk.engine import runner as runner_module
from macd_desk.engine.execution import LiveExecutor, PaperExecutor, executor_for
from macd_desk.engine.indicators import Macd, crossover, macd_series
from macd_desk.engine.runner import EngineRunner, in_session
from macd_desk.engine.selection import ContractSelector, SelectionError
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
        return self.intraday

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


def one_instrument(timeframe="5m", mode="paper", target=20.0):
    return {
        "symbol": "NIFTY", "kind": "Index", "side": "CE", "mode": mode,
        "timeframe": timeframe, "lots": 1.0, "targetPoints": target, "lotSize": 75.0,
    }


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
    def test_bullish_crossover_buys_a_call_and_bearish_reverses_into_a_put(self):
        strategy = InstrumentStrategy("NIFTY", "5m", target_points=999, lots=1, lot_size=75)
        series = ramp(120, -0.8, 60) + ramp(72, 1.5, 50) + ramp(147, -1.5, 50)
        seen = []
        for close in series:
            for decision in strategy.on_closed_candle(close):
                seen.append((decision.kind, decision.side, decision.reason))
                if decision.kind == "ENTER":
                    strategy.open_position(decision.side, 100)
                else:
                    strategy.close_position(100)

        self.assertEqual(seen[0], ("ENTER", "CE", "Reversal"))
        self.assertEqual(seen[1], ("EXIT", "CE", "Reversal"))
        self.assertEqual(seen[2], ("ENTER", "PE", "Reversal"))

    def test_target_exit_fires_at_the_configured_points(self):
        strategy = InstrumentStrategy("NIFTY", "1m", target_points=20, lots=1, lot_size=75)
        strategy.open_position("CE", 100)
        self.assertEqual(strategy.on_price(119.95), [])
        exits = strategy.on_price(120.0)
        self.assertEqual((exits[0].kind, exits[0].reason), ("EXIT", "Target"))

    def test_a_crossover_the_position_already_matches_is_a_no_op(self):
        strategy = InstrumentStrategy("NIFTY", "5m", target_points=999, lots=1, lot_size=75)
        strategy.warmup(ramp(100, 0.4, 40))
        strategy.open_position("CE", 100)
        strategy.previous = type(strategy.previous)(-1, 0, -1)
        decisions = [d for close in ramp(116, 2.0, 20)
                     for d in strategy.on_closed_candle(close)]
        self.assertEqual([d for d in decisions if d.side == "CE" and d.kind == "ENTER"], [])


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
    """A falling then rallying underlying — the rally crosses MACD upward."""

    CE_KEY = "NSE_FO|CE25100"
    CE_PREMIUM = 255.0

    def setUp(self):
        self.history = ramp(120, -0.8, 60)
        self.rally = ramp(72, 1.5, 60)

    def _client(self):
        quotes = {"NSE_INDEX|Nifty 50": SPOT, self.CE_KEY: self.CE_PREMIUM}
        # The last intraday row is still forming and must be ignored.
        return FakeClient(self.history, candles(self.rally) + [["forming", 0, 0, 0, 999, 0]],
                          quotes)

    def _desk(self, *instruments):
        return {"instruments": list(instruments), "trades": [],
                "rates": dict(charges.DEFAULT_RATES)}

    def test_a_crossover_buys_the_delta_selected_call_at_its_live_premium(self):
        client = self._client()
        engine = build_runner(client, self._desk(one_instrument()))
        engine.run_cycle(MARKET_MOMENT)

        position = engine.strategies["NIFTY:5m"].position
        self.assertIsNotNone(position)
        self.assertEqual(position.side, "CE")
        self.assertEqual(position.instrument_key, self.CE_KEY)
        self.assertEqual(position.entry_price, self.CE_PREMIUM)   # live chain premium
        self.assertEqual(position.quantity, 75)
        self.assertEqual(client.orders, [])                       # paper places nothing

    def test_the_selected_contract_is_published_for_the_page(self):
        client = self._client()
        engine = build_runner(client, self._desk(one_instrument()))
        engine.run_cycle(MARKET_MOMENT)
        snapshot = engine.snapshots["NIFTY"]
        self.assertEqual(snapshot["strike"], 25100)
        self.assertTrue(snapshot["inTheMoney"])
        self.assertEqual(snapshot["premium"], self.CE_PREMIUM)

    def test_target_exit_writes_a_costed_trade_into_the_blotter(self):
        client = self._client()
        engine = build_runner(client, self._desk(one_instrument(target=20)))
        engine.run_cycle(MARKET_MOMENT)

        client.quotes[self.CE_KEY] = self.CE_PREMIUM + 20        # target reached
        engine.run_cycle(MARKET_MOMENT)

        trades = engine.state_io.desk["trades"]
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["reason"], "Target")
        self.assertEqual((trades[0]["entryPrice"], trades[0]["exitPrice"]),
                         (self.CE_PREMIUM, self.CE_PREMIUM + 20))

        # The blotter feeds the cost model, so this is net of charges.
        totals = charges.summarize(trades)["totals"]
        self.assertAlmostEqual(totals["grossPnl"], 20 * 75, places=2)
        self.assertLess(totals["netPnl"], totals["grossPnl"])
        self.assertIsNone(engine.strategies["NIFTY:5m"].position)

    def test_live_mode_with_the_flag_on_places_both_legs(self):
        client = self._client()
        engine = build_runner(client, self._desk(one_instrument(mode="live", target=20)),
                              live=True)
        engine.run_cycle(MARKET_MOMENT)
        client.quotes[self.CE_KEY] = self.CE_PREMIUM + 20
        engine.run_cycle(MARKET_MOMENT)

        self.assertEqual([order["transaction_type"] for order in client.orders], ["BUY", "SELL"])
        self.assertEqual(client.orders[0]["instrument_key"], self.CE_KEY)
        self.assertEqual(client.orders[0]["quantity"], 75)

    def test_both_timeframes_run_side_by_side(self):
        client = self._client()
        engine = build_runner(client, self._desk(one_instrument("1m"), one_instrument("5m")))
        engine.run_cycle(MARKET_MOMENT)
        self.assertEqual(sorted(engine.strategies), ["NIFTY:1m", "NIFTY:5m"])
        self.assertTrue(all(s.position for s in engine.strategies.values()))

    def test_a_forming_candle_is_never_acted_on(self):
        client = self._client()
        engine = build_runner(client, self._desk(one_instrument()))
        engine.run_cycle(MARKET_MOMENT)
        self.assertEqual(engine._seen_candle["NIFTY:5m"], candles(self.rally)[-1][0])

    def test_one_bad_symbol_does_not_stop_the_others(self):
        client = self._client()
        broken = {**one_instrument(), "symbol": "BROKEN"}
        engine = build_runner(client, self._desk(broken, one_instrument()))
        engine.run_cycle(MARKET_MOMENT)

        self.assertIn("BROKEN:5m", engine.errors)
        self.assertIsNotNone(engine.strategies["NIFTY:5m"].position)

    def test_square_off_flattens_open_positions(self):
        client = self._client()
        engine = build_runner(client, self._desk(one_instrument(target=9999)))
        engine.run_cycle(MARKET_MOMENT)
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
