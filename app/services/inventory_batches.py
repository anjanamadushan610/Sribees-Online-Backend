"""FIFO receipts for fixed-unit products, serialized by the branch inventory row."""
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select

from app.models.product import BranchInventory, InventoryBatch


async def list_batches(db, inventory_id):
    result = await db.scalars(
        select(InventoryBatch).where(InventoryBatch.inventory_id == inventory_id)
        .order_by(InventoryBatch.sequence)
    )
    return result.all()


async def has_batches(db, inventory_id):
    return await db.scalar(select(InventoryBatch.batch_id).where(
        InventoryBatch.inventory_id == inventory_id,
    ).limit(1)) is not None



async def product_has_batches(db, product_id):
    return await db.scalar(
        select(InventoryBatch.batch_id)
        .join(BranchInventory, BranchInventory.inventory_id == InventoryBatch.inventory_id)
        .where(BranchInventory.product_id == product_id).limit(1)
    ) is not None


def allocate(batches, stock, quantity, used):
    """Plan FIFO allocations, counting unbatched legacy stock first."""
    tracked = sum(batch.remaining for batch in batches)
    legacy = max(0, stock - tracked)
    parts = []
    for batch, have in [(None, legacy), *((batch, batch.remaining) for batch in batches)]:
        key = batch.batch_id if batch else None
        take = min(quantity, max(0, have - used.get(key, 0)))
        if take:
            parts.append((batch, take))
            used[key] = used.get(key, 0) + take
            quantity -= take
        if quantity == 0:
            break
    return parts, quantity


async def price_lines(db, branch_id, items, *, lock=False, strict=True):
    """Use the same FIFO plan at quote time and again under lock before charging."""
    if not branch_id or not items:
        return items
    product_ids = set()
    for line in items:
        if not line.get("variant_name") and line.get("product_id"):
            try:
                product_ids.add(UUID(str(line["product_id"])))
            except ValueError:
                continue
    if not product_ids:
        return items
    query = select(BranchInventory).where(
        BranchInventory.branch_id == branch_id,
        BranchInventory.product_id.in_(product_ids),
    ).order_by(BranchInventory.inventory_id)
    if not lock:
        query = query.where(select(InventoryBatch.batch_id).where(
            InventoryBatch.inventory_id == BranchInventory.inventory_id,
        ).exists())
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    rows = (await db.scalars(query)).all()
    queues = {}
    for row in rows:
        batches = await list_batches(db, row.inventory_id)
        if batches:
            queues[str(row.product_id)] = (row.stock_quantity, batches, {})
    priced = []
    for line in items:
        queue = queues.get(str(line["product_id"])) if not line.get("variant_name") else None
        if queue is None:
            priced.append(line)
            continue
        stock, batches, used = queue
        quantity = int(line["quantity"])
        if quantity <= 0:
            raise ValueError("Order quantities must be positive.")
        parts, short = allocate(batches, stock, quantity, used)
        if short and not strict:
            priced.append(line)
            continue
        if short:
            raise ValueError(f"Stock changed for {line.get('name') or 'this product'}. Refresh your cart.")
        for batch, quantity in parts:
            split = {**line, "quantity": quantity}
            if batch:
                split.update(inventory_batch_id=batch.batch_id, price=batch.selling_price,
                             original_price=batch.listed_price,
                             batch_label=batch.lot_code or f"Batch {batch.sequence}")
            priced.append(split)
    return priced


async def receive(db, inventory_id, *, quantity: int, purchase_cost: Decimal | None,
                  listed_price: Decimal, selling_price: Decimal,
                  lot_code: str | None, expires_on: date | None):
    inventory = await db.get(BranchInventory, inventory_id, with_for_update=True)
    batch = InventoryBatch(
        inventory_id=inventory_id, quantity_received=quantity, remaining=quantity,
        purchase_cost=purchase_cost, listed_price=listed_price, selling_price=selling_price,
        lot_code=lot_code, expires_on=expires_on,
    )
    inventory.stock_quantity += quantity
    db.add(batch)
    await db.flush()
    return batch


async def catalog_prices(db, inventory_ids):
    """Opening unit prices for the storefront, while checkout prices every unit."""
    if not inventory_ids:
        return {}
    from sqlalchemy import func

    count = select(func.sum(InventoryBatch.remaining)).where(
        InventoryBatch.inventory_id == BranchInventory.inventory_id,
    ).correlate(BranchInventory).scalar_subquery()
    rows = await db.execute(select(
        InventoryBatch.inventory_id, InventoryBatch.listed_price, InventoryBatch.selling_price,
    ).join(BranchInventory, BranchInventory.inventory_id == InventoryBatch.inventory_id)
        .where(InventoryBatch.inventory_id.in_(inventory_ids), InventoryBatch.remaining > 0,
               BranchInventory.stock_quantity <= count)
        .distinct(InventoryBatch.inventory_id)
        .order_by(InventoryBatch.inventory_id, InventoryBatch.sequence))
    return {inventory_id: (listed, selling) for inventory_id, listed, selling in rows}


def apply_catalog_price(effective, prices):
    prices_for_row = prices.get(effective.get("inventory_id"))
    if prices_for_row is None:
        return effective
    listed, selling = prices_for_row
    return {
        **effective,
        "effective_price": float(listed),
        "effective_discount_price": float(selling) if listed > selling else None,
        "effective_discount": float((listed - selling) * 100 / listed) if listed else 0,
        "is_on_sale": listed > selling,
    }


def quote_lines(items):
    if not any(line.get("inventory_batch_id") for line in items):
        return None
    return [
        {
            "product_id": str(line["product_id"]),
            "name": line.get("name") or line.get("product_name") or "",
            "quantity": int(line["quantity"]),
            "price": float(line["price"]),
            "original_price": float(line.get("original_price", line["price"])),
            "display_size": line.get("display_size"),
            "display_size_unit": line.get("display_size_unit"),
            "variant_name": line.get("variant_name"),
        }
        for line in items
    ]
