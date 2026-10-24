"""
Weight options: how a sold-by-weight product is priced.

A product with ``sold_by_weight`` is bought as one of its weight options --
250 g, 500 g, 1 kg ... -- each a ``ProductVariant`` with ``weight_grams`` set.
An option's price resolves the way a product's does:

    branch override (BranchVariantPrice) -> the option's global price
    less the product's effective Quick Sale discount in that branch

so a 10 % Quick Sale takes 10 % off every weight. Checkout prices lines
through :func:`price_map` (via OrderService._attach_server_prices); the
storefront and the admin read the same numbers from :func:`options_for`.

A removed option is deactivated, not deleted: past order lines reference it.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product import (
    BranchInventory,
    BranchVariantPrice,
    Product,
    ProductVariant,
)

DEFAULT_GRAMS: Tuple[int, ...] = (250, 500, 1000, 2000, 5000)
MAX_GRAMS = 50_000
_CENT = Decimal("0.01")


def label(grams: int) -> str:
    """'250g', '1kg', '2.5kg' -- the name shown on the option and the order line."""
    if grams < 1000:
        return f"{grams}g"
    kg = Decimal(grams) / 1000
    return f"{kg.normalize():f}kg"


def default_options(price_per_kg: Decimal) -> List[Tuple[int, Decimal]]:
    """The starting options for a product just switched on, priced pro rata."""
    return [
        (g, (Decimal(str(price_per_kg)) * g / 1000).quantize(_CENT, rounding=ROUND_HALF_UP))
        for g in DEFAULT_GRAMS
    ]


def _discounted(price: Decimal, discount) -> Decimal:
    if discount and discount > 0 and price:
        factor = Decimal(str(1 - discount / 100))
        return (price * factor).quantize(_CENT, rounding=ROUND_HALF_UP)
    return price


async def _discounts(
    db: AsyncSession, product_ids: Iterable[UUID], branch_id: Optional[UUID]
) -> Dict[UUID, object]:
    """The effective discount % per product in *branch_id* (same rule as get_price_map)."""
    ids = list(set(product_ids))
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(
                Product.product_id,
                Product.discount_percentage,
                BranchInventory.inventory_id,
                BranchInventory.discount_percentage,
            )
            .outerjoin(
                BranchInventory,
                and_(
                    BranchInventory.product_id == Product.product_id,
                    BranchInventory.branch_id == branch_id,
                ),
            )
            .where(Product.product_id.in_(ids))
        )
    ).all()
    out: Dict[UUID, object] = {}
    for pid, global_discount, inventory_id, branch_discount in rows:
        discount = global_discount
        if inventory_id is not None and branch_discount is not None:
            discount = branch_discount
        out[pid] = discount
    return out


async def _branch_prices(
    db: AsyncSession, variant_ids: Sequence[UUID], branch_id: Optional[UUID]
) -> Dict[UUID, Decimal]:
    if not branch_id or not variant_ids:
        return {}
    rows = (
        await db.execute(
            select(BranchVariantPrice.variant_id, BranchVariantPrice.price).where(
                BranchVariantPrice.branch_id == branch_id,
                BranchVariantPrice.variant_id.in_(list(variant_ids)),
            )
        )
    ).all()
    return {vid: Decimal(str(price)) for vid, price in rows}


async def options_for(
    db: AsyncSession, product_ids: Iterable[UUID], branch_id: Optional[UUID]
) -> Dict[str, List[dict]]:
    """
    ``{product_id: [option, ...]}`` -- the active weight options, lightest first.

    Each option carries ``global_price`` (the super admin's), ``branch_price``
    (this branch's override or None), ``original_price`` (before any Quick Sale
    discount) and ``price`` (what the customer pays here).
    """
    ids = list(set(product_ids))
    if not ids:
        return {}
    variants = (
        await db.execute(
            select(ProductVariant)
            .where(
                ProductVariant.product_id.in_(ids),
                ProductVariant.weight_grams.is_not(None),
                ProductVariant.is_active.is_not(False),
            )
            .order_by(ProductVariant.weight_grams)
        )
    ).scalars().all()
    if not variants:
        return {}
    overrides = await _branch_prices(db, [v.variant_id for v in variants], branch_id)
    discounts = await _discounts(db, ids, branch_id)

    out: Dict[str, List[dict]] = {}
    for v in variants:
        global_price = Decimal(str(v.price or 0))
        branch_price = overrides.get(v.variant_id)
        original = branch_price if branch_price is not None else global_price
        out.setdefault(str(v.product_id), []).append({
            "variant_id": str(v.variant_id),
            "weight_grams": v.weight_grams,
            "label": label(v.weight_grams),
            "global_price": float(global_price),
            "branch_price": float(branch_price) if branch_price is not None else None,
            "original_price": float(original),
            "price": float(_discounted(original, discounts.get(v.product_id))),
        })
    return out


async def price_map(
    db: AsyncSession, variant_ids: Iterable[UUID], branch_id: Optional[UUID]
) -> Dict[str, Tuple[str, Decimal, Optional[str], Optional[int]]]:
    """
    ``{variant_id: (product_id, price, label, weight_grams)}`` for active variants.

    The price is what the customer pays in *branch_id*: branch override, else
    the variant's own price, less the product's effective discount. Checkout's
    only source for a variant line's price.
    """
    ids = list(set(variant_ids))
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(
                ProductVariant.variant_id,
                ProductVariant.product_id,
                ProductVariant.price,
                ProductVariant.weight_grams,
                ProductVariant.name,
            ).where(
                ProductVariant.variant_id.in_(ids),
                ProductVariant.is_active.is_not(False),
            )
        )
    ).all()
    overrides = await _branch_prices(db, [r.variant_id for r in rows], branch_id)
    discounts = await _discounts(db, [r.product_id for r in rows], branch_id)
    out = {}
    for vid, pid, price, grams, name in rows:
        base = overrides.get(vid, Decimal(str(price or 0)))
        out[str(vid)] = (
            str(pid),
            _discounted(base, discounts.get(pid)),
            label(grams) if grams else name,
            grams,
        )
    return out


async def replace_options(
    db: AsyncSession, product: Product, options: Sequence[Tuple[int, Decimal]]
) -> None:
    """
    Make *options* ``[(grams, global_price), ...]`` the product's weight options.

    Matched by weight: an existing option keeps its id (so carts and branch
    prices stay attached) and takes the new price; a new weight is added; a
    weight no longer listed is deactivated. Caller commits.
    """
    existing = {
        v.weight_grams: v
        for v in (
            await db.execute(
                select(ProductVariant).where(
                    ProductVariant.product_id == product.product_id,
                    ProductVariant.weight_grams.is_not(None),
                )
            )
        ).scalars().all()
    }
    wanted = {}
    for grams, price in options:
        wanted[int(grams)] = Decimal(str(price)).quantize(_CENT, rounding=ROUND_HALF_UP)

    for order, grams in enumerate(sorted(wanted), start=1):
        variant = existing.get(grams)
        if variant is None:
            db.add(ProductVariant(
                product_id=product.product_id,
                name=label(grams),
                price=wanted[grams],
                stock_quantity=0,
                is_active=True,
                sort_order=order,
                weight_grams=grams,
            ))
        else:
            variant.price = wanted[grams]
            variant.name = label(grams)
            variant.is_active = True
            variant.sort_order = order
    for grams, variant in existing.items():
        if grams not in wanted:
            variant.is_active = False


async def get_option(
    db: AsyncSession, product_id: UUID, variant_id: UUID
) -> Optional[ProductVariant]:
    """The active weight option *variant_id* of *product_id*, or None."""
    return (
        await db.execute(
            select(ProductVariant).where(
                ProductVariant.variant_id == variant_id,
                ProductVariant.product_id == product_id,
                ProductVariant.weight_grams.is_not(None),
                ProductVariant.is_active.is_not(False),
            )
        )
    ).scalar_one_or_none()


async def set_branch_price(
    db: AsyncSession, branch_id: UUID, variant_id: UUID, price: Optional[Decimal]
) -> None:
    """Set (or with None, clear) one branch's price for an option. Caller commits."""
    row = (
        await db.execute(
            select(BranchVariantPrice).where(
                BranchVariantPrice.branch_id == branch_id,
                BranchVariantPrice.variant_id == variant_id,
            )
        )
    ).scalar_one_or_none()
    if price is None:
        if row is not None:
            await db.delete(row)
        return
    price = Decimal(str(price)).quantize(_CENT, rounding=ROUND_HALF_UP)
    if row is None:
        db.add(BranchVariantPrice(branch_id=branch_id, variant_id=variant_id, price=price))
    else:
        row.price = price
