"""
A branch's fulfilment partner (Sribees Supply, owner 2026-11-27; migration 132).

A partner shop (first: Nimali Stores for Matugama) fills, order by order, the products a
branch sells but does not carry: its **Other Products** (``branch_inventory.sourcing_group``
``external_daily`` / ``external_slow``, set in the admin's branch inventory). While Supply
says the branch's partner is working (``branches.fulfilment_partner_active``):

* the partner sets those products' **branch price** (and weight-option prices) and their
  **stock** (its own count) through Supply (``PUT /internal/supply/partner-*``);
* a branch-scoped admin cannot change their price -- one owner per price (owner F17);
  Super Admin still can.

Everything else is untouched: Our Store / unclassified products, other branches, and every
branch while the flag is off. ``SUPPLY_PARTNER_ENABLED`` off = no route and no lock.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.models.branch import Branch
from app.models.product import BranchInventory, Product
from app.services import stock_keeping, weight_options_service

EXTERNAL = ("external_daily", "external_slow")


async def _inventory(db: AsyncSession, branch_id: UUID, product_id: UUID) -> Optional[BranchInventory]:
    return (await db.execute(select(BranchInventory).where(
        BranchInventory.branch_id == branch_id,
        BranchInventory.product_id == product_id))).scalar_one_or_none()


async def is_partner_item(db: AsyncSession, branch_id: UUID, product_id: UUID) -> bool:
    """The branch's partner prices and stocks this product now."""
    if not settings.supply_partner_enabled or branch_id is None:
        return False
    branch = await db.get(Branch, branch_id)
    if branch is None or not branch.fulfilment_partner_active:
        return False
    inv = await _inventory(db, branch_id, product_id)
    return inv is not None and inv.sourcing_group in EXTERNAL


async def guard_price(db: AsyncSession, scope, branch_id: Optional[UUID], product_id: UUID) -> None:
    """Refuse a branch-scoped admin's price change on a product the partner prices (F17)."""
    if scope.is_super_admin or branch_id is None:
        return
    if await is_partner_item(db, branch_id, product_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The partner store sets this product's price (Other Products).",
        )


async def set_partner(db: AsyncSession, branch_id: UUID, active: bool) -> dict:
    branch = await db.get(Branch, branch_id)
    if branch is None:
        raise HTTPException(status_code=404, detail="unknown branch")
    branch.fulfilment_partner_active = bool(active)
    await db.commit()
    return {"branch_id": str(branch_id), "active": branch.fulfilment_partner_active}


async def set_price(db: AsyncSession, branch_id: UUID, product_id: UUID,
                    variant_id: Optional[UUID], price: Decimal) -> dict:
    """The branch price the partner set. A weight option's price when ``variant_id`` is a
    weight option of the product; otherwise the product's branch price."""
    if not await is_partner_item(db, branch_id, product_id):
        raise HTTPException(status_code=409, detail="not an Other Products item of a branch with a partner")
    option = None
    if variant_id is not None and variant_id != product_id:
        option = await weight_options_service.get_option(db, product_id, variant_id)
    if option is not None:
        await weight_options_service.set_branch_price(db, branch_id, option, price)
        await db.commit()
        return {"product_id": str(product_id), "variant_id": str(variant_id), "price": str(price)}
    from app.services.product_service import ProductService

    inv = await _inventory(db, branch_id, product_id)
    await ProductService.update_inventory_row(db, inv, branch_price=price)
    return {"product_id": str(product_id), "variant_id": None, "price": str(price)}


async def set_stock(db: AsyncSession, branch_id: UUID, items: list) -> list[dict]:
    """The partner's own count for each item (absolute). Per item: ``set`` or why not."""
    out = []
    for it in items:
        if not await is_partner_item(db, branch_id, it.product_id):
            out.append({"product_id": str(it.product_id), "result": "not_partner"})
            continue
        mode = (await db.execute(select(Product.stock_mode).where(
            Product.product_id == it.product_id))).scalar_one_or_none()
        if it.grams is not None and mode != "loose":
            # Supply thinks it is weighed and we do not: never turn grams into a count
            out.append({"product_id": str(it.product_id), "result": "mode_mismatch"})
            continue
        if mode == "loose":
            # weighed into the bag per order: the partner's count is grams on the product
            if it.grams is None:
                out.append({"product_id": str(it.product_id), "result": "needs_grams"})
                continue
            done = await stock_keeping.set_level(
                db, branch_id, stock_keeping.Item("loose", it.product_id, None, None), it.grams)
            out.append({"product_id": str(it.product_id), "variant_id": None,
                        "result": "set" if done else "refused"})
            continue
        item = await stock_keeping.item(db, it.product_id, it.variant_id)
        done = await stock_keeping.set_level(db, branch_id, item, it.stock)
        out.append({"product_id": str(it.product_id),
                    "variant_id": str(it.variant_id) if it.variant_id else None,
                    "result": "set" if done else "refused"})
    await db.commit()
    return out
