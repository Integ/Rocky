import unittest
from pathlib import Path

from inventory import Item, load_items, restock_report


class LoadItemsTest(unittest.TestCase):
    def test_loads_all_rows_with_integer_fields(self):
        items = load_items(Path(__file__).with_name("items.csv"))
        self.assertEqual(len(items), 8)
        self.assertEqual(items[0], Item("A-100", "Packing tape", 4, 10))


class RestockReportTest(unittest.TestCase):
    def test_sorted_by_quantity_then_sku(self):
        items = [Item("B", "b", 1, 3), Item("A", "a", 1, 3), Item("C", "c", 9, 3)]
        self.assertEqual(restock_report(items), [("A", 5), ("B", 5)])


if __name__ == "__main__":
    unittest.main()
