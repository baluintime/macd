"""Rupee formatting — Indian digit grouping without a locale dependency."""

import unittest

from macd_desk import formatting as fmt


class GroupingTests(unittest.TestCase):
    def test_indian_grouping_is_three_then_pairs(self):
        self.assertEqual(fmt.group_indian("100"), "100")
        self.assertEqual(fmt.group_indian("1000"), "1,000")
        self.assertEqual(fmt.group_indian("185781"), "1,85,781")
        self.assertEqual(fmt.group_indian("12345678"), "1,23,45,678")

    def test_money_and_signed(self):
        self.assertEqual(fmt.money(185781.75), "₹1,85,781.75")
        self.assertEqual(fmt.signed(12160.75), "₹12,160.75")
        self.assertEqual(fmt.signed(-1068.75), "−₹1,068.75")

    def test_sign_class(self):
        self.assertEqual(fmt.sign_class(1), "pos")
        self.assertEqual(fmt.sign_class(-1), "neg")
        self.assertEqual(fmt.sign_class(0), "")

    def test_units(self):
        self.assertEqual(fmt.points(0.27), "0.27 pts")
        self.assertEqual(fmt.pct(4.19), "4.19%")
        self.assertEqual(fmt.qty(11000), "11,000")


if __name__ == "__main__":
    unittest.main()


class PlainNumberTests(unittest.TestCase):
    def test_integral_values_lose_the_decimal_tail(self):
        self.assertEqual(fmt.plain(40.0), "40")
        self.assertEqual(fmt.plain(20), "20")

    def test_fractional_values_survive_intact(self):
        self.assertEqual(fmt.plain(142.5), "142.5")
        self.assertEqual(fmt.plain(128.20), "128.2")
        self.assertEqual(fmt.plain(0.03503), "0.03503")
        self.assertEqual(fmt.plain(0.0001), "0.0001")
