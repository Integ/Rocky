import unittest

from pricing import apply_discount, bulk_total


class HiddenPricingTest(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(apply_discount(19.99, 0), 19.99)
        self.assertEqual(apply_discount(19.99, 100), 0.0)
        self.assertEqual(apply_discount(10.0, 33), 6.7)

    def test_invalid_percent_raises(self):
        for percent in (-1, 100.5, 250):
            with self.assertRaises(ValueError):
                apply_discount(10.0, percent)

    def test_bulk_threshold(self):
        self.assertEqual(bulk_total(2.5, 9), 22.5)
        self.assertEqual(bulk_total(2.5, 10), 23.75)
        self.assertEqual(bulk_total(1.99, 12), 22.69)


if __name__ == "__main__":
    unittest.main()
