"""
Admin Customer Promos API — the 1-to-1 retention workflow.

One endpoint: issue a private coupon to a single named customer from their
profile drawer, drop it straight into their in-app wallet, and tell them about
it. This is how a branch wins back a customer the Customer Directory has
flagged ``at_risk``, or apologises to one whose order was refunded.

Why a private coupon and not just a normal code: a campaign code handed to one
person leaks. The moment it is typed into a group chat it becomes an uncapped
discount for everyone, which is precisely the overspending the budget cap
exists to prevent. A coupon issued here carries ``target_user_id``, so the
database itself refuses it for anybody else, and ``is_public = False`` keeps it
out of the browsable offers feed (the DB constraint
``ck_coupons_targeted_is_private`` makes that pairing impossible to get wrong).

Prefix "/admin/customers" is applied by app/api/v1/router.py.
"""
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.redis import get_redis_optional
from app.core.dependencies import BranchScope, inject_branch_filter, require_permission
from app.models.admin import Admin
from app.models.coupon import Coupon
from app.models.order import Order
from app.models.user import User
from app.schemas.notification import NotificationTypeEnum
from app.services import notification_i18n as i18n
from app.services.notification_service import NotificationService

router = APIRouter(tags=["Admin Customer Promos"])

# Presets the drawer offers. Kept server-side rather than in the React form so
# the terms of a "15% Win-Back" are the same for every branch and every
# manager, and so changing them does not need a frontend deploy. The client may
# still override any individual field — these are starting points, not a
# straitjacket.
PROMO_PRESETS: dict[str, dict] = {
    "winback": {
        "label": "15% Win-Back",
        "code_prefix": "RECOVER15",
        "discount_type": "percentage",
        "discount_value": Decimal("15"),
        "min_order_value": Decimal("1500"),
        "valid_days": 7,
        "description": "Win-back offer for a lapsed customer",
    },
    "freedel": {
        "label": "Free Delivery",
        "code_prefix": "FREEDEL",
        "discount_type": "fixed",
        "discount_value": Decimal("350"),
        # A fixed discount must not exceed the order it applies to, which the
        # coupon rules enforce anyway; matching them here keeps the preset
        # from ever producing an invalid coupon.
        "min_order_value": Decimal("1000"),
        "valid_days": 7,
        "description": "Delivery fee waived for a customer who stopped ordering",
    },
    "apology": {
        "label": "Rs 500 Apology Token",
        "code_prefix": "SORRY500",
        "discount_type": "fixed",
        "discount_value": Decimal("500"),
        "min_order_value": Decimal("500"),
        "valid_days": 3,
        "description": "Goodwill token after a refund or dispute",
    },
}


class AssignPromoRequest(BaseModel):
    """
    Issue a targeted coupon. Every field except ``preset`` is optional and
    overrides that preset's default.
    """

    preset: Literal["winback", "freedel", "apology"] = "winback"
    code: Optional[str] = Field(None, min_length=4, max_length=50)
    discount_type: Optional[Literal["percentage", "fixed"]] = None
    discount_value: Optional[Decimal] = Field(None, gt=0)
    min_order_value: Optional[Decimal] = Field(None, ge=0)
    valid_days: Optional[int] = Field(None, ge=1, le=90)

    @model_validator(mode="after")
    def _normalize(self) -> "AssignPromoRequest":
        if self.code:
            self.code = self.code.strip().upper()
        if (
            self.discount_type == "percentage"
            and self.discount_value is not None
            and self.discount_value > 100
        ):
            raise ValueError("Percentage discount cannot exceed 100")
        return self


def _suggest_code(prefix: str, full_name: Optional[str]) -> str:
    """
    A readable, unguessable code: PREFIX-NAME-XXXX.

    The name makes it obvious in support chat which customer a code belongs to.
    The random suffix is what stops it being guessable — ``RECOVER15-KASUN`` on
    its own is something anyone called Kasun could type in, and a targeted
    coupon that a stranger can guess the code of is only private because the
    ``target_user_id`` check catches them. Belt and braces.
    """
    first = (full_name or "").strip().split(" ")[0].upper()
    first = "".join(ch for ch in first if ch.isalnum())[:10] or "CUSTOMER"
    return f"{prefix}-{first}-{secrets.token_hex(2).upper()}"


