"""The web layer: server-side rendering, the JSON API, and the no-JS form path."""

import csv
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from macd_desk import state as state_module
from macd_desk.app import create_app, state_from_form


def executed_trade(mode="paper", **overrides):
    """A round trip as the engine records it — the only way a trade exists."""
    trade = {
        "symbol": "NIFTY", "side": "CE", "reason": "Target", "timeframe": "5m",
        "mode": mode, "contract": "NIFTY 25100 CE", "strike": 25100,
        "entryAt": "2026-09-03 10:15:00", "exitAt": "2026-09-03 10:23:30",
        "entryPrice": 100, "exitPrice": 130, "lots": 2, "lotSize": 75,
    }
    trade.update(overrides)
    return trade


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "desk-state.json"
        self.app = create_app(self.state_path)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def stored(self):
        return json.loads(self.state_path.read_text())

    def rendered_net(self, html, book="paper"):
        match = re.search(rf'id="{book}-net"[^>]*>([^<]+)', html)
        return match.group(1).strip() if match else None

    def seed(self, *trades):
        """Write trades the way the engine does, straight into desk state."""
        desk = state_module.default_state()
        desk["trades"] = list(trades)
        state_module.save(self.state_path, desk)
        return desk


class RenderTests(AppTestCase):
    def test_a_fresh_desk_opens_with_both_books_empty(self):
        response = self.client.get("/")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        # The net profit is in the HTML itself, not fetched by script.
        self.assertEqual(self.rendered_net(html, "paper"), "₹0.00")
        self.assertEqual(self.rendered_net(html, "live"), "₹0.00")
        self.assertIn("No paper trades yet", html)
        self.assertIn("No live trades", html)

    def test_the_page_ships_no_sample_or_placeholder_data(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("Load sample", html)
        # Nothing on the page can create a trade by hand any more.
        self.assertNotIn("Add trade", html)
        self.assertNotIn('name="trade-0-entryPrice"', html)
        # Without a live chain there is no premium, so projections read as unavailable.
        self.assertIn("No live contract", html)
        self.assertIn("Not connected", html)

    def test_the_engine_is_on_the_desk(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Autotrade engine", html)
        self.assertIn("Start engine", html)
        self.assertIn("Open positions", html)
        self.assertIn("Engine log", html)

    def test_each_book_reports_only_its_own_trades(self):
        self.seed(executed_trade("paper"),
                  executed_trade("live", symbol="HDFCBANK", entryPrice=20, exitPrice=25,
                                 lots=1, lotSize=550, contract="HDFCBANK 960 CE"))
        html = self.client.get("/").get_data(as_text=True)
        # 30 points x 150 = 4500 gross on paper; 5 x 550 = 2750 on live.
        self.assertEqual(self.rendered_net(html, "paper"), "₹4,418.80")
        self.assertNotEqual(self.rendered_net(html, "live"), "₹0.00")
        self.assertIn("NIFTY 25100 CE", html)
        self.assertIn("HDFCBANK 960 CE", html)

    def test_every_srs_control_is_present_for_each_instrument(self):
        html = self.client.get("/").get_data(as_text=True)
        for index in range(len(state_module.default_instruments())):
            self.assertIn(f'name="inst-{index}-mode"', html)        # execution mode
            self.assertIn(f'name="inst-{index}-lots"', html)        # position size
            self.assertIn(f'name="inst-{index}-target1m"', html)    # target, per engine
            self.assertIn(f'name="inst-{index}-target5m"', html)

    def test_health_endpoint(self):
        self.assertEqual(self.client.get("/healthz").get_json(), {"status": "ok"})


class ApiTests(AppTestCase):
    def test_api_book_costs_the_stored_trades(self):
        self.seed(executed_trade("paper"))
        payload = self.client.post("/api/book", data={}).get_json()
        self.assertEqual(payload["fields"]["paper-net"]["text"], "₹4,418.80")
        self.assertEqual(payload["fields"]["paper-charges"]["text"], "₹81.20")
        self.assertEqual(payload["fields"]["row-0-net"]["text"], "₹4,418.80")
        self.assertEqual(payload["fields"]["live-net"]["text"], "₹0.00")

    def test_a_rate_change_recosts_without_touching_the_trades(self):
        self.seed(executed_trade("paper"))
        payload = self.client.post("/api/book", data={
            "rate-brokeragePerOrder": "0", "rate-brokeragePctCap": "0"}).get_json()
        self.assertEqual(payload["fields"]["paper-charges"]["text"], "₹34.00")

    def test_api_book_does_not_persist(self):
        # Rendering alone writes nothing — a fresh install has no state file.
        self.client.get("/")
        self.assertFalse(self.state_path.exists())

        self.seed(executed_trade("paper"))
        stored_before = self.stored()
        self.client.post("/api/book", data={"rate-gstPct": "0"})
        self.assertEqual(self.stored(), stored_before)

    def test_a_loss_is_flagged_for_the_page_to_colour(self):
        self.seed(executed_trade("paper", entryPrice=130, exitPrice=100))
        payload = self.client.post("/api/book", data={}).get_json()
        self.assertEqual(payload["fields"]["paper-net"]["cls"], "neg")
        self.assertTrue(payload["fields"]["paper-net"]["text"].startswith("−₹"))

    def test_junk_input_is_coerced_rather_than_erroring(self):
        response = self.client.post("/api/book", data={
            "inst-0-lots": "abc", "rate-gstPct": "-5"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["fields"]["paper-net"]["text"], "₹0.00")

    def test_api_state_saves_configuration_only(self):
        self.seed(executed_trade("live"))
        self.client.post("/api/state", data={"inst-0-lots": "4"})
        stored = self.stored()
        self.assertEqual(stored["instruments"][0]["lots"], 4)
        self.assertEqual(len(stored["trades"]), 1)          # the book is untouched
        self.assertEqual(stored["trades"][0]["mode"], "live")


class FormActionTests(AppTestCase):
    def test_the_form_cannot_create_a_trade(self):
        self.client.get("/")
        self.client.post("/", data={"action": "add-trade"}, follow_redirects=True)
        self.assertEqual(self.stored()["trades"], [])
        # Nor by posting trade fields directly.
        self.client.post("/", data={"trade-0-symbol": "FORGED", "trade-0-entryPrice": "1",
                                    "trade-0-exitPrice": "9999", "trade-0-lots": "100",
                                    "trade-0-lotSize": "75"}, follow_redirects=True)
        self.assertEqual(self.stored()["trades"], [])

    def test_the_form_cannot_edit_an_executed_trade(self):
        self.seed(executed_trade("live"))
        self.client.post("/", data={"trade-0-exitPrice": "99999",
                                    "trade-0-mode": "paper"}, follow_redirects=True)
        stored = self.stored()["trades"][0]
        self.assertEqual(stored["exitPrice"], 130)
        self.assertEqual(stored["mode"], "live")

    def test_clearing_empties_both_books(self):
        self.seed(executed_trade("paper"), executed_trade("live"))
        self.client.post("/", data={"action": "clear-trades"}, follow_redirects=True)
        self.assertEqual(self.stored()["trades"], [])
        self.assertIn("No paper trades yet", self.client.get("/").get_data(as_text=True))

    def test_a_rate_edit_moves_the_net_and_reset_restores_it(self):
        self.seed(executed_trade("paper"))
        before = self.rendered_net(self.client.get("/").get_data(as_text=True))
        self.client.post("/", data={
            "action": "save", "rate-brokeragePerOrder": "0", "rate-brokeragePctCap": "0",
        }, follow_redirects=True)
        after = self.rendered_net(self.client.get("/").get_data(as_text=True))
        self.assertNotEqual(before, after)
        self.assertEqual(self.stored()["rates"]["brokeragePerOrder"], 0)

        self.client.post("/", data={"action": "reset-rates"}, follow_redirects=True)
        self.assertEqual(self.rendered_net(self.client.get("/").get_data(as_text=True)), before)

    def test_instrument_configuration_still_persists(self):
        self.client.post("/", data={"inst-0-lots": "3", "inst-0-target1m": "8",
                                    "inst-0-target5m": "45", "inst-0-mode": "live"},
                         follow_redirects=True)
        instrument = self.stored()["instruments"][0]
        self.assertEqual((instrument["lots"], instrument["target1m"],
                          instrument["target5m"]), (3, 8, 45))
        self.assertEqual(instrument["mode"], "live")


class ExportTests(AppTestCase):
    def test_csv_download_carries_the_costed_rows_and_a_total(self):
        self.seed(executed_trade("paper"))
        response = self.client.get("/export.csv")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        lines = [line for line in body.splitlines() if line]
        # header + trade + paper subtotal + 5m subtotal + total
        self.assertEqual(len(lines), 5)
        self.assertTrue(lines[-1].startswith("TOTAL"))
        self.assertIn("4418.8", lines[-1])

    def test_the_csv_says_which_engine_made_each_trade(self):
        self.seed(executed_trade("paper", timeframe="1m"),
                  executed_trade("live", timeframe="5m", symbol="HDFCBANK"))
        rows = list(csv.DictReader(io.StringIO(
            self.client.get("/export.csv").get_data(as_text=True))))

        trades = [row for row in rows
                  if not row["Buy time"].startswith(("SUBTOTAL", "TOTAL"))]
        self.assertEqual([row["Timeframe"] for row in trades], ["1m", "5m"])
        self.assertEqual([row["Book"] for row in trades], ["paper", "live"])
        self.assertEqual([row["Contract"] for row in trades],
                         ["NIFTY 25100 CE", "NIFTY 25100 CE"])

    def test_the_csv_carries_both_legs_and_how_long_the_trade_was_held(self):
        self.seed(executed_trade("paper"))
        rows = list(csv.DictReader(io.StringIO(
            self.client.get("/export.csv").get_data(as_text=True))))
        trade = rows[0]
        self.assertEqual(trade["Buy time"], "2026-09-03 10:15:00")
        self.assertEqual(trade["Sell time"], "2026-09-03 10:23:30")
        self.assertEqual(trade["Held (min)"], "8.50")

    def test_a_trade_with_no_entry_time_still_exports(self):
        # Books written before both legs were timestamped.
        self.seed({**executed_trade("paper"), "entryAt": "", "exitAt": "2026-09-03 10:23:30"})
        rows = list(csv.DictReader(io.StringIO(
            self.client.get("/export.csv").get_data(as_text=True))))
        self.assertEqual(rows[0]["Buy time"], "")
        self.assertEqual(rows[0]["Held (min)"], "")

    def test_an_older_book_reads_its_single_timestamp_as_the_sell_time(self):
        trade = executed_trade("paper")
        trade.pop("entryAt"), trade.pop("exitAt")
        self.seed({**trade, "at": "2026-09-03 10:23:30"})
        rows = list(csv.DictReader(io.StringIO(
            self.client.get("/export.csv").get_data(as_text=True))))
        self.assertEqual(rows[0]["Sell time"], "2026-09-03 10:23:30")

    def test_the_csv_subtotals_each_book_and_each_timeframe(self):
        self.seed(executed_trade("paper", timeframe="1m"),
                  executed_trade("live", timeframe="5m"))
        lines = [line for line in
                 self.client.get("/export.csv").get_data(as_text=True).splitlines() if line]
        labels = [line.split(",")[0] for line in lines]
        for expected in ("SUBTOTAL paper", "SUBTOTAL live", "SUBTOTAL 1m", "SUBTOTAL 5m"):
            self.assertIn(expected, labels)


class StatePersistenceTests(AppTestCase):
    def test_a_corrupt_state_file_falls_back_to_defaults(self):
        self.state_path.write_text("{not json at all")
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.rendered_net(response.get_data(as_text=True)), "₹0.00")
        self.assertEqual(len(self.client.get("/api/state").get_json()["instruments"]), 2)

    def test_state_survives_a_restart(self):
        self.seed(executed_trade("live"))
        fresh = create_app(self.state_path).test_client()
        trades = fresh.get("/api/state").get_json()["trades"]
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["mode"], "live")


class FormParsingTests(unittest.TestCase):
    def test_the_book_is_carried_through_untouched(self):
        current = state_module.default_state()
        current["trades"] = [executed_trade("live")]
        rebuilt = state_from_form({"inst-0-lots": "2"}, current)
        self.assertEqual(len(rebuilt["trades"]), 1)
        self.assertEqual(rebuilt["trades"][0]["mode"], "live")

    def test_an_unknown_mode_falls_back_to_paper_not_live(self):
        cleaned = state_module.clean_trade({"symbol": "NIFTY", "mode": "REAL-MONEY",
                                            "entryPrice": 1, "exitPrice": 2,
                                            "lots": 1, "lotSize": 75})
        self.assertEqual(cleaned["mode"], "paper")


if __name__ == "__main__":
    unittest.main()


class BookSeparationTests(AppTestCase):
    """Simulated fills and real ones are never added together."""

    def setUp(self):
        super().setUp()
        self.seed(executed_trade("paper"),                                  # 4500 gross
                  executed_trade("live", symbol="HDFCBANK", entryPrice=20.25,
                                 exitPrice=30.25, lots=2, lotSize=550,
                                 contract="HDFCBANK 960 CE"))               # 11000 gross

    def payload(self, **form):
        return self.client.post("/api/book", data=form).get_json()["fields"]

    def test_each_book_totals_only_its_own_fills(self):
        fields = self.payload()
        self.assertEqual(fields["paper-t-gross"]["text"], "₹4,500.00")
        self.assertEqual(fields["live-t-gross"]["text"], "₹11,000.00")

    def test_a_paper_loss_never_reduces_the_live_book(self):
        self.seed(executed_trade("paper", entryPrice=200, exitPrice=100),
                  executed_trade("live", entryPrice=100, exitPrice=130))
        fields = self.payload()
        self.assertEqual(fields["paper-net"]["cls"], "neg")
        self.assertEqual(fields["live-net"]["cls"], "pos")

    def test_the_breakdown_describes_one_book_at_a_time(self):
        self.assertIn("Paper book", self.client.post(
            "/api/book", data={"book": "paper"}).get_json()["caption"])
        self.assertIn("Live book", self.client.post(
            "/api/book", data={"book": "live"}).get_json()["caption"])
        self.assertIn("Both books", self.client.post("/api/book", data={}).get_json()["caption"])

    def test_the_book_selector_survives_a_nonsense_value(self):
        response = self.client.get("/?book=; DROP TABLE")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Both books", response.get_data(as_text=True))

    def test_the_desk_labels_which_book_a_trade_belongs_to(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('class="chip paper"', html)
        self.assertIn('class="chip live"', html)


class DeskComposionTests(AppTestCase):
    """The desk ships two indices; anything else is added by the operator."""

    def symbols(self):
        return [i["symbol"] for i in self.client.get("/api/state").get_json()["instruments"]]

    def test_the_desk_starts_with_nifty_and_banknifty(self):
        self.assertEqual(self.symbols(), ["NIFTY", "BANKNIFTY"])

    def test_a_symbol_can_be_added(self):
        self.client.post("/instruments/add", data={"new-symbol": " tcs "},
                         follow_redirects=True)
        self.assertEqual(self.symbols(), ["NIFTY", "BANKNIFTY", "TCS"])
        added = self.stored()["instruments"][-1]
        self.assertEqual((added["target1m"], added["target5m"]), (10, 20))

    def test_a_duplicate_is_refused_with_a_reason(self):
        response = self.client.post("/instruments/add", data={"new-symbol": "NIFTY"})
        self.assertIn("already+on+the+desk", response.headers["Location"])
        self.assertEqual(self.symbols(), ["NIFTY", "BANKNIFTY"])

    def test_an_empty_symbol_is_refused(self):
        self.client.post("/instruments/add", data={"new-symbol": "  "})
        self.assertEqual(self.symbols(), ["NIFTY", "BANKNIFTY"])

    def test_a_symbol_can_be_removed(self):
        self.client.post("/", data={"action": "remove-instrument", "remove-index": "0"},
                         follow_redirects=True)
        self.assertEqual(self.symbols(), ["BANKNIFTY"])

    def test_removing_an_index_that_is_not_there_changes_nothing(self):
        self.client.post("/", data={"action": "remove-instrument", "remove-index": "9"},
                         follow_redirects=True)
        self.assertEqual(self.symbols(), ["NIFTY", "BANKNIFTY"])

    def test_adding_does_not_disturb_the_configuration_being_edited(self):
        self.client.post("/", data={"inst-0-lots": "4"}, follow_redirects=True)
        self.client.post("/instruments/add",
                         data={"new-symbol": "TCS", "inst-0-lots": "4"},
                         follow_redirects=True)
        self.assertEqual(self.stored()["instruments"][0]["lots"], 4)


class PerTimeframeTargetTests(AppTestCase):
    def test_each_engine_has_its_own_target(self):
        self.client.post("/", data={"inst-0-target1m": "8", "inst-0-target5m": "25"},
                         follow_redirects=True)
        instrument = self.stored()["instruments"][0]
        self.assertEqual(instrument["target1m"], 8)
        self.assertEqual(instrument["target5m"], 25)

    def test_a_book_with_one_shared_target_reads_it_for_both(self):
        desk = state_module.default_state()
        desk["instruments"][0].pop("target1m")
        desk["instruments"][0].pop("target5m")
        desk["instruments"][0]["targetPoints"] = 30
        state_module.save(self.state_path, desk)

        instrument = self.client.get("/api/state").get_json()["instruments"][0]
        self.assertEqual((instrument["target1m"], instrument["target5m"]), (30, 30))

    def test_the_card_projects_both_targets(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="inst-0-net-1m"', html)
        self.assertIn('id="inst-0-net-5m"', html)
