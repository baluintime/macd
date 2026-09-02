"""The MACD readout — the rows, the chart geometry, and the CSV export."""

import csv
import io
import tempfile
import unittest
from pathlib import Path

from macd_desk import analysis
from macd_desk.broker.tokens import Token, TokenStore
from macd_desk.engine.indicators import Macd, macd_rows


def ramp(start, step, count):
    return [start + step * i for i in range(count)]


def candles(closes, day="2026-09-02"):
    return [[f"{day}T{9 + i // 60:02d}:{i % 60:02d}:00+05:30",
             close - 0.5, close + 1.0, close - 1.0, close, 1000]
            for i, close in enumerate(closes)]


SERIES = ramp(120, -0.8, 60) + ramp(72, 1.5, 60)


class RowTests(unittest.TestCase):
    def setUp(self):
        self.rows = analysis.build_rows(candles(SERIES))

    def test_one_row_per_candle_with_the_raw_prices_intact(self):
        self.assertEqual(len(self.rows), len(SERIES))
        self.assertEqual(self.rows[0]["close"], SERIES[0])
        self.assertEqual(self.rows[-1]["close"], SERIES[-1])
        self.assertIn("high", self.rows[0])

    def test_values_are_absent_until_the_warmup_completes(self):
        warmup = Macd().warmup_candles
        self.assertIsNone(self.rows[warmup - 2]["macd"])
        self.assertIsNotNone(self.rows[warmup - 1]["macd"])

    def test_both_emas_are_exposed_so_a_mismatch_can_be_traced(self):
        row = self.rows[-1]
        self.assertIsNotNone(row["emaFast"])
        self.assertIsNotNone(row["emaSlow"])
        # The MACD line is exactly the gap between them.
        self.assertAlmostEqual(row["macd"], row["emaFast"] - row["emaSlow"], places=9)
        self.assertAlmostEqual(row["histogram"], row["macd"] - row["signal"], places=9)

    def test_crossovers_are_marked_on_the_row_they_happen(self):
        marked = [row for row in self.rows if row["cross"]]
        self.assertTrue(marked)
        self.assertIn(marked[0]["cross"], ("BULLISH", "BEARISH"))

    def test_rows_match_the_indicator_used_by_the_engine(self):
        self.assertEqual(self.rows, macd_rows(candles(SERIES)))


class ChartTests(unittest.TestCase):
    def setUp(self):
        self.rows = analysis.build_rows(candles(SERIES))
        self.chart = analysis.build_chart(self.rows)

    def test_the_chart_plots_only_candles_that_have_values(self):
        ready = [row for row in self.rows if row["macd"] is not None]
        self.assertEqual(self.chart["count"], min(len(ready), analysis.CHART_CANDLES))

    def test_every_tick_names_a_level_the_data_reaches(self):
        values = [row["macd"] for row in self.rows if row["macd"] is not None]
        low, high = min(values), max(values)
        span = high - low
        for tick in self.chart["ticks"]:
            self.assertGreaterEqual(tick["value"], low - span)
            self.assertLessEqual(tick["value"], high + span)

    def test_marks_stay_inside_the_drawing(self):
        for bar in self.chart["bars"]:
            self.assertGreaterEqual(bar["y"], 0)
            self.assertLessEqual(bar["y"] + bar["height"], self.chart["height"])
            self.assertLessEqual(bar["x"] + bar["width"], self.chart["width"])

    def test_both_lines_share_one_scale(self):
        # A single y-mapping means a crossing on screen is a crossing in the data.
        zero = self.chart["zeroY"]
        for bar in self.chart["bars"]:
            if bar["positive"]:
                self.assertLessEqual(bar["y"], zero + 0.01)
            else:
                self.assertGreaterEqual(bar["y"] + bar["height"], zero - 0.01)

    def test_a_series_too_short_to_plot_returns_nothing(self):
        self.assertIsNone(analysis.build_chart(analysis.build_rows(candles(ramp(100, 1, 10)))))


