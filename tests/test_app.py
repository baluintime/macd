"""The web layer: server-side rendering, the JSON API, and the no-JS form path."""

import json
import re
import tempfile
import unittest
from pathlib import Path

from macd_desk import state as state_module
from macd_desk.app import create_app, state_from_form


def _sample_form(**overrides):
    form = {
        "trade-0-symbol": "NIFTY", "trade-0-side": "CE", "trade-0-reason": "Target",
        "trade-0-entryPrice": "100", "trade-0-exitPrice": "130",
        "trade-0-lots": "2", "trade-0-lotSize": "75",
    }
    form.update(overrides)
    return form


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

    def rendered_net(self, html):
        match = re.search(r'id="kpi-net"[^>]*>([^<]+)', html)
        return match.group(1).strip() if match else None


class RenderTests(AppTestCase):
    def test_a_fresh_desk_opens_with_an_empty_book(self):
        response = self.client.get("/")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        # The net profit is in the HTML itself, not fetched by script.
        self.assertEqual(self.rendered_net(html), "₹0.00")
        self.assertIn("No trades yet", html)
        self.assertIn("Upstox MACD Options Desk", html)

    def test_the_page_ships_no_sample_or_placeholder_prices(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("Load sample", html)
        # Without a live chain there is no premium, so projections read as unavailable.
        self.assertIn("Not selected", html)
        self.assertIn("Not connected", html)

    def test_trades_render_once_the_book_has_them(self):
        self.client.post("/", data=_sample_form(action="save"), follow_redirects=True)
        html = self.client.get("/").get_data(as_text=True)
        self.assertEqual(self.rendered_net(html), "₹4,418.80")

    def test_every_srs_control_is_present_for_each_instrument(self):
        html = self.client.get("/").get_data(as_text=True)
        for index in range(len(state_module.default_instruments())):
            self.assertIn(f'name="inst-{index}-mode"', html)        # execution mode
            self.assertIn(f'name="inst-{index}-lots"', html)        # position size
            self.assertIn(f'name="inst-{index}-targetPoints"', html)
            self.assertIn(f'name="inst-{index}-timeframe"', html)   # engine timeframe

    def test_health_endpoint(self):
        self.assertEqual(self.client.get("/healthz").get_json(), {"status": "ok"})


class ApiTests(AppTestCase):
    def test_api_book_costs_the_submitted_form(self):
        payload = self.client.post("/api/book", data=_sample_form()).get_json()
        self.assertEqual(payload["fields"]["kpi-net"]["text"], "₹4,418.80")
        self.assertEqual(payload["fields"]["kpi-charges"]["text"], "₹81.20")
        self.assertEqual(payload["fields"]["row-0-net"]["text"], "₹4,418.80")

    def test_api_book_does_not_persist(self):
        # Rendering alone writes nothing — a fresh install has no state file.
        self.client.get("/")
        self.assertFalse(self.state_path.exists())

        self.client.post("/", data=_sample_form(action="save"), follow_redirects=True)
        stored_before = self.stored()
        self.client.post("/api/book", data=_sample_form())
        self.assertEqual(self.stored(), stored_before)

    def test_a_loss_is_flagged_for_the_page_to_colour(self):
        payload = self.client.post("/api/book", data=_sample_form(
            **{"trade-0-entryPrice": "130", "trade-0-exitPrice": "100"})).get_json()
        self.assertEqual(payload["fields"]["kpi-net"]["cls"], "neg")
        self.assertTrue(payload["fields"]["kpi-net"]["text"].startswith("−₹"))

    def test_junk_input_is_coerced_rather_than_erroring(self):
        response = self.client.post("/api/book", data=_sample_form(
            **{"trade-0-entryPrice": "abc", "trade-0-lots": "-3"}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["fields"]["kpi-net"]["text"], "₹0.00")

    def test_api_state_saves(self):
        self.client.post("/api/state", data=_sample_form())
        self.assertEqual(len(self.stored()["trades"]), 1)


class FormActionTests(AppTestCase):
    def test_add_and_delete_a_trade_without_javascript(self):
        self.client.get("/")
        self.client.post("/", data={"action": "add-trade"}, follow_redirects=True)
        self.assertEqual(len(self.stored()["trades"]), 1)
        self.client.post("/", data={"action": "add-trade"}, follow_redirects=True)
        self.assertEqual(len(self.stored()["trades"]), 2)
        self.client.post("/", data={"delete-index": "0"}, follow_redirects=True)
        self.assertEqual(len(self.stored()["trades"]), 1)

    def test_clearing_empties_the_book(self):
        self.client.post("/", data=_sample_form(action="save"), follow_redirects=True)
        self.assertEqual(len(self.stored()["trades"]), 1)
        self.client.post("/", data={"action": "clear-trades"}, follow_redirects=True)
        self.assertEqual(self.stored()["trades"], [])
        self.assertIn("No trades yet", self.client.get("/").get_data(as_text=True))

    def test_a_rate_edit_moves_the_net_and_reset_restores_it(self):
        self.client.post("/", data=_sample_form(action="save"), follow_redirects=True)
        before = self.rendered_net(self.client.get("/").get_data(as_text=True))
        self.client.post("/", data={
            "action": "save", "rate-brokeragePerOrder": "0", "rate-brokeragePctCap": "0",
        }, follow_redirects=True)
        after = self.rendered_net(self.client.get("/").get_data(as_text=True))
        self.assertNotEqual(before, after)
        self.assertEqual(self.stored()["rates"]["brokeragePerOrder"], 0)

        self.client.post("/", data={"action": "reset-rates"}, follow_redirects=True)
        self.assertEqual(self.rendered_net(self.client.get("/").get_data(as_text=True)), before)

    def test_editing_a_trade_through_the_form_persists(self):
        self.client.post("/", data=_sample_form(action="save"), follow_redirects=True)
        trades = self.stored()["trades"]
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["exitPrice"], 130.0)


class ExportTests(AppTestCase):
    def test_csv_download_carries_the_costed_rows_and_a_total(self):
        self.client.post("/", data=_sample_form(action="save"), follow_redirects=True)
        response = self.client.get("/export.csv")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        lines = [line for line in body.splitlines() if line]
        self.assertEqual(len(lines), 3)             # header + 1 trade + total
        self.assertTrue(lines[-1].startswith("TOTAL"))
        self.assertIn("4418.8", lines[-1])


class StatePersistenceTests(AppTestCase):
    def test_a_corrupt_state_file_falls_back_to_defaults(self):
        self.state_path.write_text("{not json at all")
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.rendered_net(response.get_data(as_text=True)), "₹0.00")
        self.assertEqual(len(self.client.get("/api/state").get_json()["instruments"]), 6)

    def test_state_survives_a_restart(self):
        self.client.post("/", data=_sample_form(action="save"), follow_redirects=True)
        fresh = create_app(self.state_path).test_client()
        self.assertEqual(len(fresh.get("/api/state").get_json()["trades"]), 1)


class FormParsingTests(unittest.TestCase):
    def test_missing_trade_fields_keep_the_current_book(self):
        current = state_module.default_state()
        current["trades"] = [{"symbol": "NIFTY", "side": "CE", "reason": "Target",
                              "entryPrice": 100, "exitPrice": 120, "lots": 1, "lotSize": 75}]
        rebuilt = state_from_form({"action": "add-trade"}, current)
        self.assertEqual(len(rebuilt["trades"]), len(current["trades"]))

    def test_an_empty_blotter_is_respected_not_treated_as_missing(self):
        current = state_module.default_state()
        rebuilt = state_from_form({"trade-0-symbol": ""}, current)
        self.assertEqual(len(rebuilt["trades"]), 1)


if __name__ == "__main__":
    unittest.main()
