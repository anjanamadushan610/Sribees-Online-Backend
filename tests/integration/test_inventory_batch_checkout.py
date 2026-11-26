from decimal import Decimal
from uuid import UUID

from sqlalchemy import select

from app.models.order import OrderItem
from app.models.product import BranchInventory
from app.services import inventory_batches
from tests.integration.test_checkout_journey import _order_body, _set_branch


async def test_mixed_batch_checkout_and_cancellation(api, db, app_redis, world, make_customer,
                                                     make_product, fake_courier):
    customer = await make_customer()
    product = await make_product(price="420.00", stock=0)
    await _set_branch(app_redis, customer, world)
    inventory_id = await db.scalar(select(BranchInventory.inventory_id).where(
        BranchInventory.product_id == product, BranchInventory.branch_id == world.branch_id,
    ))
    older = await inventory_batches.receive(
        db, inventory_id, quantity=50, lot_code="older", expires_on=None,
        purchase_cost=Decimal("380"), listed_price=Decimal("450"), selling_price=Decimal("420"),
    )
    newer = await inventory_batches.receive(
        db, inventory_id, quantity=50, lot_code="newer", expires_on=None,
        purchase_cost=Decimal("400"), listed_price=Decimal("470"), selling_price=Decimal("440"),
    )
    await db.commit()
    older_id, newer_id = older.batch_id, newer.batch_id

    items = [{"product_id": str(product), "name": "Crackers", "quantity": 51, "price": 1}]
    quote = await api.post("/api/v1/orders/quote", json={
        "payment_method": "COD", "delivery_address_id": str(customer.address_id), "items": items,
    }, headers=customer.headers)
    assert quote.status_code == 200, quote.text
    data = quote.json()["data"]
    assert data["subtotal"] == 21440
    assert [(row["quantity"], row["price"]) for row in data["priced_lines"]] == [(50, 420), (1, 440)]

    placed = await api.post("/api/v1/orders", json=_order_body(
        customer, is_buy_now=True, items=items, expected_subtotal=21440,
    ), headers=customer.headers)
    assert placed.status_code == 201, placed.text
    order_id = UUID(placed.json()["data"]["order_id"])
    lines = (await db.scalars(select(OrderItem).where(OrderItem.order_id == order_id))).all()
    assert {line.inventory_batch_id: (line.quantity, line.unit_price) for line in lines} == {
        older_id: (50, Decimal("420")), newer_id: (1, Decimal("440")),
    }
    db.expire_all()
    assert [batch.remaining for batch in await inventory_batches.list_batches(db, inventory_id)] == [0, 49]

    cancelled = await api.post(f"/api/v1/orders/{order_id}/cancel?reason=stock+test", headers=customer.headers)
    assert cancelled.status_code == 200, cancelled.text
    db.expire_all()
    assert [batch.remaining for batch in await inventory_batches.list_batches(db, inventory_id)] == [50, 50]
