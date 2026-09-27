import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from order_service.pricing import calculate_line_total, calculate_total


class PricingRefactorAcceptanceTests(unittest.TestCase):
    def test_helper_calculates_unrounded_line_extension(self):
        self.assertAlmostEqual(calculate_line_total({"unit_price": 1.239, "quantity": 3}), 3.717)

    def test_aggregate_rounds_once(self):
        self.assertEqual(calculate_total([
            {"unit_price": 0.105, "quantity": 1},
            {"unit_price": 0.105, "quantity": 1},
        ]), 0.21)
