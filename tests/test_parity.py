"""The published static preview must agree with the Python model to the paisa.

preview/charges.js exists only to keep the shareable page interactive without a
server. Two implementations of one cost model is a drift risk, so this pins them
together. Skipped where Node is not installed — it guards the preview bundle,
not the app.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from macd_desk import charges

PREVIEW = Path(__file__).resolve().parent.parent / "preview" / "charges.js"

CASES = [
    {"entryPrice": 128.20, "exitPrice": 148.20, "lots": 1, "lotSize": 75},
    {"entryPrice": 112.60, "exitPrice": 98.35, "lots": 1, "lotSize": 75},
    {"entryPrice": 245.00, "exitPrice": 280.00, "lots": 3, "lotSize": 35},
    {"entryPrice": 22.40, "exitPrice": 32.40, "lots": 2, "lotSize": 550},
    {"entryPrice": 1.05, "exitPrice": 1.10, "lots": 1, "lotSize": 100},   # % cap territory
    {"entryPrice": 0, "exitPrice": 0, "lots": 0, "lotSize": 0},           # degenerate
    {"entryPrice": 1000.005, "exitPrice": 1200.005, "lots": 1, "lotSize": 15},
]


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ParityTests(unittest.TestCase):
    def test_javascript_and_python_agree_on_every_case(self):
        script = (
            f"const C = require({str(PREVIEW)!r});"
            f"const cases = {json.dumps(CASES)};"
            "console.log(JSON.stringify(cases.map(c => C.computeTrade(c))));"
        )
        output = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, check=True).stdout
        js_results = json.loads(output)

        for case, from_js in zip(CASES, js_results):
            with self.subTest(case=case):
                from_py = charges.compute_trade(case)
                self.assertEqual(from_py["grossPnl"], from_js["grossPnl"])
                self.assertEqual(from_py["totalCharges"], from_js["totalCharges"])
                self.assertEqual(from_py["netPnl"], from_js["netPnl"])
                self.assertEqual(from_py["breakEvenPoints"], from_js["breakEvenPoints"])
                self.assertEqual(from_py["charges"], from_js["charges"])


if __name__ == "__main__":
    unittest.main()
