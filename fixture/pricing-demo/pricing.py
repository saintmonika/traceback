"""A small example pricing module: order totals with a discount and shipping."""

SHIPPING_RATES = {"standard": 5, "express": 15}


def apply_discount(subtotal):
    """Return the subtotal after any discount."""
    if subtotal > 100:
        return round(subtotal * 0.90, 2)
    return subtotal


def shipping_cost(method="standard"):
    """Return the shipping cost for a shipping method."""
    return SHIPPING_RATES[method]


def order_total(subtotal, shipping_method="standard"):
    """Return the discounted subtotal plus shipping."""
    return apply_discount(subtotal) + shipping_cost(shipping_method)
