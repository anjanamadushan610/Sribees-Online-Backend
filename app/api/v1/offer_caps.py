"""Super Admin: branch offers with a hidden per-customer loss cap (docs/plans/offer-loss-cap.md)."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import require_super_admin
from app.models.branch import Branch
from app.models.offer_cap import LIVE, SCHEDULED, OfferCap, OfferCapItem, OfferCapUsage
from app.models.product import BranchInventory, Product
from app.models.user import User
from app.services import offer_cap_service

router = APIRouter(tags=["Offer Caps"], dependencies=[Depends(require_super_admin)])


class OfferItemBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: UUID
    offer_price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)


class OfferBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    branch_id: UUID
    name: str = Field(min_length=1, max_length=120)
    cap_amount: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    starts_at: datetime
    ends_at: datetime
    items: list[OfferItemBody] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def sensible(self):
        if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
            raise ValueError("Dates must include a timezone.")
        if self.ends_at <= self.starts_at:
            raise ValueError("The offer must end after it starts.")
        if self.ends_at <= datetime.now(timezone.utc):
            raise ValueError("The end must be in the future.")
        if len({i.product_id for i in self.items}) != len(self.items):
            raise ValueError("A product is listed twice.")
        return self


async def _json(db: AsyncSession, offer: OfferCap) -> dict:
    rows = (
        await db.execute(
            select(OfferCapItem, Product.name)
            .join(Product, Product.product_id == OfferCapItem.product_id)
            .where(OfferCapItem.offer_id == offer.offer_id)
            .order_by(Product.name)
        )
    ).all()
    customers, taken = (
        await db.execute(
            select(func.count(func.distinct(OfferCapUsage.user_id)), func.coalesce(func.sum(OfferCapUsage.amount), 0))
            .where(OfferCapUsage.offer_id == offer.offer_id, OfferCapUsage.released_at.is_(None))
        )
    ).one()
    return {
        "offer_id": str(offer.offer_id),
        "branch_id": str(offer.branch_id),
        "name": offer.name,
        "cap_amount": float(offer.cap_amount),
        "starts_at": offer.starts_at.isoformat(),
        "ends_at": offer.ends_at.isoformat(),
        "status": offer.status,
        "customers": int(customers or 0),
        "loss_taken": float(taken or 0),
        "items": [
            {"product_id": str(i.product_id), "name": name, "offer_price": float(i.offer_price)}
            for i, name in rows
        ],
    }


@router.get("/admin/offer-caps", response_model=dict)
async def list_offers(branch_id: Optional[UUID] = None, db: AsyncSession = Depends(get_db)):
    query = select(OfferCap).order_by(OfferCap.starts_at.desc()).limit(100)
    if branch_id is not None:
        query = query.where(OfferCap.branch_id == branch_id)
    return {"success": True, "data": [await _json(db, o) for o in (await db.execute(query)).scalars().all()]}


@router.post("/admin/offer-caps", response_model=dict)
async def create_offer(body: OfferBody, db: AsyncSession = Depends(get_db), admin=Depends(require_super_admin)):
    if await db.get(Branch, body.branch_id) is None:
        raise HTTPException(404, "Branch not found")
    ids = [i.product_id for i in body.items]
    rows = {
        pid: (name, price, branch_price)
        for pid, name, price, branch_price in (
            await db.execute(
                select(Product.product_id, Product.name, Product.price, BranchInventory.branch_price)
                .join(
                    BranchInventory,
                    and_(BranchInventory.product_id == Product.product_id, BranchInventory.branch_id == body.branch_id),
                )
                .where(Product.product_id.in_(ids))
            )
        ).all()
    }
    for item in body.items:
        if item.product_id not in rows:
            raise HTTPException(400, f"Product {item.product_id} is not stocked in this branch.")
        name, price, branch_price = rows[item.product_id]
        regular = Decimal(str(branch_price if branch_price is not None else price))
        if item.offer_price >= regular:
            raise HTTPException(400, f"{name}: the offer price must be below the normal price ({regular}).")
        try:
            offer_cap_service.discount_for(regular, item.offer_price)
        except ValueError:
            raise HTTPException(400, f"{name}: {item.offer_price} cannot be charged exactly; try another price.")
    await offer_cap_service._lock(db)  # two creates cannot both pass the clash check
    clash = await db.scalar(
        select(Product.name)
        .join(OfferCapItem, OfferCapItem.product_id == Product.product_id)
        .join(OfferCap, OfferCap.offer_id == OfferCapItem.offer_id)
        .where(
            OfferCap.branch_id == body.branch_id,
            OfferCap.status.in_([SCHEDULED, LIVE]),
            OfferCap.starts_at < body.ends_at,
            OfferCap.ends_at > body.starts_at,
            OfferCapItem.product_id.in_(ids),
        )
        .limit(1)
    )
    if clash:
        raise HTTPException(409, f"{clash} is already in another offer at that time.")
    offer = OfferCap(
        branch_id=body.branch_id, name=body.name, cap_amount=body.cap_amount,
        starts_at=body.starts_at, ends_at=body.ends_at, status=SCHEDULED,
        created_by=getattr(admin, "admin_id", None),
    )
    db.add(offer)
    await db.flush()
    for item in body.items:
        db.add(OfferCapItem(offer_id=offer.offer_id, product_id=item.product_id, offer_price=item.offer_price))
    await db.commit()
    # Prices go on at starts_at (the worker); one starting now goes on here.
    # The offer is saved either way: a failure here leaves it to the worker.
    try:
        await offer_cap_service.apply_due(db)
    except Exception as exc:  # pragma: no cover - the worker retries
        await db.rollback()
        logger.warning(f"Offer {offer.offer_id} saved; starting it now failed: {exc}")
    await db.refresh(offer)
    return {"success": True, "data": await _json(db, offer)}


@router.post("/admin/offer-caps/{offer_id}/end", response_model=dict)
async def end_offer(offer_id: UUID, db: AsyncSession = Depends(get_db)):
    offer = await db.scalar(select(OfferCap).where(OfferCap.offer_id == offer_id).with_for_update())
    if offer is None:
        raise HTTPException(404, "Offer not found")
    await offer_cap_service.end_now(db, offer)
    await db.commit()
    await db.refresh(offer)
    return {"success": True, "data": await _json(db, offer)}


@router.get("/admin/offer-caps/{offer_id}/usage", response_model=dict)
async def offer_usage(offer_id: UUID, limit: int = Query(200, ge=1, le=1000), db: AsyncSession = Depends(get_db)):
    offer = await db.get(OfferCap, offer_id)
    if offer is None:
        raise HTTPException(404, "Offer not found")
    rows = (
        await db.execute(
            select(User.user_id, User.first_name, User.last_name, User.phone, func.sum(OfferCapUsage.amount), func.count())
            .join(User, User.user_id == OfferCapUsage.user_id)
            .where(OfferCapUsage.offer_id == offer_id, OfferCapUsage.released_at.is_(None))
            .group_by(User.user_id, User.first_name, User.last_name, User.phone)
            .order_by(func.sum(OfferCapUsage.amount).desc())
            .limit(limit)
        )
    ).all()
    cap = float(offer.cap_amount)
    return {
        "success": True,
        "data": [
            {
                "user_id": str(uid),
                "customer": " ".join(x for x in (first, last) if x) or phone,
                "phone": phone,
                "loss_taken": float(total),
                "orders": int(count),
                "reached": float(total) >= cap,
            }
            for uid, first, last, phone, total, count in rows
        ],
    }
