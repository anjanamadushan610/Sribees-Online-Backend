"""
Customer Coupons API (/coupons)

The shopper-facing side of promotions: browse the public "Available Offers"
feed, collect a coupon into a personal wallet, and review collected coupons.
Applying a coupon to the cart still goes through POST /cart/coupon.

Every route is scoped to the authenticated customer. Admin CRUD over the coupon
catalogue lives in app/api/v1/admin_coupons.py.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import get_current_user
from app.services.coupon_service import CouponValidationError
from app.services.user_coupon_service import UserCouponService

router = APIRouter(tags=["Coupons"])


class CollectCouponRequest(BaseModel):
    code: str = Field(..., min_length=2, max_length=50)


@router.get("/available", response_model=dict)
async def list_available_coupons(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Public, currently-valid coupons, each flagged with whether the customer
    has already collected it."""
    coupons = await UserCouponService.list_available(db, current_user.user_id)
    return {"success": True, "data": {"coupons": coupons}}


@router.get("/collected", response_model=dict)
async def list_collected_coupons(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """The customer's coupon wallet — collected coupons with a computed state
    (collected / used / expired)."""
    coupons = await UserCouponService.list_collected(db, current_user.user_id)
    return {"success": True, "data": {"coupons": coupons}}


@router.post("/collect", response_model=dict)
async def collect_coupon(
    body: CollectCouponRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Add a public coupon to the customer's wallet (idempotent)."""
    try:
        coupon = await UserCouponService.collect(db, current_user.user_id, body.code)
    except CouponValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return {"success": True, "data": coupon, "message": "Offer collected"}
