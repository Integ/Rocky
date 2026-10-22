import unittest

from pricing import apply_discount, bulk_total


class PricingTest(unittest.TestCase):
    def test_discount_is_a_percentage(self):
        self.assertEqual(apply_discount(80.0, 25), 60.0)

    def test_small_order_has_no_discount(self):
        self.assertEqual(bulk_total(4.0, 3), 12.0)

    def test_ten_units_get_bulk_discount(self):
        self.assertEqual(bulk_total(10.0, 10), 95.0)


if __name__ == "__main__":
    unittest.main()
