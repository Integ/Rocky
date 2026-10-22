"""Warehouse inventory helpers."""

import csv
from dataclasses import dataclass


@dataclass(frozen=True)
class Item:
    sku: str
    name: str
    quantity: int
    reorder_level: int


def load_items(path):
    """Read items from a CSV file with sku,name,quantity,reorder_level columns."""
    with open(path, newline="", encoding="utf-8") as handle:
        return [
            Item(
                sku=row["sku"],
                name=row["name"],
                quantity=int(row["quantity"]),
                reorder_level=int(row["reorder_level"]),
            )
            for row in csv.DictReader(handle)
        ]


def restock_report(items, *, multiplier=2):
    """Return (sku, order_quantity) for items at or below their reorder level."""
    report = [
        (item.sku, item.reorder_level * multiplier - item.quantity)
        for item in items
        if item.quantity <= item.reorder_level
    ]
    return sorted(report, key=lambda entry: (-entry[1], entry[0]))
