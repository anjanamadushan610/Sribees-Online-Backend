"""
One rule for a branch's stock, whatever kind of product (migration 100).

* ``unit``   -- ``branch_inventory.stock_quantity`` counts units; an order line
                of N takes N (the rule the shop always had).
* ``loose``  -- ``branch_inventory.stock_grams`` holds grams; a weight option
                of G grams ordered N times takes N x G.
* ``packed`` -- each weight option has its own count in ``branch_variant_inventory``.

For loose and packed goods ``branch_inventory.stock_quantity`` is kept as the
figure every screen, filter and the app already read: packs of the smallest
weight option (loose), or all packs together (packed). So "in stock", list
filters and the app's limits stay right without a new app release.

Cart checks, checkout, a cancel putting goods back and Sribees Finance's stock
pushes all go through here, so they can never disagree. Checkout locks the row
(``FOR UPDATE``): two checkouts racing for the last pack cannot both get it.

A product a branch has no ``branch_inventory`` row for is not tracked there
(``available`` answers None), as before.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product import (
    BranchInventory,
    BranchVariantInventory,
    Product,
    ProductVariant,
)


class OutOfStock(Exception):
    def __init__(self, left: int):
        super().__init__(f"only {left} available")
        self.left = left


@dataclass
class Item:
    mode: str
    product_id: UUID
    variant_id: Optional[UUID]
    weight_grams: Optional[int]


def _uuid(v) -> Optional[UUID]:
    return v if isinstance(v, UUID) or v is None else UUID(str(v))


async def item(db: AsyncSession, product_id, variant_id=None) -> Item:
    product_id, variant_id = _uuid(product_id), _uuid(variant_id)
    mode = (await db.execute(select(Product.stock_mode).where(Product.product_id == product_id))
            ).scalar_one_or_none() or "unit"
    grams = None
    if variant_id is not None:
        grams = (await db.execute(select(ProductVariant.weight_grams)
                                  .where(ProductVariant.variant_id == variant_id))).scalar_one_or_none()
    if mode != "unit" and not grams:
        mode = "unit"  # a non-weight option of a weight product: counted as units
    return Item(mode, product_id, variant_id, grams)


async def _row(db: AsyncSession, branch_id, it: Item, *, lock: bool):
    if it.mode == "packed":
        if not await _carried(db, branch_id, it.product_id):
            return None
        q = select(BranchVariantInventory).where(BranchVariantInventory.branch_id == branch_id,
                                                 BranchVariantInventory.variant_id == it.variant_id)
        row = (await db.execute(q.with_for_update() if lock else q)).scalars().first()
        if row is None and not lock:
            # A read: a size never counted has none. Only a write creates the row.
            return BranchVariantInventory(branch_id=branch_id, variant_id=it.variant_id, stock_quantity=0)
        if row is None:
            await db.execute(insert(BranchVariantInventory).values(
                branch_id=branch_id, variant_id=it.variant_id, stock_quantity=0
            ).on_conflict_do_nothing())
            row = (await db.execute(q.with_for_update() if lock else q)).scalars().first()
        return row
    q = select(BranchInventory).where(BranchInventory.branch_id == branch_id,
                                      BranchInventory.product_id == it.product_id)
    return (await db.execute(q.with_for_update() if lock else q)).scalars().first()


async def _carried(db: AsyncSession, branch_id, product_id) -> bool:
    return (await db.execute(select(BranchInventory.inventory_id).where(
        BranchInventory.branch_id == branch_id, BranchInventory.product_id == product_id))
    ).first() is not None


def per_line(it: Item) -> int:
    """How much of the row one ordered unit of this option uses."""
    return it.weight_grams if it.mode == "loose" else 1


def _get(row, it: Item) -> int:
    return int((row.stock_grams if it.mode == "loose" else row.stock_quantity) or 0)


async def _put(db: AsyncSession, branch_id, row, it: Item, value: int) -> None:
    if it.mode == "loose":
        row.stock_grams = value
    else:
        row.stock_quantity = value
    if it.mode != "unit":
        await refresh_summary(db, branch_id, it.product_id)


async def refresh_summary(db: AsyncSession, branch_id, product_id) -> None:
    """branch_inventory.stock_quantity as screens read it: packs of the smallest active
    weight option (loose), or all packs of the active sizes together (packed)."""
    inv = (await db.execute(select(BranchInventory).where(
        BranchInventory.branch_id == branch_id, BranchInventory.product_id == product_id))).scalars().first()
    if inv is None:
        return
    mode = (await db.execute(select(Product.stock_mode).where(Product.product_id == product_id))
            ).scalar_one_or_none()
    if mode == "loose":
        smallest = (await db.execute(select(func.min(ProductVariant.weight_grams)).where(
            ProductVariant.product_id == product_id, ProductVariant.is_active.is_(True),
            ProductVariant.weight_grams.is_not(None)))).scalar_one_or_none()
        inv.stock_quantity = int(inv.stock_grams or 0) // smallest if smallest else 0
    elif mode == "packed":
        await db.flush()
        inv.stock_quantity = int((await db.execute(
            select(func.coalesce(func.sum(BranchVariantInventory.stock_quantity), 0))
            .join(ProductVariant, ProductVariant.variant_id == BranchVariantInventory.variant_id)
            .where(BranchVariantInventory.branch_id == branch_id,
                   ProductVariant.product_id == product_id,
                   # a pack size no longer offered cannot be bought: not "in stock"
                   ProductVariant.is_active.is_not(False)))).scalar_one())


async def available(db: AsyncSession, branch_id, product_id, variant_id=None) -> Optional[int]:
    """How many of this option the branch can still sell; None = not tracked there."""
    it = await item(db, product_id, variant_id)
    row = await _row(db, _uuid(branch_id), it, lock=False)
    if row is None:
        return None
    return max(_get(row, it), 0) // per_line(it)


async def take(db: AsyncSession, branch_id, product_id, variant_id, quantity: int) -> None:
    """Take an order line off the shelf, under a row lock. Raises OutOfStock(left)."""
    branch_id = _uuid(branch_id)
    it = await item(db, product_id, variant_id)
    row = await _row(db, branch_id, it, lock=True)
    if row is None:
        return
    need = quantity * per_line(it)
    have = _get(row, it)
    if have < need:
        raise OutOfStock(max(have, 0) // per_line(it))
    await _put(db, branch_id, row, it, have - need)


async def give_back(db: AsyncSession, branch_id, product_id, variant_id, quantity: int) -> None:
    """A cancelled line's goods back on the shelf."""
    branch_id = _uuid(branch_id)
    it = await item(db, product_id, variant_id)
    row = await _row(db, branch_id, it, lock=True)
    if row is not None:
        await _put(db, branch_id, row, it, _get(row, it) + quantity * per_line(it))
        if it.mode == "unit" and variant_id is None:
            # A line without a size, placed before the product became loose or
            # pre-packed, lands on the product's own row -- for those modes only
            # the screens' summary, worked out from the sizes. Put it back to that.
            await refresh_summary(db, branch_id, it.product_id)


async def lock_row(db: AsyncSession, branch_id, it: Item) -> bool:
    """Take checkout's row lock on the item's stock row (until the transaction ends).
    False if the branch does not carry the product."""
    return await _row(db, _uuid(branch_id), it, lock=True) is not None


async def set_level(db: AsyncSession, branch_id, it: Item, value: int) -> bool:
    """Set a row to an absolute figure in its own measure -- grams for a loose product,
    packs for a pack size, units otherwise (Finance's push). False if the branch does
    not carry the product."""
    branch_id = _uuid(branch_id)
    row = await _row(db, branch_id, it, lock=True)
    if row is None:
        return False
    await _put(db, branch_id, row, it, max(int(value), 0))
    return True
