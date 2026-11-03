"""
Partial delivery: a COD customer took some items and refused the rest.

SribeesExpress has no item list of its own; it reports the delivery as
`delivered` with the COD the rider actually collected. Less than the order's
total means a partial delivery, and the shortfall is the value of what came
back. When the goods are back at the shop, staff record which items they were:

* the stock goes back on the shelf (app/services/stock_keeping.py);
* Sribees Finance gets an ``order.undelivered`` event and takes the goods back
  into stock and those sales off its books.

The customer paid nothing for the refused items, so there is no refund.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order, OrderStatus


class PartialDeliveryError(ValueError):
    pass


def is_cod(order: Order) -> bool:
    return (order.payment_method or "").upper() == "COD"


def cod_shortfall(order: Order) -> Optional[Decimal]:
    """What the rider did not collect on a delivered COD order, or None when the
    collection is not known yet or it is not a COD order."""
    if not is_cod(order) or order.courier_cod_collected_amount is None:
        return None
    short = Decimal(order.total_amount or 0) - Decimal(order.courier_cod_collected_amount)
    return short if short > 0 else Decimal("0")


async def record(db: AsyncSession, order: Order, items: list[dict]) -> Order:
    """Record the refused items of a partially delivered COD order, once."""
    from app.services import finance_outbox, stock_keeping

    if order.status != OrderStatus.DELIVERED.value:
        raise PartialDeliveryError("Only a delivered order can have items that came back undelivered.")
    short = cod_shortfall(order)
    if not short:
        raise PartialDeliveryError(
            "SribeesExpress collected the whole amount for this order; nothing came back undelivered.")
    if order.undelivered_recorded_at is not None:
        raise PartialDeliveryError("The undelivered items of this order are already recorded.")

    lines = {str(i.order_item_id): i for i in order.items}
    clean: list[dict] = []
    for want in items:
        line = lines.get(str(want.get("order_item_id")))
        qty = int(want.get("quantity") or 0)
        if line is None:
            raise PartialDeliveryError("That item is not on this order.")
        if qty <= 0 or qty > int(line.quantity):
            raise PartialDeliveryError(f"{line.product_name}: between 1 and {int(line.quantity)} came back.")
        clean.append({"order_item_id": str(line.order_item_id), "quantity": qty})
    if not clean:
        raise PartialDeliveryError("Choose the items that came back.")

    if order.branch_id is not None:
        for c in clean:
            line = lines[c["order_item_id"]]
            await stock_keeping.give_back(db, order.branch_id, line.product_id, line.variant_id, c["quantity"])
    order.undelivered_items = clean
    order.undelivered_recorded_at = datetime.now(timezone.utc)
    await finance_outbox.order_undelivered(db, order)
    await db.commit()
    await db.refresh(order)
    return order
