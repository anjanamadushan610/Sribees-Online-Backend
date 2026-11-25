from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services import order_gifts


@pytest.mark.asyncio
async def test_gifts_use_discounted_merchandise_total_and_branch_stock(monkeypatch):
    product_id = uuid4()
    branch_id = uuid4()
    product = SimpleNamespace(
        product_id=product_id, name="Gift", display_size=None, display_size_unit=None, sku="GIFT",
    )
    rule = order_gifts.GiftRule(
        enabled=True, minimum=Decimal("1000"),
        items=[order_gifts.GiftItem(product_id=product_id, quantity=2)],
    )
    monkeypatch.setattr(order_gifts, "get_rule", AsyncMock(return_value=rule))
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [(product, 3)])))

    assert await order_gifts.qualify(db, branch_id, Decimal("1100"), Decimal("100.01"), []) == []
    db.execute.assert_not_awaited()

    paid = [{"product_id": str(product_id), "quantity": 1}]
    gifts = await order_gifts.qualify(db, branch_id, Decimal("1100"), Decimal("100"), paid)
    assert [(gift["product_id"], gift["quantity"]) for gift in gifts] == [(str(product_id), 2)]

    paid[0]["quantity"] = 2
    assert await order_gifts.qualify(db, branch_id, Decimal("1100"), Decimal("100"), paid) == []
