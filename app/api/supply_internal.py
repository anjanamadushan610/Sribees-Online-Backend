"""
Internal: the product list Sribees Supply mirrors (``GET /internal/supply/catalog``).

Sribees Supply (supplier price lists, purchasing, base prices) matches each supplier's
mostly-Sinhala price lines to our products, so it needs the Sinhala name and the size
as well as the ids, and the product's courier weight (Supply splits a delivery charge
by it).

Fulfilment partner (Sribees Supply, owner 2026-11-27): each catalog product lists the
branches that sell it with their ``sourcing_group`` (Our Store / Other Products), and
``GET /orders/{id}`` gives the partner app who an order is for and its lines (the event
of a brand-new order is written before its lines exist).

Two more routes serve receiving. ``POST /stock-info`` reads what a branch sells an item
for now. ``POST /receipts`` is the one route that changes Online: it **adds** received
goods to a branch's stock (never sets a figure), each Supply receipt line once only, and
exists only while ``SUPPLY_STOCK_IN_ENABLED`` is on.

Same three locks as ``/internal/finance/*`` (app/api/finance_internal.py), with its
own token so the two services can be revoked separately:

* **Service token.** ``Authorization: Bearer <token>``; only its SHA-256 is in our
  settings (``SUPPLY_SERVICE_TOKEN_SHA256``), compared in constant time. No token
  configured = the route answers 404, as if it did not exist.
* **Never through the edge.** A request that came through Caddy carries
  ``X-Forwarded-For`` / ``Forwarded`` and is refused whatever token it has.
* **IP allow-list** (``SUPPLY_ALLOWED_IPS``) on the connecting peer, when set.
"""
import hashlib
import hmac
import ipaddress
import uuid
from collections import defaultdict
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.settings import settings
from app.core.media import media_url
from app.models.branch import Branch
from app.models.category import Category
from app.models.finance_outbox import FinanceOutbox
from app.models.order import Order, OrderItem
from app.models.product import BranchInventory, Product, ProductImage, ProductVariant
from app.models.user import Address, User
from app.services import supply_partner, supply_stock_in

router = APIRouter(prefix="/internal/supply", tags=["Internal: Supply"], include_in_schema=False)


def _allowed(peer: str) -> bool:
    raw = (settings.supply_allowed_ips or "").strip()
    if not raw:
        return True
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for part in raw.split(","):
        part = part.strip()
        if part and ip in ipaddress.ip_network(part, strict=False):
            return True
    return False


def require_supply_service(request: Request) -> None:
    expected = (settings.supply_service_token_sha256 or "").strip().lower()
    if not expected:
        raise HTTPException(status_code=404, detail="Not Found")
    if request.headers.get("x-forwarded-for") or request.headers.get("forwarded"):
        raise HTTPException(status_code=403, detail="not available from outside")
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    presented = hashlib.sha256(token.encode()).hexdigest() if token else ""
    if not token or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="service token required")
    peer = request.client.host if request.client else ""
    if not _allowed(peer):
        raise HTTPException(status_code=403, detail="caller not allowed")


_GRAMS_PER = {"kg": Decimal(1000), "g": Decimal(1), "l": Decimal(1000), "ml": Decimal(1),
              "lb": Decimal("453.592")}


def weight_grams(weight, unit) -> int | None:
    """A product's courier weight in grams (1 ml = 1 g). None when it is not entered or
    its unit is not one we know: Supply then asks for it, and never guesses."""
    factor = _GRAMS_PER.get((unit or "kg").strip().lower())
    if weight is None or factor is None:
        return None
    grams = int((Decimal(str(weight)) * factor).to_integral_value())
    return grams if grams > 0 else None


class StockInfoIn(BaseModel):
    branch_id: uuid.UUID
    items: list[supply_stock_in.ItemRef] = Field(min_length=1, max_length=500)


class ReceiptLinesIn(BaseModel):
    branch_id: uuid.UUID
    receipt_no: str = Field(min_length=1, max_length=40)
    lines: list[supply_stock_in.Line] = Field(min_length=1, max_length=500)


def require_partner(_: None = Depends(require_supply_service)) -> None:
    if not settings.supply_partner_enabled:
        raise HTTPException(status_code=404, detail="Not Found")


class PartnerFlagIn(BaseModel):
    active: bool


class PartnerPriceIn(BaseModel):
    branch_id: uuid.UUID
    product_id: uuid.UUID
    variant_id: uuid.UUID | None = None
    price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)


