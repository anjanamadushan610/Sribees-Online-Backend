"""Super Admin-only acquisition promotions; customer read endpoint."""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import get_current_user, require_super_admin
from app.models.partner import Partner
from app.models.referral_promotion import ReferralPromotion
from app.services.referral_promotion_service import ReferralPromotionService as Service
from app.services.referral_promotion_service import enabled

router = APIRouter(tags=['Referral Promotions'])
Money = Decimal


class Rules(BaseModel):
    model_config = ConfigDict(extra='forbid')
    valid_days: int = Field(30, ge=1, le=365)
    free_delivery: bool = False
    delivery_uses: int | None = Field(1, ge=1, le=1000)
    min_order: Money = Field(0, ge=0, max_digits=10, decimal_places=2)
    max_delivery_fee: Money = Field(1000, gt=0, max_digits=10, decimal_places=2)
    coupon_type: Literal['fixed', 'percentage'] = 'fixed'
    coupon_value: Money = Field(0, ge=0, max_digits=10, decimal_places=2)
    coupon_cap: Money = Field(0, ge=0, max_digits=10, decimal_places=2)
    coupon_uses: int = Field(1, ge=1, le=100)

    @model_validator(mode='after')
    def sensible(self):
        if not self.free_delivery and self.coupon_value <= 0:
            raise ValueError('Choose free delivery, a coupon, or both.')
        if self.coupon_type == 'percentage' and self.coupon_value > 100:
            raise ValueError('Percentage must not exceed 100.')
        if self.coupon_value > 0 and (self.coupon_cap <= 0 or (self.coupon_type == 'fixed' and self.coupon_cap < self.coupon_value)):
            raise ValueError('Enter a coupon cap covering the fixed discount, or a positive cap for percentage discounts.')
        return self


class CampaignBody(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)
    scope: Literal['all', 'partner', 'master_team'] = 'all'
    partner_id: UUID | None = None
    starts_at: datetime
    ends_at: datetime
    active: bool = False
    max_customers: int = Field(100, ge=1, le=100000)
    delivery_budget: Money = Field(0, ge=0, max_digits=12, decimal_places=2)
    rules: Rules

    @model_validator(mode='after')
    def sensible(self):
        if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
            raise ValueError('Dates must include a timezone.')
        if self.ends_at <= self.starts_at:
            raise ValueError('End must be after start.')
        if (self.scope == 'all') != (self.partner_id is None):
            raise ValueError('Select a Partner for a specific scope, or clear it for all Partners.')
        if self.rules.free_delivery and self.delivery_budget <= 0:
            raise ValueError('Free delivery requires a campaign delivery budget.')
        return self


def require_enabled():
    if not enabled():
        raise HTTPException(404, 'Referral promotions are not enabled yet.')


def serialize(row):
    return {'id': str(row.promotion_id), 'name': row.name, 'scope': row.scope,
        'partner_id': str(row.partner_id) if row.partner_id else None, 'rules': row.rules,
        'starts_at': row.starts_at.isoformat(), 'ends_at': row.ends_at.isoformat(),
        'active': row.active, 'max_customers': row.max_customers, 'enrolled_count': row.enrolled_count,
        'delivery_budget': str(row.delivery_budget), 'delivery_spent': str(row.delivery_spent)}


@router.get('/referrals/benefits')
async def benefits(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    return {'success': True, 'data': await Service.customer_summary(db, user.user_id)}


@router.get('/admin/referral-promotions')
async def campaigns(db: AsyncSession = Depends(get_db), admin=Depends(require_super_admin)):
    # Configuration may be prepared while execution remains disabled.
    rows = (await db.execute(select(ReferralPromotion).order_by(ReferralPromotion.created_at.desc()))).scalars().all()
    return {'success': True, 'data': [serialize(r) for r in rows], 'enabled': enabled()}


async def save(db, body, row=None):
    if body.scope != 'all':
        partner = await db.get(Partner, body.partner_id)
        if not partner or not partner.is_active:
            raise HTTPException(400, 'Choose an active Partner.')
        if body.scope == 'master_team' and partner.role != 'master_partner':
            raise HTTPException(400, 'Team offers require a Master Partner.')
    now = datetime.now(timezone.utc)
    if body.active and not enabled():
        raise HTTPException(409, 'Enable the reviewed referral promotions rollout before activating a campaign.')
    if body.active and body.ends_at <= now:
        raise HTTPException(400, 'An active campaign must end in the future.')
    if row and (body.max_customers < row.enrolled_count or body.delivery_budget < row.delivery_spent):
        raise HTTPException(400, 'Limits cannot be reduced below customers already enrolled or delivery budget already spent.')
    starts_at = body.starts_at
    if body.active and (row is None or not row.active):
        starts_at = max(starts_at, now)  # no retroactive enrollment when enabled
    if body.ends_at <= starts_at:
        raise HTTPException(400, 'End must be after activation.')
    row = row or ReferralPromotion(enrolled_count=0, delivery_spent=0)
    for field in ['name', 'scope', 'partner_id', 'ends_at', 'active', 'max_customers', 'delivery_budget']:
        setattr(row, field, getattr(body, field))
    row.starts_at = starts_at
    row.rules = body.rules.model_dump(mode='json')
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {'success': True, 'data': serialize(row)}


@router.post('/admin/referral-promotions')
async def create(body: CampaignBody, db: AsyncSession = Depends(get_db), admin=Depends(require_super_admin)):
    return await save(db, body)


@router.put('/admin/referral-promotions/{promotion_id}')
async def edit(promotion_id: UUID, body: CampaignBody, db: AsyncSession = Depends(get_db), admin=Depends(require_super_admin)):
    row = (await db.execute(select(ReferralPromotion).where(ReferralPromotion.promotion_id == promotion_id)
        .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, 'Campaign not found.')
    return await save(db, body, row)
