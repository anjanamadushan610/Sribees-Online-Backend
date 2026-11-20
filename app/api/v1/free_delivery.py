"""The welcome offer the app shows, and the Super Admin's branch and customer controls."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.redis import get_redis
from app.core.dependencies import get_current_user_optional, require_super_admin
from app.models.branch import Branch
from app.models.free_delivery import FreeDeliveryBranchOffer, FreeDeliveryGrant
from app.models.user import User
from app.services import free_delivery_service
from app.services.free_delivery_notifications import notify_grants

router = APIRouter(tags=["Free Delivery"])


@router.get("/free-delivery/welcome", response_model=dict)
async def welcome_offer(
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user=Depends(get_current_user_optional),
):
    """What the popup after the tour says, for this person in this branch. Null: no popup."""
    from app.api.v1.products import _get_branch_context_for_request

    try:
        context = await _get_branch_context_for_request(redis, request, current_user)
    except HTTPException:
        return {"success": True, "data": None}  # no location chosen yet
    offer = await free_delivery_service.welcome_offer(
        db, current_user.user_id if current_user else None, UUID(context["branch_id"])
    )
    return {"success": True, "data": offer}


class BranchOfferBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    months: int = Field(3, ge=1, le=24)
    min_order: Decimal = Field(Decimal("0"), ge=0, max_digits=10, decimal_places=2)


class GrantBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    user_ids: list[UUID] = Field(min_length=1, max_length=200)
    ends_at: datetime
    branch_id: Optional[UUID] = None
    min_order: Decimal = Field(Decimal("0"), ge=0, max_digits=10, decimal_places=2)
    note: Optional[str] = Field(None, max_length=200)

    @model_validator(mode="after")
    def sensible(self):
        if self.ends_at.tzinfo is None:
            raise ValueError("The end date must include a timezone.")
        if self.ends_at <= datetime.now(timezone.utc):
            raise ValueError("The end date must be in the future.")
        return self


def _offer_json(branch: Branch, offer: Optional[FreeDeliveryBranchOffer]) -> dict:
    ends_at = free_delivery_service.offer_ends_at(offer) if offer else None
    return {
        "branch_id": str(branch.branch_id),
        "branch_name": branch.name,
        "enabled": bool(offer and offer.enabled),
        "months": offer.months if offer else 3,
        "min_order": float(offer.min_order) if offer else 0.0,
        "enabled_at": offer.enabled_at.isoformat() if offer and offer.enabled_at else None,
        "ends_at": ends_at.isoformat() if ends_at else None,
        "expired": bool(ends_at and ends_at <= datetime.now(timezone.utc)),
    }


@router.get("/admin/free-delivery/branches", response_model=dict, dependencies=[Depends(require_super_admin)])
async def list_branch_offers(db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(Branch, FreeDeliveryBranchOffer)
            .outerjoin(FreeDeliveryBranchOffer, FreeDeliveryBranchOffer.branch_id == Branch.branch_id)
            .order_by(Branch.name)
        )
    ).all()
    return {"success": True, "data": [_offer_json(branch, offer) for branch, offer in rows]}


@router.put("/admin/free-delivery/branches/{branch_id}", response_model=dict, dependencies=[Depends(require_super_admin)])
async def save_branch_offer(branch_id: UUID, body: BranchOfferBody, db: AsyncSession = Depends(get_db)):
    branch = await db.get(Branch, branch_id)
    if branch is None:
        raise HTTPException(404, "Branch not found")
    offer = await db.get(FreeDeliveryBranchOffer, branch_id)
    if offer is None:
        offer = FreeDeliveryBranchOffer(branch_id=branch_id)
        db.add(offer)
    # The period is counted from each switch-on.
    if body.enabled and not offer.enabled:
        offer.enabled_at = datetime.now(timezone.utc)
    offer.enabled, offer.months, offer.min_order = body.enabled, body.months, body.min_order
    await db.commit()
    return {"success": True, "data": _offer_json(branch, offer)}


@router.get("/admin/free-delivery/grants", response_model=dict, dependencies=[Depends(require_super_admin)])
async def list_grants(
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(FreeDeliveryGrant, User, Branch.name)
        .join(User, User.user_id == FreeDeliveryGrant.user_id)
        .outerjoin(Branch, Branch.branch_id == FreeDeliveryGrant.branch_id)
        .order_by(FreeDeliveryGrant.created_at.desc())
        .limit(limit)
    )
    rows = (await db.execute(query)).all()
    return {
        "success": True,
        "data": [
            {
                "grant_id": str(g.grant_id),
                "user_id": str(g.user_id),
                "customer": " ".join(x for x in (u.first_name, u.last_name) if x) or u.phone or u.email,
                "phone": u.phone,
                "branch_name": name,
                "starts_at": g.starts_at.isoformat(),
                "ends_at": g.ends_at.isoformat(),
                "min_order": float(g.min_order),
                "note": g.note,
                "revoked_at": g.revoked_at.isoformat() if g.revoked_at else None,
            }
            for g, u, name in rows
        ],
    }


@router.post("/admin/free-delivery/grants", response_model=dict)
async def create_grants(
    body: GrantBody, db: AsyncSession = Depends(get_db), admin=Depends(require_super_admin)
):
    found = set(
        (await db.execute(select(User.user_id).where(User.user_id.in_(body.user_ids), User.deleted_at.is_(None))))
        .scalars()
        .all()
    )
    if len(found) != len(set(body.user_ids)):
        raise HTTPException(404, "A selected customer no longer exists")
    branch = await db.get(Branch, body.branch_id) if body.branch_id else None
    if body.branch_id is not None and branch is None:
        raise HTTPException(404, "Branch not found")
    branch_name = branch.name if branch else None
    now = datetime.now(timezone.utc)
    grants = []
    for user_id in found:
        grant_id = uuid4()
        grants.append((user_id, grant_id))
        db.add(
            FreeDeliveryGrant(
                grant_id=grant_id, user_id=user_id, branch_id=body.branch_id, starts_at=now, ends_at=body.ends_at,
                min_order=body.min_order, note=body.note, granted_by=getattr(admin, "admin_id", None),
            )
        )
    await db.commit()
    # Never announce an entitlement that failed to commit. Notification delivery
    # is best effort, as with customer coupon gifts; the grant remains usable.
    notified = await notify_grants(
        db, grants, ends_at=body.ends_at, min_order=body.min_order, branch_name=branch_name
    )
    return {"success": True, "data": {"granted": len(found), "notified": notified}}


@router.delete("/admin/free-delivery/grants/{grant_id}", response_model=dict, dependencies=[Depends(require_super_admin)])
async def revoke_grant(grant_id: UUID, db: AsyncSession = Depends(get_db)):
    grant = await db.get(FreeDeliveryGrant, grant_id)
    if grant is None:
        raise HTTPException(404, "Grant not found")
    if grant.revoked_at is None:
        grant.revoked_at = datetime.now(timezone.utc)
        await db.commit()
    return {"success": True, "data": {"revoked": True}}
