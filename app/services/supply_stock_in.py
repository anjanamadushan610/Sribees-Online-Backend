"""
Goods received in Sribees Supply, added to a branch's stock (owner, 2026-11-26).

* Stock is **added**, never set: sales between the delivery and this call stay counted.
* Each Supply receipt line is applied once (``supply_stock_receipts``, keyed by the
  line id and written in the same transaction as the stock).
* The measure follows the product (migration 100): units, grams for loose goods, packs
  of one size for pre-packed goods.
* A branch that does not carry the product gets a **hidden** row: customers see it only
  after staff switch it on.
* A unit product with stock batches (migration 127) receives a batch. One without them
  gets one when staff typed a price different from today's, or when the line carries an
  expiry date (owner 2026-11-27: every delivery is a batch, so its expiry is known and an
  expired batch is never sold); then the stock already there becomes its own "Opening
  stock" batch at the old price, as the admin screen does. Otherwise the count just goes up.
* A new MRP (owner 2026-11-27): old stock keeps selling at its own price; the new batch is
  listed at the new MRP and sells at the same discount as now (MRP 200 sold at 180 -> MRP
  220 sells at 198, rounded down to the rupee, never above the MRP). Staff in Supply
  review that price; ``set_batch_price`` changes it.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import ROUND_FLOOR, Decimal

from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product import (
    BranchInventory,
    InventoryBatch,
    Product,
    ProductVariant,
    SupplyStockReceipt,
)
from app.services import inventory_batches, stock_keeping
from app.services.product_service import ProductService

Money = Field(default=None, ge=0, max_digits=10, decimal_places=2)
MAX_COUNT = 1_000_000_000


class ItemRef(BaseModel):
    product_id: uuid.UUID
    variant_id: uuid.UUID | None = None


class Line(ItemRef):
    line_id: uuid.UUID
    units: int = Field(gt=0, le=1_000_000)
    grams: int | None = Field(default=None, gt=0, le=MAX_COUNT)  # loose goods: what arrived
    purchase_cost: Decimal | None = Money  # per unit, delivered to the store
    listed_price: Decimal | None = Money
    selling_price: Decimal | None = Money
    mrp: Decimal | None = Money  # the MRP printed on this delivery's packs
    expires_on: date | None = None


class _Refused(Exception):
    pass


def same_discount(listed_now: Decimal, selling_now: Decimal, mrp: Decimal) -> Decimal:
    """The new MRP's selling price at today's discount, rounded down to the rupee."""
    if listed_now <= 0 or selling_now >= listed_now:
        return mrp
    price = (mrp * selling_now / listed_now).quantize(Decimal("1"), rounding=ROUND_FLOOR)
    if price <= 0:  # a tiny MRP must never round down to a free batch
        return mrp
    return min(price, mrp).quantize(Decimal("0.01"))


async def _prices(db: AsyncSession, product: Product, row: BranchInventory | None,
                  batches: list) -> tuple[Decimal, Decimal]:
    """(listed, selling) the branch uses for a new delivery: the newest batch's prices,
    or the branch / product price and discount."""
    if batches:
        return batches[-1].listed_price, batches[-1].selling_price
    effective = ProductService.compute_effective_data(product, row)
    listed = Decimal(str(effective["effective_price"])).quantize(Decimal("0.01"))
    selling = effective["effective_discount_price"]
    selling = listed if selling is None else Decimal(str(selling)).quantize(Decimal("0.01"))
    return listed, min(selling, listed)


async def _row(db: AsyncSession, branch_id, product_id, *, lock: bool) -> BranchInventory | None:
    q = select(BranchInventory).where(BranchInventory.branch_id == branch_id,
                                      BranchInventory.product_id == product_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    return await db.scalar(q)


async def info(db: AsyncSession, branch_id: uuid.UUID, items: list[ItemRef]) -> list[dict]:
    out = []
    for ref in items:
        product = await db.get(Product, ref.product_id)
        base = {"product_id": str(ref.product_id),
                "variant_id": str(ref.variant_id) if ref.variant_id else None}
        if product is None:
            out.append({**base, "known": False})
            continue
        row = await _row(db, branch_id, ref.product_id, lock=False)
        batches = list(await inventory_batches.list_batches(db, row.inventory_id)) if row else []
        listed, selling = await _prices(db, product, row, batches)
        out.append({
            **base, "known": True, "carried": row is not None,
            "visible": bool(row and row.is_active),
            "stock_mode": product.stock_mode or "unit",
            "stock_quantity": int(row.stock_quantity or 0) if row else 0,
            "stock_grams": int(row.stock_grams or 0) if row else 0,
            "reserved_quantity": int(row.reserved_quantity or 0) if row else 0,
            "has_batches": bool(batches),
            "listed_price": str(listed), "selling_price": str(selling),
        })
    return out


async def apply(db: AsyncSession, branch_id: uuid.UUID, receipt_no: str, line: Line) -> dict:
    """Add one received line, in its own transaction."""
    done = {"line_id": str(line.line_id)}
    try:
        # Claim the line first: a second call for it waits here, then finds it taken.
        claimed = await db.execute(insert(SupplyStockReceipt).values(
            line_id=line.line_id, receipt_no=receipt_no, branch_id=branch_id,
            product_id=line.product_id, variant_id=line.variant_id, units=line.units,
            grams=line.grams, purchase_cost=line.purchase_cost,
        ).on_conflict_do_nothing().returning(SupplyStockReceipt.line_id))
        if claimed.first() is None:
            await db.rollback()
            # a retry after a lost answer: say which batch it made, so Supply's review is kept
            taken = await db.get(SupplyStockReceipt, line.line_id)
            batch = await db.get(InventoryBatch, taken.batch_id) if taken and taken.batch_id else None
            if batch is None:
                return {**done, "status": "already"}
            return {**done, "status": "already", "batch_id": str(batch.batch_id),
                    "listed_price": str(batch.listed_price),
                    "selling_price": str(batch.selling_price)}
        priced = await _add(db, branch_id, receipt_no, line)
        if priced is not None:
            taken = await db.get(SupplyStockReceipt, line.line_id)
            taken.batch_id = priced["batch_id"]
            priced = {k: str(v) if v is not None else None for k, v in priced.items()}
        await db.commit()
    except _Refused as why:
        await db.rollback()
        logger.warning(f"Supply receipt {receipt_no} line {line.line_id} refused: {why}")
        return {**done, "status": "refused", "reason": str(why)}
    return {**done, "status": "added", **(priced or {})}


async def _add(db: AsyncSession, branch_id, receipt_no: str, line: Line) -> dict | None:
    """None, or the new batch: {"batch_id", "listed_price", "selling_price",
    "old_listed_price", "old_selling_price"}."""
    product = await db.get(Product, line.product_id)
    if product is None:
        raise _Refused("this product is not in Online")
    variant = None
    if line.variant_id is not None:
        variant = await db.get(ProductVariant, line.variant_id)
        if variant is None or variant.product_id != line.product_id:
            raise _Refused("that pack size is not one of this product")
    await db.execute(insert(BranchInventory).values(
        inventory_id=uuid.uuid4(), product_id=line.product_id, branch_id=branch_id,
        stock_quantity=0, is_active=False,
    ).on_conflict_do_nothing(index_elements=["product_id", "branch_id"]))
    mode = product.stock_mode or "unit"
    # Same lock order as checkout (stock_keeping): a pre-packed product's size row first,
    # its branch row only when the summary is written. Locking it here would deadlock.
    row = await _row(db, branch_id, line.product_id, lock=mode != "packed")

    if mode == "loose":
        grams = line.grams or (variant.weight_grams * line.units
                               if variant is not None and variant.weight_grams else None)
        if not grams:
            raise _Refused("kept by weight: the kilograms received are needed")
        row.stock_grams = _plus(row.stock_grams, grams)
        await db.flush()
        await stock_keeping.refresh_summary(db, branch_id, line.product_id)
        return None
    if mode == "packed":
        if variant is None:
            raise _Refused("kept per pack size: choose which size arrived")
        item = stock_keeping.Item("packed", line.product_id, line.variant_id, variant.weight_grams)
        count = await stock_keeping._row(db, branch_id, item, lock=True)
        count.stock_quantity = _plus(count.stock_quantity, line.units)
        await db.flush()
        await stock_keeping.refresh_summary(db, branch_id, line.product_id)
        return None

    batches = list(await inventory_batches.list_batches(db, row.inventory_id))
    listed_now, selling_now = await _prices(db, product, row, batches)
    listed = line.listed_price if line.listed_price is not None else listed_now
    selling = line.selling_price if line.selling_price is not None else selling_now
    if line.mrp is not None and line.listed_price is None and line.mrp != listed_now:
        listed = line.mrp  # a new MRP: same discount as today unless staff typed a price
        if line.selling_price is None:
            selling = same_discount(listed_now, selling_now, line.mrp)
    repriced = (listed, selling) != (listed_now, selling_now)
    if selling > listed:
        raise _Refused("the selling price is above the listed price")
    dated = line.expires_on is not None and not product.sold_by_weight
    if not batches and ((not repriced and not dated) or product.sold_by_weight):
        if repriced:
            raise _Refused("this product cannot take a new price here; change it in Online")
        row.stock_quantity = _plus(row.stock_quantity, line.units)
        return None
    if not batches and row.stock_quantity:
        db.add(InventoryBatch(
            inventory_id=row.inventory_id, lot_code="Opening stock",
            quantity_received=row.stock_quantity, remaining=row.stock_quantity,
            listed_price=listed_now, selling_price=selling_now))
        await db.flush()
    _plus(row.stock_quantity, line.units)
    batch = await inventory_batches.receive(
        db, row.inventory_id, quantity=line.units, purchase_cost=line.purchase_cost,
        listed_price=listed, selling_price=selling, lot_code=receipt_no[:80],
        expires_on=line.expires_on)
    return {"batch_id": batch.batch_id, "listed_price": listed, "selling_price": selling,
            "old_listed_price": listed_now, "old_selling_price": selling_now}


async def set_batch_price(db: AsyncSession, batch_id: uuid.UUID, selling_price: Decimal,
                          *, branch_id: uuid.UUID) -> dict:
    """A branch manager's reviewed price for a batch Supply delivered (P6): its unsold units
    sell at it. Never above the batch's listed price (MRP)."""
    batch = await db.get(InventoryBatch, batch_id)
    if batch is None:
        raise LookupError("batch not found")
    row = await db.get(BranchInventory, batch.inventory_id, with_for_update=True)
    batch = await db.get(InventoryBatch, batch_id, with_for_update=True, populate_existing=True)
    if row is None or row.branch_id != branch_id:
        raise LookupError("batch not found")
    if not (Decimal("0") < selling_price <= batch.listed_price):
        raise ValueError(f"the selling price must be above 0 and not above the MRP "
                         f"({batch.listed_price})")
    before = batch.selling_price
    batch.selling_price = selling_price
    await db.commit()
    logger.info(f"Batch {batch.lot_code or batch.sequence}: selling price {before} -> "
                f"{selling_price} (Supply review)")
    return {"batch_id": str(batch.batch_id), "listed_price": str(batch.listed_price),
            "selling_price": str(batch.selling_price), "remaining": int(batch.remaining)}


def _plus(have, more: int) -> int:
    total = int(have or 0) + more
    if total > MAX_COUNT:
        raise _Refused("the stock figure would be too large")
    return total
