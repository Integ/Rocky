"""Printable receipts."""

from shop import cart


def receipt_lines(items):
    lines = [f"{quantity} x {price:.2f}" for price, quantity in items]
    lines.append(f"TOTAL {cart.compute_total(items):.2f}")
    return lines
