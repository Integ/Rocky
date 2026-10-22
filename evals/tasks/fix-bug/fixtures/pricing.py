"""Price calculations used by the checkout service."""


def apply_discount(price, percent):
    """Return price reduced by percent (0-100), rounded to cents."""
    return round(price - price * percent, 2)


def bulk_total(unit_price, quantity):
    """Total for quantity units; orders of 10 or more units get 5% off."""
    total = unit_price * quantity
    if quantity > 10:
        total = apply_discount(total, 5)
    return round(total, 2)
