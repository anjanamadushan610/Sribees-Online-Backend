"""
Products marked non-returnable stay with the customer.

The flag the return path reads is the order item's copy, taken at checkout,
so these tests build orders out of items directly.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.order import OrderStatus
from app.schemas.order import ReturnItemSchema
from app.schemas.product import ProductUpdate
from app.services.order_service import OrderService
from app.services.product_service import ProductService


class _FakeSession:
    def __init__(self) -> None:
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        pass

    async def refresh(self, _obj) -> None:
        pass


def _item(item_id: str, name: str, returnable: bool, qty: int = 1, price: str = "100.00"):
    return SimpleNamespace(
        order_item_id=item_id,
        product_name=name,
        quantity=qty,
        unit_price=Decimal(price),
        subtotal=Decimal(price) * qty,
        is_returnable=returnable,
    )


def _order(*items):
    return SimpleNamespace(
        order_id="11111111-1111-1111-1111-111111111111",
        order_number="SB-0001",
        status=OrderStatus.DELIVERED.value,
        delivered_at=datetime.now(timezone.utc) - timedelta(hours=1),
        items=list(items),
        return_items=None,
        return_reason=None,
        return_comments=None,
        return_images=None,
        return_requested_at=None,
    )


SHAMPOO = _item("a", "Sunsilk Shampoo", returnable=True, qty=2, price="1150.00")
STRAWBERRIES = _item("b", "Strawberries (200 g)", returnable=False, price="690.00")


async def _request(order, items=None):
    return await OrderService.request_return(
        _FakeSession(), order, "Damaged", items=items, images=["/uploads/returns/x/1.jpg"]
    )


async def test_naming_a_non_returnable_item_is_refused_by_name():
    order = _order(SHAMPOO, STRAWBERRIES)
    with pytest.raises(ValueError, match="Strawberries"):
        await _request(order, [ReturnItemSchema(order_item_id="b", quantity=1)])
    assert order.status == OrderStatus.DELIVERED.value


async def test_returnable_items_still_return():
    order = _order(SHAMPOO, STRAWBERRIES)
    await _request(order, [ReturnItemSchema(order_item_id="a", quantity=1)])
    assert order.status == OrderStatus.RETURN_REQUESTED.value
    assert order.return_items == [{"order_item_id": "a", "quantity": 1}]


async def test_whole_order_return_leaves_non_returnable_items_out_of_the_refund():
    order = _order(SHAMPOO, STRAWBERRIES)
    await _request(order)
    assert order.return_items == [{"order_item_id": "a", "quantity": 2}]
    assert OrderService._compute_return_refund(order) == Decimal("2300.00")


async def test_order_of_only_non_returnable_items_cannot_be_returned():
    order = _order(STRAWBERRIES)
    with pytest.raises(ValueError, match="None of the items"):
        await _request(order)


async def test_fully_returnable_order_keeps_the_whole_order_meaning():
    order = _order(SHAMPOO)
    await _request(order)
    assert order.return_items is None


async def test_explicit_null_does_not_clear_the_flag():
    # The column is NOT NULL: a client sending null means "not changing it".
    product = SimpleNamespace(is_returnable=False, name="Strawberries")
    await ProductService.update(_FakeSession(), product, ProductUpdate(is_returnable=None, name="Strawberries (200 g)"))
    assert product.is_returnable is False
    assert product.name == "Strawberries (200 g)"


def test_the_flag_is_mapped_on_the_order_item_not_the_order():
    # The fakes above can't catch a mis-mapped column. This did happen: the
    # column landed on Order, OrderItem(is_returnable=...) raised TypeError,
    # and checkout broke while every test here passed.
    from app.models.order import Order, OrderItem

    assert "is_returnable" in OrderItem.__table__.columns
    assert "is_returnable" not in Order.__table__.columns
    assert OrderItem(is_returnable=False).is_returnable is False
