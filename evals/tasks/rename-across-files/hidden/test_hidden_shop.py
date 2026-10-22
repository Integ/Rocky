import unittest
import warnings

from shop import compute_total
from shop.cart import calc_total
from shop.checkout import checkout
from shop.receipts import receipt_lines

ITEMS = [(2.5, 2), (1.25, 4)]


class HiddenShopTest(unittest.TestCase):
    def test_new_name_is_exported(self):
        self.assertEqual(compute_total(ITEMS), 10.0)

    def test_old_name_warns_and_matches(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.assertEqual(calc_total(ITEMS), 10.0)
        self.assertTrue(any(issubclass(item.category, DeprecationWarning) for item in caught))

    def test_call_sites_do_not_warn(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            self.assertEqual(checkout(ITEMS), {"subtotal": 10.0, "tax": 0.8})
            self.assertEqual(receipt_lines(ITEMS)[-1], "TOTAL 10.00")


if __name__ == "__main__":
    unittest.main()
