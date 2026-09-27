import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from order_service import CheckoutService


class CouponAcceptanceTests(unittest.TestCase):
    def test_twenty_percent_discount(self):
        self.assertEqual(CheckoutService().total(
            [{"unit_price": 100, "quantity": 1}], discount_percent=20), 80.0)

    def test_discount_is_applied_after_aggregating_cart(self):
        items = [{"unit_price": 19.99, "quantity": 3}, {"unit_price": 40.03, "quantity": 1}]
        self.assertEqual(CheckoutService().total(items, discount_percent=25), 75.0)

    def test_discount_boundaries(self):
        service = CheckoutService()
        items = [{"unit_price": 100, "quantity": 1}]
        self.assertEqual(service.total(items, discount_percent=0), 100.0)
        self.assertEqual(service.total(items, discount_percent=100), 0.0)

    def test_invalid_discount_values(self):
        service = CheckoutService()
        for value in (-0.01, 100.01):
            with self.subTest(value=value), self.assertRaises(ValueError):
                service.total([{"unit_price": 100, "quantity": 1}], discount_percent=value)
