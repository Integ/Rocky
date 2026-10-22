import unittest
from pathlib import Path

from inventory import Item, load_items


class LoadItemsTest(unittest.TestCase):
    def test_loads_all_rows_with_integer_fields(self):
        items = load_items(Path(__file__).with_name("items.csv"))
        self.assertEqual(len(items), 8)
        self.assertEqual(items[0], Item("A-100", "Packing tape", 4, 10))


if __name__ == "__main__":
    unittest.main()
