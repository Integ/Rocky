"""Checkout flow."""

from shop.cart import compute_total

TAX_RATE = 0.08


def checkout(items):
    subtotal = compute_total(items)
    return {"subtotal": subtotal, "tax": round(subtotal * TAX_RATE, 2)}
