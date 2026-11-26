"""
Pack scan: Online's half (Sribees Supply joint-go-live plan, phase E).

The packer scans each unit's QR label in the New Orders app. Online passes the scan to
Sribees Supply at once (Docker network, service token) with the order line it is for;
Supply checks the label is on that pack size, at this branch, not expired and free, and
holds the unit for the order. Supply later settles the hold from our order events
(``GET /internal/supply/events``): delivered, cancelled, returned.

Off unless ``SUPPLY_PACK_SCAN_ENABLED``. When on, an order is only marked packed once every
line is scanned (pieces: one label per unit; goods by weight: at least the kilograms ordered).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional
from uuid import UUID

import httpx
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.models.order import Order
from app.models.product import Product, ProductVariant

TIMEOUT = httpx.Timeout(8.0, connect=3.0)


def enabled() -> bool:
    """On only with the order events Supply settles its holds from (review #9)."""
    return bool(settings.supply_pack_scan_enabled and settings.supply_api_url
                and settings.supply_pick_token and settings.finance_outbox_enabled)


async def _call(method: str, path: str, json: Optional[dict] = None) -> dict:
    url = f"{settings.supply_api_url.rstrip('/')}/internal/online{path}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.request(method, url, json=json,
                                     headers={"Authorization": f"Bearer {settings.supply_pick_token}"})
    except httpx.HTTPError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Sribees Supply is not reachable; try the scan again")
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = None
        code = r.status_code if r.status_code in (404, 409, 422) else status.HTTP_502_BAD_GATEWAY
        raise HTTPException(status_code=code, detail=detail if isinstance(detail, str)
                            else "Sribees Supply refused the scan")
    return r.json()


async def _weights(db: AsyncSession, order: Order) -> dict:
    ids = [i.variant_id for i in order.items if i.variant_id]
    if not ids:
        return {}
    rows = await db.execute(select(ProductVariant.variant_id, ProductVariant.weight_grams)
                            .where(ProductVariant.variant_id.in_(ids)))
    return dict(rows.all())


async def _loose(db: AsyncSession, order: Order) -> set:
    """Products sold by weight (stock_mode 'loose'): their scans carry kilograms."""
    ids = [i.product_id for i in order.items if i.product_id]
    if not ids:
        return set()
    rows = await db.execute(select(Product.product_id).where(
        Product.product_id.in_(ids), Product.stock_mode == "loose"))
    return set(rows.scalars().all())


async def scan(db: AsyncSession, order: Order, item_id: UUID, label: str,
               weight_kg: Optional[Decimal], picked_by: str) -> dict:
    item = next((i for i in order.items if i.order_item_id == item_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="That item is not on this order")
    weights = await _weights(db, order)
    body = {
        "online_order_id": str(order.order_id), "order_number": order.order_number,
        "order_item_id": str(item.order_item_id), "online_branch_id": str(order.branch_id),
        "variant_id": str(item.variant_id) if item.variant_id else None,
        "quantity": int(item.quantity or 0), "label": label, "picked_by": picked_by[:120],
    }
    if item.variant_id and weights.get(item.variant_id):
        body["weight_grams"] = int(weights[item.variant_id])
    if weight_kg is not None:
        body["weight_kg"] = str(weight_kg)
    await _call("POST", "/pick", body)
    return await progress(db, order)


async def undo(order: Order, label: str) -> None:
    await _call("POST", "/unpick", {"online_order_id": str(order.order_id), "label": label})


async def progress(db: AsyncSession, order: Order) -> dict:
    """Per line: what is ordered and what is scanned; `complete` when packing may finish."""
    picks = {it["order_item_id"]: it for it in
             (await _call("GET", f"/orders/{order.order_id}/picks")).get("items", [])}
    weights = await _weights(db, order)
    loose = await _loose(db, order)
    lines, complete = [], True
    for i in order.items:
        got = picks.get(str(i.order_item_id), {})
        qty = int(i.quantity or 0)
        grams = weights.get(i.variant_id) if i.variant_id else None
        kg = Decimal(str(got.get("scanned_kg") or "0"))
        units = int(got.get("scanned_units") or 0)
        by_weight = i.product_id in loose or (kg > 0 and units == 0)
        need_kg = (Decimal(qty * grams) / 1000) if (grams and by_weight) else None
        # a line without a pack size has no stock in Supply: nothing to scan (review #10)
        no_scan = i.variant_id is None
        if no_scan:
            done = True
        elif by_weight:
            done = kg >= need_kg if need_kg is not None else kg > 0
        else:
            done = units >= qty
        complete = complete and done
        lines.append({"order_item_id": str(i.order_item_id), "name": i.product_name,
                      "variant": i.variant_name, "quantity": qty, "scanned_units": units,
                      "scanned_kg": str(kg), "need_kg": str(need_kg) if need_kg else None,
                      "by_weight": by_weight, "no_scan": no_scan,
                      "labels": got.get("labels", []), "done": done})
    return {"enabled": enabled(), "complete": complete, "lines": lines}


async def require_complete(db: AsyncSession, order: Order) -> None:
    if not enabled():
        return
    p = await progress(db, order)
    if not p["complete"]:
        left = [ln["name"] for ln in p["lines"] if not ln["done"]]
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Scan every unit before packing: " + ", ".join(left[:5]))
