"""Cart arithmetic."""

import warnings


def compute_total(items):
    """Sum price * quantity for (price, quantity) pairs, rounded to cents."""
    return round(sum(price * quantity for price, quantity in items), 2)


def calc_total(items):
    """Deprecated alias for compute_total."""
    warnings.warn("calc_total is deprecated; use compute_total", DeprecationWarning, stacklevel=2)
    return compute_total(items)
