"""The cost model: every charge head, and the net profit it produces."""

import unittest

from macd_desk import charges, state


class ComputeTradeTests(unittest.TestCase):
    def test_gross_is_the_premium_move_times_quantity(self):
        result = charges.compute_trade(
            {"entryPrice": 128.20, "exitPrice": 148.20, "lots": 1, "lotSize": 75})
        self.assertEqual(result["qty"], 75)
        self.assertAlmostEqual(result["grossPnl"], 20 * 75, places=2)

    def test_each_head_follows_its_own_base(self):
        result = charges.compute_trade(
            {"entryPrice": 100, "exitPrice": 130, "lots": 2, "lotSize": 75})
        buy, sell = 100 * 150, 130 * 150
        turnover = buy + sell
        heads = result["charges"]

        self.assertAlmostEqual(heads["brokerage"], 40, places=2)          # flat 20 x 2 legs
        self.assertAlmostEqual(heads["stt"], sell * 0.001, places=2)      # sell leg only
        self.assertAlmostEqual(heads["exchangeTxn"], turnover * 0.0003503, places=2)
        self.assertAlmostEqual(heads["ipft"], turnover * 0.000005, places=2)
        self.assertAlmostEqual(heads["sebi"], turnover * 0.000001, places=2)
        self.assertEqual(heads["stampDuty"], charges.half_up(buy * 0.00003))
        self.assertAlmostEqual(
            heads["gst"],
            (heads["brokerage"] + heads["exchangeTxn"] + heads["sebi"] + heads["ipft"]) * 0.18,
            places=2)

    def test_net_is_gross_less_every_head(self):
        result = charges.compute_trade(
            {"entryPrice": 100, "exitPrice": 130, "lots": 2, "lotSize": 75})
        self.assertAlmostEqual(result["totalCharges"], sum(result["charges"].values()), places=2)
        self.assertAlmostEqual(result["netPnl"],
                               result["grossPnl"] - result["totalCharges"], places=2)
        self.assertLess(result["netPnl"], result["grossPnl"])

    def test_brokerage_falls_back_to_the_percentage_cap(self):
        # 2.5% of a Rs 100 leg is Rs 2.50, below the Rs 20 flat fee.
        result = charges.compute_trade(
            {"entryPrice": 1, "exitPrice": 1, "lots": 1, "lotSize": 100})
        self.assertAlmostEqual(result["charges"]["brokerage"], 5.0, places=2)

    def test_break_even_is_the_per_unit_cost(self):
        result = charges.compute_trade(
            {"entryPrice": 100, "exitPrice": 100, "lots": 1, "lotSize": 75})
        self.assertAlmostEqual(result["breakEvenPoints"], result["totalCharges"] / 75, places=2)
        self.assertAlmostEqual(result["netPnl"], -result["totalCharges"], places=2)

    def test_a_losing_trade_carries_charges_on_top_of_the_loss(self):
        result = charges.compute_trade(
            {"entryPrice": 120, "exitPrice": 100, "lots": 1, "lotSize": 75})
        self.assertLess(result["grossPnl"], 0)
        self.assertLess(result["netPnl"], result["grossPnl"])

    def test_rate_overrides_do_not_mutate_the_defaults(self):
        base = charges.compute_trade({"entryPrice": 100, "exitPrice": 110, "lots": 1, "lotSize": 75})
        free = charges.compute_trade(
            {"entryPrice": 100, "exitPrice": 110, "lots": 1, "lotSize": 75},
            {"brokeragePerOrder": 0, "brokeragePctCap": 0})
        self.assertEqual(free["charges"]["brokerage"], 0)
        self.assertGreater(free["netPnl"], base["netPnl"])
        self.assertEqual(charges.DEFAULT_RATES["brokeragePerOrder"], 20)

    def test_junk_input_is_coerced_not_raised(self):
        result = charges.compute_trade(
            {"entryPrice": "abc", "exitPrice": None, "lots": "", "lotSize": 75})
        self.assertEqual(result["grossPnl"], 0)
        self.assertEqual(result["breakEvenPoints"], 0)

    def test_zero_quantity_does_not_divide_by_zero(self):
        result = charges.compute_trade(
            {"entryPrice": 100, "exitPrice": 120, "lots": 0, "lotSize": 75})
        self.assertEqual(result["breakEvenPoints"], 0)


class ProjectionTests(unittest.TestCase):
    def test_projection_exits_exactly_target_points_above_entry(self):
        projection = charges.project_at_target(
            {"entryPrice": 142.5, "targetPoints": 20, "lots": 1, "lotSize": 75})
        self.assertAlmostEqual(projection["grossPnl"], 20 * 75, places=2)
        self.assertAlmostEqual(projection["netPnl"],
                               projection["grossPnl"] - projection["totalCharges"], places=2)


class SummarizeTests(unittest.TestCase):
    TRADES = [
        {"symbol": "NIFTY", "entryPrice": 100, "exitPrice": 120, "lots": 1, "lotSize": 75},
        {"symbol": "NIFTY", "entryPrice": 100, "exitPrice": 90, "lots": 1, "lotSize": 75},
    ]

    def test_book_totals_are_the_sum_of_the_rows(self):
        book = charges.summarize(self.TRADES)
        rows, totals = book["rows"], book["totals"]
        self.assertEqual(len(rows), 2)
        self.assertEqual((totals["trades"], totals["wins"], totals["losses"]), (2, 1, 1))
        self.assertAlmostEqual(totals["grossPnl"], rows[0]["grossPnl"] + rows[1]["grossPnl"], places=2)
        self.assertAlmostEqual(totals["totalCharges"],
                               rows[0]["totalCharges"] + rows[1]["totalCharges"], places=2)
        self.assertAlmostEqual(totals["netPnl"], totals["grossPnl"] - totals["totalCharges"], places=2)
        self.assertAlmostEqual(totals["charges"]["stt"],
                               rows[0]["charges"]["stt"] + rows[1]["charges"]["stt"], places=2)

    def test_empty_book_is_zeroes_not_errors(self):
        totals = charges.summarize([])["totals"]
        self.assertEqual(totals["netPnl"], 0)
        self.assertEqual(totals["chargeRatioPct"], 0)
        self.assertEqual(totals["avgBreakEvenPoints"], 0)

    def test_breakdown_ranks_heads_and_shares_sum_to_a_hundred(self):
        totals = charges.summarize(self.TRADES)["totals"]
        breakdown = charges.charge_breakdown(totals)
        self.assertEqual(breakdown[0]["key"], "brokerage")
        amounts = [head["amount"] for head in breakdown]
        self.assertEqual(amounts, sorted(amounts, reverse=True))
        self.assertAlmostEqual(sum(head["sharePct"] for head in breakdown), 100.0, delta=0.1)

    def test_sample_session_nets_the_documented_figure(self):
        totals = charges.summarize(state.sample_trades())["totals"]
        self.assertAlmostEqual(totals["grossPnl"], 12160.75, places=2)
        self.assertAlmostEqual(totals["totalCharges"], 509.50, places=2)
        self.assertAlmostEqual(totals["netPnl"], 11651.25, places=2)


class RoundingTests(unittest.TestCase):
    def test_half_up_not_bankers(self):
        # Python's round() would give 2 here; a contract note gives 3.
        self.assertEqual(charges.half_up(2.5), 3)
        self.assertEqual(charges.half_up(-2.5), -3)
        self.assertEqual(charges.round2(1.005), 1.01)


if __name__ == "__main__":
    unittest.main()
