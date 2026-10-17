import pytest

from pricing import order_total, shipping_cost


def test_standard_shipping_cost():
    assert shipping_cost("standard") == 5


def test_express_shipping_cost():
    assert shipping_cost("express") == 15


def test_shipping_is_added_to_total():
    assert order_total(50, "express") == 65


def test_unknown_shipping_method_is_rejected():
    with pytest.raises(KeyError):
        shipping_cost("teleport")