@router.post("/{user_id}/promo", response_model=dict, status_code=status.HTTP_201_CREATED)
async def assign_promo(
    user_id: UUID,
    body: AssignPromoRequest,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(require_permission("marketing", "create")),
    scope: BranchScope = Depends(inject_branch_filter),
    redis=Depends(get_redis_optional),
):
    """
    Issue a private, single-customer coupon and deliver it to their app.

    Three things happen, in this order and for this reason:

    1. The coupon row is written, pinned to this branch and to this customer.
    2. It is auto-collected into their wallet, so they do not have to find and
       "claim" a code that was given to them personally.
    3. A push notification tells them it is there. A retention offer nobody
       opens the app to discover does not retain anybody.

    Steps 2 and 3 are best-effort: the coupon is real once step 1 commits, and
    a Redis or FCM outage must not lose it. The response reports honestly which
    of the three actually happened rather than implying success.
    """
    customer = (
        await db.execute(select(User).where(User.user_id == user_id))
    ).scalar_one_or_none()
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    # Branch isolation, identical to the rest of the customer directory: a
    # scoped admin may only act on a customer their own branch has served.
    # Without this, any branch's marketing manager could mint discounts
    # redeemable at their branch for the entire national customer base.
    branch_id = None if scope.is_unscoped else scope.branch_id
    if branch_id is not None:
        served_here = await db.scalar(
            select(func.count(Order.order_id)).where(
                Order.user_id == user_id, Order.branch_id == branch_id
            )
        )
        if not served_here:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found"
            )

    preset = PROMO_PRESETS[body.preset]
    discount_type = body.discount_type or preset["discount_type"]
    discount_value = (
        body.discount_value if body.discount_value is not None else preset["discount_value"]
    )
    min_order_value = (
        body.min_order_value if body.min_order_value is not None else preset["min_order_value"]
    )
    valid_days = body.valid_days or preset["valid_days"]

    # The same rule the public coupon schema enforces: a fixed discount that
    # exceeds the order it applies to would hand out free money.
    if discount_type == "fixed" and min_order_value < discount_value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Minimum order must be at least the fixed discount amount",
        )

    code = body.code or _suggest_code(preset["code_prefix"], customer.full_name)
    if await db.scalar(select(Coupon.coupon_id).where(func.lower(Coupon.code) == code.lower())):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A coupon with this code already exists",
        )

    now = datetime.now(timezone.utc)
    coupon = Coupon(
        coupon_id=uuid4(),
        code=code,
        description=f"{preset['description']} — {customer.full_name or customer.email}",
        branch_id=branch_id,
        discount_type=discount_type,
        discount_value=discount_value,
        min_order_value=min_order_value,
        valid_from=now,
        # Short windows are the point of a retention offer: an open-ended
        # apology token is just a permanent discount.
        valid_until=now + timedelta(days=valid_days),
        is_active=True,
        # One person, one use. usage_limit alone would let the recipient share
        # it; target_user_id is what actually makes it theirs.
        usage_limit=1,
        per_user_limit=1,
        target_user_id=user_id,
        is_public=False,
        # Retention money is not for stacking on clearance stock.
        exclude_quick_sale=True,
    )
    db.add(coupon)
    await db.commit()
    await db.refresh(coupon)

    logger.info(
        f"[admin] Promo {coupon.code} issued to customer {user_id} by {admin.email}"
    )

    # --- Best-effort delivery -------------------------------------------------
    collected = False
    if redis is not None:
        try:
            await redis.sadd(f"user:{user_id}:collected_coupons", code.upper())
            collected = True
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Could not auto-collect promo {code} into wallet: {exc}")

    # An offer: a customer who switched offers off still gets the coupon in
    # their wallet, just no notification about it. `push_sent` says which.
    notified = False
    try:
        notification = await NotificationService.notify(
            db,
            user_id,
            template="promo.gift",
            type=NotificationTypeEnum.PROMOTION,
            category="offers",
            params={"code": code, "days": valid_days},
            per_lang_params=lambda lang: {
                "off": i18n.coupon_value(discount_type, discount_value, lang)
            },
            reference_type="coupon",
            data={"type": "coupon", "code": code, "screen": "offers"},
        )
        notified = notification is not None
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Could not notify customer {user_id} of promo {code}: {exc}")

    return {
        "success": True,
        "data": {
            "coupon_id": str(coupon.coupon_id),
            "code": coupon.code,
            "discount_type": coupon.discount_type,
            "discount_value": float(coupon.discount_value),
            "min_order_value": float(coupon.min_order_value),
            "valid_until": coupon.valid_until.isoformat(),
            "target_user_id": str(user_id),
            # Reported rather than assumed: the manager needs to know whether
            # to tell the customer themselves.
            "auto_collected": collected,
            "push_sent": notified,
        },
        "message": f"Coupon {coupon.code} issued to {customer.full_name or customer.email}",
    }


@router.get("/promo-presets", response_model=dict)
async def list_promo_presets(
    _admin: Admin = Depends(require_permission("marketing", "read")),
):
    """
    The preset offers the Assign Promo dialog shows.

    Served from the backend so every branch offers the same terms and changing
    them is a config change, not a frontend release.
    """
    return {
        "success": True,
        "data": {
            "presets": [
                {
                    "key": key,
                    "label": p["label"],
                    "code_prefix": p["code_prefix"],
                    "discount_type": p["discount_type"],
                    "discount_value": float(p["discount_value"]),
                    "min_order_value": float(p["min_order_value"]),
                    "valid_days": p["valid_days"],
                }
                for key, p in PROMO_PRESETS.items()
            ]
        },
    }
