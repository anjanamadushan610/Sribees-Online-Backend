"""
A return refunds what the customer paid for the goods that came back: the
unit price less that line's share of the coupon. Worked by hand beside each.
"""
from decimal import Decimal
from types import SimpleNamespace

from app.services.order_service import OrderService

D = Decimal


def _item(item_id, price, qty, coupon="0"):
    return SimpleNamespace(order_item_id=item_id, unit_price=D(price), quantity=qty,
                           subtotal=D(price) * qty, coupon_discount=D(coupon))


def _order(*items, discount="0", returns=None):
    return SimpleNamespace(items=list(items), discount_amount=D(discount), return_items=returns)


RICE = lambda: _item("rice", "1000.00", 2, coupon="200.00")   # noqa: E731
SALT = lambda: _item("salt", "500.00", 1, coupon="50.00")     # noqa: E731
TEA = lambda: _item("tea", "200.00", 1, coupon="0.00")        # noqa: E731  (coupon did not cover tea)


def test_a_whole_return_refunds_subtotal_less_discount():
    # 2000 + 500 + 200 - (200 + 50) = 2450
    order = _order(RICE(), SALT(), TEA(), discount="250.00")
    assert OrderService._compute_return_refund(order) == D("2450.00")


def test_one_unit_back_refunds_its_share_of_the_coupon_too():
    # One bag of rice: 1000 - 200/2 = 900
    order = _order(RICE(), SALT(), TEA(), discount="250.00",
                   returns=[{"order_item_id": "rice", "quantity": 1}])
    assert OrderService._compute_return_refund(order) == D("900.00")


def test_a_line_the_coupon_did_not_cover_refunds_in_full():
    order = _order(RICE(), TEA(), discount="200.00", returns=[{"order_item_id": "tea", "quantity": 1}])
    assert OrderService._compute_return_refund(order) == D("200.00")


def test_an_order_from_before_line_shares_spreads_the_discount_by_value():
    # 2700 of goods, 270 off, no per-line shares: rice keeps 2000/2700 of it.
    # One bag back: 1000 - 270 * (1000/2700) = 1000 - 100 = 900
    rice = _item("rice", "1000.00", 2)
    order = _order(rice, _item("salt", "500.00", 1), _item("tea", "200.00", 1), discount="270.00",
                   returns=[{"order_item_id": "rice", "quantity": 1}])
    assert OrderService._compute_return_refund(order) == D("900.00")


def test_without_a_coupon_nothing_changes():
    order = _order(_item("a", "1150.00", 2), returns=[{"order_item_id": "a", "quantity": 1}])
    assert OrderService._compute_return_refund(order) == D("1150.00")


def test_old_commission_rows_still_reduce_on_shelf_value():
    order = _order(RICE(), SALT(), discount="250.00", returns=[{"order_item_id": "rice", "quantity": 1}])
    assert OrderService._returned_gross(order) == D("1000.00")
