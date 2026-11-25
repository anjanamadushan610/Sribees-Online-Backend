"""
Product options: one product in several shades / colours (migration 123).

A product with ``option_label`` ("Shade", "Colour") is bought as one of its
options -- ``ProductVariant`` rows with ``weight_grams`` NULL. Each option has

* its own photo (``image_url``; NULL = the product's photo),
* its own stock per branch (the product is ``stock_mode = 'packed'``, so
  ``branch_variant_inventory`` holds a count per option, stock_keeping.py),
* the product's price unless it has one of its own (``price`` NULL = the
  product's branch price; the product's discount applies either way).

Pricing is NOT done here: checkout, the cart and the product page all read
``weight_options_service.price_map`` / ``regular_price_map``, which know both
kinds of option. That keeps one price path for every variant line.

An option is never deleted, only deactivated: past order lines point at it.
A line for a product with options but without a valid, active option is
refused (``OptionRequired``) -- that is what an old app version sends, since it
does not know options exist.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, Iterable, List, Optional, Sequence, Set
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.media import media_path, media_url
from app.models.product import (
    BranchInventory,
    BranchVariantInventory,
    Product,
    ProductVariant,
)

MAX_OPTIONS = 60
MAX_NAME = 50  # order_items.variant_name is VARCHAR(50)
MAX_LABEL = 20
_CENT = Decimal("0.01")

OPTION_REQUIRED = "OPTION_REQUIRED"

# Shown when a client that cannot pick an option (an old app) tries to buy the
# product. en / si / ta -- the old app shows the server's message as is.
_REQUIRED_TEXT = {
    "en": "Please update the SRIBEES app to choose a {label} for {name}.",
    "si": "{name} සඳහා {label} එකක් තෝරන්න SRIBEES app එක update කරන්න.",
    "ta": "{name} க்கு {label} தேர்வு செய்ய SRIBEES app ஐ update செய்யவும்.",
}


class OptionRequired(ValueError):
    """A line for a product with options came without a valid option."""

    code = OPTION_REQUIRED

    def __init__(self, name: str, label: str, lang: str = "en"):
        text = _REQUIRED_TEXT.get(lang, _REQUIRED_TEXT["en"])
        super().__init__(text.format(name=name or "this product", label=(label or "option").lower()))
        self.product_name = name
        self.label = label


class OptionsError(ValueError):
    """An admin's option list cannot be saved (message is for the admin)."""


@dataclass
class OptionIn:
    name: str
    variant_id: Optional[UUID] = None
    price: Optional[Decimal] = None
    image_url: Optional[str] = None
    is_active: bool = True


def _uuid(raw) -> Optional[UUID]:
    if raw is None or isinstance(raw, UUID):
        return raw
    try:
        return UUID(str(raw))
    except (ValueError, TypeError):
        return None


async def labels(db: AsyncSession, product_ids: Iterable) -> Dict[str, str]:
    """``{product_id: option_label}`` for the products that have options."""
    ids = [u for u in (_uuid(p) for p in product_ids) if u]
    if not ids:
        return {}
    rows = (await db.execute(
        select(Product.product_id, Product.option_label).where(
            Product.product_id.in_(ids), Product.option_label.is_not(None))
    )).all()
    return {str(pid): label for pid, label in rows}


async def check_line(db: AsyncSession, product_id, variant_id, lang: str = "en") -> None:
    """Refuse a line for a product with options unless it names one of its active
    options. Raises OptionRequired; a product without options always passes."""
    pid = _uuid(product_id)
    if pid is None:
        return
    row = (await db.execute(
        select(Product.name, Product.option_label).where(Product.product_id == pid)
    )).first()
    if row is None or not row.option_label:
        return
    vid = _uuid(variant_id)
    if vid is not None:
        ok = (await db.execute(select(ProductVariant.variant_id).where(
            ProductVariant.variant_id == vid,
            ProductVariant.product_id == pid,
            ProductVariant.weight_grams.is_(None),
            ProductVariant.is_active.is_not(False),
        ))).first()
        if ok is not None:
            return
    raise OptionRequired(row.name, row.option_label, lang)


async def missing_options(db: AsyncSession, items: Sequence[dict]) -> Optional[dict]:
    """The first cart line (a dict with product_id / variant_id) that needs an
    option and has none -- or None when every line is fine."""
    need = await labels(db, [i.get("product_id") for i in items])
    if not need:
        return None
    for item in items:
        if str(item.get("product_id")) in need:
            try:
                await check_line(db, item.get("product_id"), item.get("variant_id"))
            except OptionRequired:
                return item
    return None


async def _variants(db: AsyncSession, product_id: UUID, *, active_only: bool) -> List[ProductVariant]:
    q = select(ProductVariant).where(
        ProductVariant.product_id == product_id, ProductVariant.weight_grams.is_(None))
    if active_only:
        q = q.where(ProductVariant.is_active.is_not(False))
    return list((await db.execute(q.order_by(ProductVariant.sort_order, ProductVariant.name))).scalars())


async def options_for(db: AsyncSession, product: Product, branch_id: Optional[UUID]) -> List[dict]:
    """The active options as the shop shows them in *branch_id*: price (after the
    product's discount), regular price, photo and how many can still be sold
    (None = not tracked in that branch)."""
    from app.services import stock_keeping, weight_options_service

    variants = await _variants(db, product.product_id, active_only=True)
    if not variants:
        return []
    ids = [v.variant_id for v in variants]
    paid = await weight_options_service.price_map(db, ids, branch_id)
    regular = await weight_options_service.regular_price_map(db, ids, branch_id)
    out = []
    for v in variants:
        key = str(v.variant_id)
        price = paid[key][1] if key in paid else Decimal(0)
        original = regular[key][1] if key in regular else price
        out.append({
            "variant_id": key,
            "name": v.name,
            "image_url": media_url(v.image_url) if v.image_url else None,
            "price": float(price),
            "original_price": float(max(price, original)),
            "has_own_price": v.price is not None,
            "available": (await stock_keeping.available(db, branch_id, product.product_id, v.variant_id)
                          if branch_id else None),
        })
    return out


