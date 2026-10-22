"""Cart arithmetic."""


def calc_total(items):
    """Sum price * quantity for (price, quantity) pairs, rounded to cents."""
    return round(sum(price * quantity for price, quantity in items), 2)