class PartnerStockItem(BaseModel):
    product_id: uuid.UUID
    variant_id: uuid.UUID | None = None
    stock: int = Field(ge=0, le=1_000_000)
    grams: int | None = Field(default=None, ge=0, le=1_000_000_000)  # a loose product's


class PartnerStockIn(BaseModel):
    branch_id: uuid.UUID
    items: list[PartnerStockItem] = Field(min_length=1, max_length=500)


def require_stock_in(_: None = Depends(require_supply_service)) -> None:
    if not settings.supply_stock_in_enabled:
        raise HTTPException(status_code=404, detail="Not Found")


async def _branch(db: AsyncSession, branch_id: uuid.UUID) -> None:
    if await db.get(Branch, branch_id) is None:
        raise HTTPException(status_code=404, detail="unknown branch")


@router.post("/stock-info")
async def supply_stock_info(
    body: StockInfoIn,
    _: None = Depends(require_supply_service),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """What the branch holds and sells each item for now (read only)."""
    await _branch(db, body.branch_id)
    return {"stock_in_enabled": bool(settings.supply_stock_in_enabled),
            "items": await supply_stock_in.info(db, body.branch_id, body.items)}


@router.get("/stock-levels")
async def supply_stock_levels(
    branch_id: uuid.UUID,
    _: None = Depends(require_supply_service),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Every product the branch carries: stock, reserved, minimum and maximum (read only).
    Sribees Supply prepares its draft orders from this (owner, 2026-11-26)."""
    await _branch(db, branch_id)
    rows = (await db.execute(
        select(BranchInventory, Product.stock_mode, Product.is_active)
        .join(Product, Product.product_id == BranchInventory.product_id)
        .where(BranchInventory.branch_id == branch_id))).all()
    return {"branch_id": str(branch_id), "items": [{
        "product_id": str(inv.product_id), "stock_mode": mode or "unit",
        "stock_quantity": int(inv.stock_quantity or 0), "stock_grams": int(inv.stock_grams or 0),
        "reserved_quantity": int(inv.reserved_quantity or 0),
        "low_stock_threshold": int(inv.low_stock_threshold or 0),
        "max_stock_level": inv.max_stock_level,
        "visible": bool(inv.is_active), "product_active": bool(active),
    } for inv, mode, active in rows]}


@router.post("/receipts")
async def supply_receipts(
    body: ReceiptLinesIn,
    _: None = Depends(require_stock_in),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Add received goods to the branch's stock. One transaction per line; a line sent
    again answers ``already``; a refused line changes nothing and may be sent again."""
    if settings.finance_stock_authority:
        raise HTTPException(status_code=409, detail="stock is kept by Sribees Finance")
    await _branch(db, body.branch_id)
    results = []
    for line in body.lines:
        results.append(await supply_stock_in.apply(db, body.branch_id, body.receipt_no, line))
    return {"results": results}


@router.get("/catalog")
async def supply_catalog(
    _: None = Depends(require_supply_service),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Every product (active or not) with its Sinhala name, size, main photo and pack sizes, and every
    branch (id, code, name, active): Sribees Supply follows our branches from here."""
    variants: dict = defaultdict(list)
    for v in (await db.execute(select(ProductVariant).order_by(ProductVariant.sort_order))).scalars():
        variants[v.product_id].append({
            "variant_id": str(v.variant_id), "sku": v.sku, "name": v.name,
            "weight_grams": v.weight_grams, "is_active": bool(v.is_active)})
    # the product's main photo, so Supply staff see what they match (owner 2026-11-26)
    image: dict = {}
    for i in (await db.execute(select(ProductImage).order_by(
            ProductImage.product_id, ProductImage.is_primary.desc(),
            ProductImage.sort_order, ProductImage.created_at))).scalars():
        image.setdefault(i.product_id, media_url(i.image_url))
    # where each product is on sale and how that branch gets it (migration 131): Supply sends
    # "Other Products" lines of an order to the branch's fulfilment partner
    listings: dict = defaultdict(list)
    for inv in (await db.execute(select(BranchInventory.product_id, BranchInventory.branch_id,
                                        BranchInventory.sourcing_group)
                                 .where(BranchInventory.is_active.is_(True)))).all():
        listings[inv.product_id].append({"branch_id": str(inv.branch_id),
                                         "sourcing_group": inv.sourcing_group})
    categories = dict((await db.execute(select(Category.category_id, Category.name))).all())
    products = (await db.execute(select(Product).order_by(Product.name))).scalars()
    branch_rows = (await db.execute(select(Branch).order_by(Branch.name))).scalars()
    return {
        "branches": [
            {"branch_id": str(b.branch_id), "code": b.code, "name": b.name,
             "is_active": bool(b.is_active)}
            for b in branch_rows],
        "products": [
            {"product_id": str(p.product_id), "sku": p.sku, "name": p.name, "name_si": p.name_si,
             "display_size": p.display_size, "display_size_unit": p.display_size_unit,
             "category": categories.get(p.category_id), "is_active": bool(p.is_active),
             "image_url": image.get(p.product_id),
             "weight_grams": weight_grams(p.weight, p.weight_unit),
             "variants": variants.get(p.product_id, []),
             "stock_mode": p.stock_mode or "unit",
             "listings": listings.get(p.product_id, [])}
            for p in products],
    }


# Pack scan (Supply joint-go-live plan, phase E): Supply settles the units its scans hold
# from what happened to the order. The same outbox Finance reads, only the order events.
SUPPLY_EVENT_TYPES = ("order.status", "order.undelivered")


@router.get("/events")
async def supply_events(
    after: int = Query(default=0, ge=0, description="return events with seq greater than this"),
    limit: int = Query(default=200, ge=1, le=1000),
    _: None = Depends(require_supply_service),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Every event after ``after`` in order; only the order events carry their details
    (others come as ``{"seq", "type", "occurred_at"}`` with an empty payload). Supply sees
    each number, so a hole means an event not committed yet, which it waits for."""
    rows = (await db.execute(
        select(FinanceOutbox).where(FinanceOutbox.seq > after)
        .order_by(FinanceOutbox.seq).limit(limit))).scalars().all()
    events = [
        {"seq": r.seq, "event_id": str(r.event_id), "type": r.event_type,
         "aggregate_id": str(r.aggregate_id) if r.aggregate_id else None,
         "occurred_at": r.occurred_at.isoformat(),
         "payload": r.payload if r.event_type in SUPPLY_EVENT_TYPES else {}}
        for r in rows
    ]
    return {"events": events, "enabled": bool(settings.finance_outbox_enabled)}


@router.get("/orders/{order_id}")
async def supply_order(
    order_id: uuid.UUID,
    _: None = Depends(require_supply_service),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """An order for the fulfilment partner: who it is for (owner 2026-11-27: the partner sees
    the customer's name, phone and address so orders never get mixed up) and its lines.
    Read only."""
    order = await db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="unknown order")
    user = await db.get(User, order.user_id)
    address = await db.get(Address, order.delivery_address_id) if order.delivery_address_id else None
    name = (address.recipient_name if address and address.recipient_name else None) or (
        user.full_name if user else None)
    lines = [address.address_line1, address.address_line2, address.postal_city,
             address.postal_code] if address else []
    return {
        "order_id": str(order.order_id),
        "order_number": order.order_number,
        "branch_id": str(order.branch_id) if order.branch_id else None,
        "customer_name": name,
        "phone": order.contact_phone or (address.phone if address else None)
        or (user.phone if user else None),
        "alternate_phone": order.alternate_phone,
        "address": ", ".join(x for x in lines if x) or None,
        "notes": order.notes,
        "status": order.status,
        "payment_status": order.payment_status,
        "lines": [
            {"order_item_id": str(i.order_item_id), "product_id": str(i.product_id),
             "variant_id": str(i.variant_id) if i.variant_id else None,
             "name": i.product_name, "variant_name": i.variant_name,
             "quantity": int(i.quantity or 0)}
            for i in (await db.execute(select(OrderItem).where(OrderItem.order_id == order.order_id)
                                       .order_by(OrderItem.created_at, OrderItem.order_item_id)))
            .scalars()],
    }


# ---------------------------------------------------------------- fulfilment partner
@router.put("/branches/{branch_id}/partner")
async def supply_branch_partner(
    branch_id: uuid.UUID,
    body: PartnerFlagIn,
    _: None = Depends(require_partner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Supply says whether the branch's fulfilment partner is working (migration 132)."""
    return await supply_partner.set_partner(db, branch_id, body.active)


@router.put("/partner-price")
async def supply_partner_price(
    body: PartnerPriceIn,
    _: None = Depends(require_partner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The partner's customer price for one of the branch's Other Products (Supply has
    checked the partner serves the branch and audited it)."""
    return await supply_partner.set_price(db, body.branch_id, body.product_id, body.variant_id,
                                          body.price)


@router.put("/partner-stock")
async def supply_partner_stock(
    body: PartnerStockIn,
    _: None = Depends(require_partner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The partner's own counts for the branch's Other Products (absolute)."""
    await _branch(db, body.branch_id)
    return {"results": await supply_partner.set_stock(db, body.branch_id, body.items)}
