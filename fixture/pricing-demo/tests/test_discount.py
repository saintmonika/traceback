from pricing import apply_discount, order_total


def test_small_order_gets_no_discount():
    assert apply_discount(50) == 50


def test_large_order_gets_ten_percent_off():
    assert apply_discount(150) == 135


def test_very_large_order_gets_ten_percent_off():
    assert apply_discount(200) == 180


def test_order_total_includes_discount_and_shipping():
    assert order_total(150) == 140
