"""
Internal: the product list Sribees Supply mirrors (``GET /internal/supply/catalog``).

Sribees Supply (supplier price lists, purchasing, base prices) matches each supplier's
mostly-Sinhala price lines to our products, so it needs the Sinhala name and the size
as well as the ids, and the product's courier weight (Supply splits a delivery charge
by it).

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

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.settings import settings
from app.core.media import media_url
from app.models.branch import Branch
from app.models.category import Category
from app.models.product import BranchInventory, Product, ProductImage, ProductVariant
from app.services import batch_expiry, supply_stock_in

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


class BatchPriceIn(BaseModel):
    branch_id: uuid.UUID
    selling_price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)


@router.post("/batches/{batch_id}/price")
async def supply_batch_price(
    batch_id: uuid.UUID,
    body: BatchPriceIn,
    _: None = Depends(require_stock_in),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The branch manager's reviewed selling price for a delivered batch (owner 2026-11-27)."""
    try:
        return await supply_stock_in.set_batch_price(db, batch_id, body.selling_price,
                                                     branch_id=body.branch_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="batch not found")
    except ValueError as why:
        raise HTTPException(status_code=422, detail=str(why))


@router.get("/expiring")
async def supply_expiring(
    branch_id: uuid.UUID | None = None,
    _: None = Depends(require_supply_service),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Online batches that are expired or close to expiry (each product's alert days)."""
    return {"today": batch_expiry.colombo_today().isoformat(),
            "items": await batch_expiry.near_expiry(
                db, branch_ids=[branch_id] if branch_id else None)}


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
    cats = {c.category_id: c for c in (await db.execute(select(Category))).scalars()}
    categories = {k: c.name for k, c in cats.items()}
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
             # days before expiry it shows as close to expiry (owner 2026-11-27)
             "expiry_alert_days": batch_expiry.alert_days(
                 p, cats.get(p.subcategory_id), cats.get(p.category_id)),
             "variants": variants.get(p.product_id, [])}
            for p in products],
    }