async def admin_options(db: AsyncSession, product: Product, branch_id: Optional[UUID] = None) -> dict:
    """Every option, inactive ones too, as the admin edits them."""
    variants = await _variants(db, product.product_id, active_only=False)
    counts: Dict[UUID, int] = {}
    if branch_id and variants:
        counts = dict((await db.execute(
            select(BranchVariantInventory.variant_id, BranchVariantInventory.stock_quantity).where(
                BranchVariantInventory.branch_id == branch_id,
                BranchVariantInventory.variant_id.in_([v.variant_id for v in variants]))
        )).all())
    return {
        "product_id": str(product.product_id),
        "option_label": product.option_label,
        "stock_mode": product.stock_mode or "unit",
        "options": [{
            "variant_id": str(v.variant_id),
            "name": v.name,
            "price": float(v.price) if v.price is not None else None,
            "image_url": media_url(v.image_url) if v.image_url else None,
            "is_active": v.is_active is not False,
            "sort_order": v.sort_order or 0,
            **({"stock_quantity": int(counts.get(v.variant_id, 0))} if branch_id else {}),
        } for v in variants],
    }


def _clean(options: Sequence[OptionIn]) -> List[OptionIn]:
    if len(options) > MAX_OPTIONS:
        raise OptionsError(f"At most {MAX_OPTIONS} options.")
    seen: Set[str] = set()
    out = []
    for o in options:
        name = " ".join((o.name or "").split())
        if not name:
            raise OptionsError("Every option needs a name.")
        if len(name) > MAX_NAME:
            raise OptionsError(f"An option name can be at most {MAX_NAME} characters: {name}")
        if name.lower() in seen:
            raise OptionsError(f"Two options are called {name}.")
        seen.add(name.lower())
        price = None
        if o.price is not None:
            price = Decimal(str(o.price)).quantize(_CENT, rounding=ROUND_HALF_UP)
            if price <= 0:
                raise OptionsError(f"{name}: a price must be above 0 (leave it empty to use the product's).")
        image = (media_path(o.image_url) or o.image_url) if o.image_url else None
        out.append(OptionIn(name=name, variant_id=o.variant_id, price=price, image_url=image,
                            is_active=bool(o.is_active)))
    return out


async def save_options(
    db: AsyncSession, product: Product, label: Optional[str], options: Sequence[OptionIn]
) -> None:
    """
    Make *options* (in order) the product's options and *label* its option label.

    Matched by ``variant_id``: an existing option keeps its id (carts, stock and
    past orders stay attached) and takes the new name/price/photo; one without an
    id is added; an option left out is deactivated. ``label`` None (or no active
    option left) turns the product back into a plain one. Switching between plain
    and options zeroes the branch stock -- it was counted in the other measure --
    the same rule ProductService.update applies to any stock mode change.
    Caller commits.
    """
    from app.config.settings import settings

    label = " ".join((label or "").split()) or None
    if label and len(label) > MAX_LABEL:
        raise OptionsError(f"The option label can be at most {MAX_LABEL} characters.")
    cleaned = _clean(options)
    if label and product.sold_by_weight:
        raise OptionsError("A product sold by weight has weight options; it cannot also have shades.")
    if label and not any(o.is_active for o in cleaned):
        raise OptionsError("Give at least one active option, or clear the option label.")

    existing = {v.variant_id: v for v in await _variants(db, product.product_id, active_only=False)}
    for o in cleaned:
        if o.variant_id is not None and o.variant_id not in existing:
            raise OptionsError(f"{o.name}: that option belongs to another product.")

    new_mode = "packed" if label else ("unit" if not product.sold_by_weight else product.stock_mode)
    mode_changes = (product.stock_mode or "unit") != new_mode and not product.sold_by_weight
    if mode_changes and settings.finance_stock_authority:
        raise OptionsError("Stock is kept by Sribees Finance; change how it is kept there first.")

    kept = set()
    for order, o in enumerate(cleaned, start=1):
        v = existing.get(o.variant_id) if o.variant_id else None
        if v is None:
            v = ProductVariant(product_id=product.product_id, stock_quantity=0)
            db.add(v)
        v.name = o.name
        v.price = o.price
        v.image_url = o.image_url
        v.is_active = o.is_active and bool(label)
        v.sort_order = order
        v.weight_grams = None
        if o.variant_id:
            kept.add(o.variant_id)
    for vid, v in existing.items():
        if vid not in kept:
            v.is_active = False

    product.option_label = label
    if mode_changes:
        product.stock_mode = new_mode
        await db.execute(BranchInventory.__table__.update()
                         .where(BranchInventory.__table__.c.product_id == product.product_id)
                         .values(stock_quantity=0, stock_grams=0))
        await db.execute(BranchVariantInventory.__table__.delete().where(
            BranchVariantInventory.__table__.c.variant_id.in_(
                select(ProductVariant.variant_id).where(ProductVariant.product_id == product.product_id))))
    await db.flush()
    if not mode_changes and label:
        # A deactivated shade's packs no longer count as "in stock".
        from app.services import stock_keeping

        branches = (await db.execute(select(BranchInventory.branch_id).where(
            BranchInventory.product_id == product.product_id))).scalars().all()
        for branch_id in branches:
            await stock_keeping.refresh_summary(db, branch_id, product.product_id)
