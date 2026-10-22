"""Checkout flow."""

from shop.cart import calc_total

TAX_RATE = 0.08


def checkout(items):
    subtotal = calc_total(items)
    return {"subtotal": subtotal, "tax": round(subtotal * TAX_RATE, 2)}
