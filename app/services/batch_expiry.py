"""
Expiry of stock batches (owner plan 2026-11-27, Sribees Supply barcode-first receiving).

* A batch whose expiry date has passed is **never sold**: FIFO pricing skips it at once
  (inventory_batches.allocate), and the sweep takes its remaining units off the branch's
  stock so the shop shows the right count. Staff write the goods off in Sribees Supply.
* "Close to expiry" = within the product's alert days: the product's own figure, else its
  sub-category's, else its category's, else 30.
* Dates are Sri Lanka dates (expiry dates are calendar dates on the pack).
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import async_session_maker
from app.config.settings import settings
from app.models.category import Category
from app.models.product import BranchInventory, InventoryBatch, Product

COLOMBO = timezone(timedelta(hours=5, minutes=30))
DEFAULT_ALERT_DAYS = 30
SWEEP_SECONDS = 900
BATCH = 200


def colombo_today(now: datetime | None = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(COLOMBO).date()


def expired(batch, today: date | None = None) -> bool:
    """Past its expiry date: the day printed on the pack is still a selling day."""
    expires_on = getattr(batch, "expires_on", None)
    return expires_on is not None and expires_on < (today or colombo_today())


def alert_days(product: Product | None, sub: Category | None, cat: Category | None) -> int:
    for days in (getattr(product, "expiry_alert_days", None),
                 getattr(sub, "expiry_alert_days", None),
                 getattr(cat, "expiry_alert_days", None)):
        if days:
            return int(days)
    return DEFAULT_ALERT_DAYS


async def sweep(db: AsyncSession, today: date | None = None, limit: int = BATCH) -> int:
    """Take expired units off stock, one inventory row at a time (row lock first, the same
    order as checkout). Returns how many batches were expired."""
    today = today or colombo_today()
    ids = (await db.execute(
        select(InventoryBatch.batch_id, InventoryBatch.inventory_id)
        .where(InventoryBatch.expires_on < today, InventoryBatch.remaining > 0)
        .order_by(InventoryBatch.inventory_id).limit(limit))).all()
    done = 0
    for batch_id, inventory_id in ids:
        row = await db.get(BranchInventory, inventory_id, with_for_update=True,
                           populate_existing=True)
        batch = await db.get(InventoryBatch, batch_id, with_for_update=True,
                             populate_existing=True)
        if row is None or batch is None or not expired(batch, today) or batch.remaining <= 0:
            await db.rollback()  # let go of the row locks
            continue
        units = int(batch.remaining)
        row.stock_quantity = max(int(row.stock_quantity or 0) - units, 0)
        batch.expired_units = int(batch.expired_units or 0) + units
        batch.remaining = 0
        batch.expired_at = datetime.now(timezone.utc)
        done += 1
        await db.commit()  # one row lock at a time: checkouts never wait on the whole sweep
        logger.info(f"Batch {batch.lot_code or batch.sequence} expired on {batch.expires_on}: "
                    f"{units} units off stock (inventory {inventory_id})")
    await db.commit()
    return done


async def near_expiry(db: AsyncSession, *, branch_ids: list | None = None,
                      today: date | None = None) -> list[dict]:
    """Batches with stock that are expired or within their alert days, soonest first."""
    today = today or colombo_today()
    q = (select(InventoryBatch, BranchInventory, Product)
         .join(BranchInventory, BranchInventory.inventory_id == InventoryBatch.inventory_id)
         .join(Product, Product.product_id == BranchInventory.product_id)
         .where(InventoryBatch.expires_on.is_not(None),
                (InventoryBatch.remaining > 0) | (InventoryBatch.expired_units > 0),
                InventoryBatch.expires_on <= today + timedelta(days=365))
         .order_by(InventoryBatch.expires_on, Product.name))
    if branch_ids is not None:
        q = q.where(BranchInventory.branch_id.in_(branch_ids))
    rows = (await db.execute(q)).all()
    cats = {c.category_id: c for c in (await db.scalars(select(Category))).all()}
    out = []
    for batch, inv, product in rows:
        days = alert_days(product, cats.get(product.subcategory_id), cats.get(product.category_id))
        left = (batch.expires_on - today).days
        if left > days:
            continue
        if batch.remaining <= 0 and left < -30:
            continue  # written off a month ago: no longer news
        out.append({
            "batch_id": str(batch.batch_id), "branch_id": str(inv.branch_id),
            "product_id": str(product.product_id), "name": product.name,
            "name_si": product.name_si, "lot_code": batch.lot_code,
            "expires_on": batch.expires_on.isoformat(), "days_left": left,
            "alert_days": days, "remaining": int(batch.remaining),
            "expired_units": int(batch.expired_units or 0),
            "state": "expired" if left < 0 else "near",
            "selling_price": str(batch.selling_price),
        })
    return out


async def run_batch_expiry_worker() -> None:
    """Every 15 minutes while BATCH_EXPIRY_ENABLED: expire what passed its date."""
    if not settings.batch_expiry_enabled:
        logger.info("Batch expiry sweep off (BATCH_EXPIRY_ENABLED=false)")
        return
    while True:
        try:
            async with async_session_maker() as db:
                for _ in range(50):  # at most 10,000 batches a round
                    if await sweep(db) < BATCH:
                        break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a failed sweep is retried next round
            logger.exception(f"Batch expiry sweep failed: {exc}")
        await asyncio.sleep(SWEEP_SECONDS)
