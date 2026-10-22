import unittest

from inventory import Item, restock_report


class HiddenRestockTest(unittest.TestCase):
    def test_orders_items_at_or_below_reorder_level(self):
        items = [
            Item("X-1", "a", 2, 5),
            Item("X-2", "b", 5, 5),
            Item("X-3", "c", 6, 5),
            Item("X-0", "d", 2, 5),
        ]
        self.assertEqual(restock_report(items), [("X-0", 8), ("X-1", 8), ("X-2", 5)])

    def test_multiplier_is_keyword_only(self):
        items = [Item("Y-1", "a", 1, 4)]
        self.assertEqual(restock_report(items, multiplier=3), [("Y-1", 11)])
        with self.assertRaises(TypeError):
            restock_report(items, 3)

    def test_empty_input(self):
        self.assertEqual(restock_report([]), [])


if __name__ == "__main__":
    unittest.main()