class SummaryTests(unittest.TestCase):
    def test_the_stance_follows_the_histogram(self):
        rising = analysis.summarise(analysis.build_rows(candles(SERIES)))
        self.assertEqual(rising["stance"], "Bullish")
        falling = analysis.summarise(analysis.build_rows(
            candles(ramp(72, 1.5, 60) + ramp(162, -1.5, 60))))
        self.assertEqual(falling["stance"], "Bearish")

    def test_an_empty_series_summarises_without_error(self):
        summary = analysis.summarise([])
        self.assertEqual(summary["candles"], 0)
        self.assertIsNone(summary["latest"])
        self.assertEqual(summary["stance"], "—")


class ConnectedAppTestCase(unittest.TestCase):
    """The MACD page needs a connected broker, so stand one up."""

    def setUp(self):
        from macd_desk.app import create_app
        from macd_desk.config import Settings, UpstoxSettings

        self.tmp = tempfile.TemporaryDirectory()
        token_file = Path(self.tmp.name) / "token.json"
        TokenStore(token_file).save(Token.issued_now("tok-1"))

        settings = Settings(upstox=UpstoxSettings(
            api_key="k", api_secret="s", redirect_uri="r", token_file=token_file))
        self.app = create_app(Path(self.tmp.name) / "desk.json", settings=settings)

        history, intraday = candles(SERIES[:60]), candles(SERIES[60:], day="2026-09-03")
        broker = self.app.config["BROKER"]
        broker.historical_candles = lambda key, tf, days=3: history
        broker.intraday_candles = lambda key, tf: intraday
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()


class MacdPageTests(ConnectedAppTestCase):
    def test_the_page_renders_values_and_the_chart(self):
        response = self.client.get("/macd/NIFTY")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("MACD 12/26/9", html)
        self.assertIn("<svg", html)
        self.assertIn("Signal line", html)

    def test_the_timeframe_can_be_switched(self):
        self.assertIn("1-min", self.client.get("/macd/NIFTY?tf=1m").get_data(as_text=True))
        self.assertEqual(self.client.get("/macd/NIFTY?tf=nonsense").status_code, 200)

    def test_an_unknown_symbol_is_sent_back_with_a_reason(self):
        response = self.client.get("/macd/NOTONDESK")
        self.assertEqual(response.status_code, 302)
        self.assertIn("problem=", response.headers["Location"])

    def test_the_desk_links_to_the_readout_for_every_instrument(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("/macd/NIFTY", html)
        self.assertIn("/macd/BANKNIFTY", html)


class MacdCsvTests(ConnectedAppTestCase):
    def test_the_csv_carries_every_candle_and_every_column(self):
        response = self.client.get("/macd/NIFTY.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertIn("NIFTY-5m-macd-", response.headers["Content-Disposition"])

        rows = list(csv.reader(io.StringIO(response.get_data(as_text=True))))
        self.assertEqual(tuple(rows[0]), analysis.CSV_COLUMNS)
        self.assertEqual(len(rows) - 1, len(SERIES))          # header + every candle

    def test_warmup_rows_export_as_empty_not_zero(self):
        rows = list(csv.DictReader(io.StringIO(
            self.client.get("/macd/NIFTY.csv").get_data(as_text=True))))
        self.assertEqual(rows[0]["macd"], "")                 # genuinely no value yet
        self.assertNotEqual(rows[-1]["macd"], "")

    def test_the_exported_numbers_match_the_page(self):
        rows = list(csv.DictReader(io.StringIO(
            self.client.get("/macd/NIFTY.csv").get_data(as_text=True))))
        last = rows[-1]
        self.assertAlmostEqual(float(last["macd"]),
                               float(last["emaFast"]) - float(last["emaSlow"]), places=9)
        self.assertAlmostEqual(float(last["histogram"]),
                               float(last["macd"]) - float(last["signal"]), places=9)

    def test_a_disconnected_desk_is_told_to_connect(self):
        self.app.config["BROKER"].tokens.clear()
        response = self.client.get("/macd/NIFTY.csv")
        self.assertEqual(response.status_code, 302)
        self.assertIn("Connect+to+Upstox", response.headers["Location"])


if __name__ == "__main__":
    unittest.main()
