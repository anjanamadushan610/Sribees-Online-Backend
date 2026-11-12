"""
Internal: the product list Sribees Supply mirrors (``GET /internal/supply/catalog``).

Sribees Supply (supplier price lists, purchasing, base prices) matches each supplier's
mostly-Sinhala price lines to our products, so it needs the Sinhala name and the size
as well as the ids. Read only; nothing here changes Online.

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
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.settings import settings
from app.models.category import Category
from app.models.product import Product, ProductVariant

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


@router.get("/catalog")
async def supply_catalog(
    _: None = Depends(require_supply_service),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Every product (active or not) with its Sinhala name, size and pack sizes."""
    variants: dict = defaultdict(list)
    for v in (await db.execute(select(ProductVariant).order_by(ProductVariant.sort_order))).scalars():
        variants[v.product_id].append({
            "variant_id": str(v.variant_id), "sku": v.sku, "name": v.name,
            "weight_grams": v.weight_grams, "is_active": bool(v.is_active)})
    categories = dict((await db.execute(select(Category.category_id, Category.name))).all())
    products = (await db.execute(select(Product).order_by(Product.name))).scalars()
    return {"products": [
        {"product_id": str(p.product_id), "sku": p.sku, "name": p.name, "name_si": p.name_si,
         "display_size": p.display_size, "display_size_unit": p.display_size_unit,
         "category": categories.get(p.category_id), "is_active": bool(p.is_active),
         "variants": variants.get(p.product_id, [])}
        for p in products]}
