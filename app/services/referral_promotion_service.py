"""New-customer benefits. Snapshots never change when a campaign is edited.

Delivery reservations lock campaign -> benefit; order and reservation commit
atomically. No courier quote is fabricated or discounted: only the customer's
charge is waived and the business subsidy is recorded separately.
"""
import os
import secrets
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import or_, select

from app.models.coupon import Coupon
from app.models.order import Order
from app.models.referral_promotion import (
    ReferralBenefit,
    ReferralDeliveryRedemption,
    ReferralPromotion,
)
from app.models.user import User


def enabled():
    if os.getenv('REFERRAL_PROMOTIONS_ENABLED', 'false').lower() != 'true':
        return False
    from app.services.finance_outbox import enabled as finance_enabled
    return not finance_enabled() or os.getenv('REFERRAL_PROMOTIONS_FINANCE_READY', 'false').lower() == 'true'


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class ReferralPromotionService:
    @staticmethod
    async def enroll(db, user, partner):
        if not enabled():
            return None
        # Locks the exact customer to serialize binding and duplicate enrollment.
        user = (await db.execute(select(User).where(User.user_id == user.user_id)
            .with_for_update().execution_options(populate_existing=True))).scalar_one()
        if await db.scalar(select(ReferralBenefit.benefit_id).where(ReferralBenefit.user_id == user.user_id)):
            return None
        if await db.scalar(select(Order.order_id).where(Order.user_id == user.user_id).limit(1)):
            return None  # never grant acquisition rewards to an existing buyer
        now = datetime.now(timezone.utc)
        campaigns = (await db.execute(select(ReferralPromotion).where(
            ReferralPromotion.active.is_(True), ReferralPromotion.starts_at <= now,
            ReferralPromotion.ends_at > now,
            or_(ReferralPromotion.scope == 'all', ReferralPromotion.partner_id == partner.partner_id,
                (ReferralPromotion.scope == 'master_team') & (ReferralPromotion.partner_id == partner.parent_partner_id))
        ).order_by(ReferralPromotion.created_at.desc(), ReferralPromotion.promotion_id))).scalars().all()
        # Direct override > Master's team > common offer. Never stack campaigns.
        campaigns.sort(key=lambda p: 0 if p.partner_id == partner.partner_id else (1 if p.scope == 'master_team' else 2))
        for candidate in campaigns:
            campaign = (await db.execute(select(ReferralPromotion).where(ReferralPromotion.promotion_id == candidate.promotion_id)
                .with_for_update().execution_options(populate_existing=True))).scalar_one()
            if not campaign.active or campaign.enrolled_count >= campaign.max_customers:
                continue
            # Prevent backdating a campaign to award existing accounts.
            if utc(user.created_at) < max(utc(campaign.created_at), utc(campaign.starts_at)):
                continue
            rules = dict(campaign.rules)
            expiry = utc(user.created_at) + timedelta(days=rules['valid_days'])
            if expiry <= now:
                continue
            coupon = None
            if rules.get('coupon_value'):
                coupon = Coupon(code='REF-' + secrets.token_hex(12).upper(), description=campaign.name,
                    discount_type=rules['coupon_type'], discount_value=Decimal(rules['coupon_value']),
                    min_order_value=Decimal(rules['min_order']), max_discount_amount=Decimal(rules['coupon_cap']),
                    usage_limit=rules['coupon_uses'], per_user_limit=rules['coupon_uses'], used_count=0,
                    valid_from=now, valid_until=expiry, is_public=False, target_user_id=user.user_id,
                    target_customer_type='all', is_active=True, first_order_only=False,
                    budget_cap=Decimal(rules['coupon_cap']) * rules['coupon_uses'], budget_spent=0)
                db.add(coupon)
                await db.flush()
            benefit = ReferralBenefit(user_id=user.user_id, promotion_id=campaign.promotion_id,
                partner_id=partner.partner_id, title=campaign.name, rules=rules, expires_at=expiry,
                coupon_id=coupon.coupon_id if coupon else None)
            db.add(benefit)
            campaign.enrolled_count += 1
            await db.flush()
            return benefit
        return None

    @staticmethod
    async def delivery(db, user_id, subtotal, fee, *, lock=False):
        if not enabled() or fee is None or fee <= 0:
            return None
        benefit = (await db.execute(select(ReferralBenefit).where(ReferralBenefit.user_id == user_id))).scalar_one_or_none()
        if benefit is None:
            return None
        cq = select(ReferralPromotion).where(ReferralPromotion.promotion_id == benefit.promotion_id)
        if lock:
            cq = cq.with_for_update().execution_options(populate_existing=True)
        campaign = (await db.execute(cq)).scalar_one()
        if lock:
            benefit = (await db.execute(select(ReferralBenefit).where(ReferralBenefit.benefit_id == benefit.benefit_id)
                .with_for_update().execution_options(populate_existing=True))).scalar_one()
        rules = benefit.rules
        limit = rules.get('delivery_uses')
        if (not rules.get('free_delivery') or utc(benefit.expires_at) <= datetime.now(timezone.utc)
            or (limit is not None and benefit.used_deliveries >= limit)
            or subtotal < Decimal(rules['min_order']) or fee > Decimal(rules['max_delivery_fee'])
            or Decimal(str(campaign.delivery_spent)) + fee > Decimal(str(campaign.delivery_budget))):
            return None
        return benefit, campaign

    @staticmethod
    async def quote(db, user_id, subtotal, delivery):
        if delivery.available and await ReferralPromotionService.delivery(db, user_id, subtotal, delivery.fee):
            return replace(delivery, fee=Decimal('0.00'), original_fee=delivery.fee, promotion='Referral free delivery')
        return delivery

    @staticmethod
    async def reserve(db, order, benefit_id, amount):
        if await db.get(ReferralDeliveryRedemption, order.order_id):
            return
        result = await ReferralPromotionService.delivery(db, order.user_id, order.subtotal, amount, lock=True)
        if result is None or result[0].benefit_id != benefit_id:
            raise ValueError('Your free delivery offer changed. Refresh checkout before placing the order.')
        benefit, campaign = result
        benefit.used_deliveries += 1
        campaign.delivery_spent += amount
        db.add(ReferralDeliveryRedemption(order_id=order.order_id, benefit_id=benefit_id, amount=amount))
        await db.flush()

    @staticmethod
    async def release(db, order):
        # Only unshipped cancellations/payment failures restore a delivery use.
        # Delivered/refunded parcels have already cost the business money.
        if not getattr(order, 'referral_delivery_discount', None):
            return
        if order.courier_shipment_id or order.status not in ('cancelled', 'payment_failed'):
            return
        initial = await db.get(ReferralDeliveryRedemption, order.order_id)
        if initial is None:
            return
        benefit = await db.get(ReferralBenefit, initial.benefit_id)
        campaign = (await db.execute(select(ReferralPromotion).where(ReferralPromotion.promotion_id == benefit.promotion_id)
            .with_for_update().execution_options(populate_existing=True))).scalar_one()
        benefit = (await db.execute(select(ReferralBenefit).where(ReferralBenefit.benefit_id == initial.benefit_id)
            .with_for_update().execution_options(populate_existing=True))).scalar_one()
        row = (await db.execute(select(ReferralDeliveryRedemption).where(ReferralDeliveryRedemption.order_id == order.order_id)
            .with_for_update().execution_options(populate_existing=True))).scalar_one()
        if row.released:
            return
        benefit.used_deliveries -= 1
        campaign.delivery_spent -= row.amount
        row.released = True
        await db.flush()

    @staticmethod
    async def customer_summary(db, user_id):
        if not enabled():
            return None
        benefit = (await db.execute(select(ReferralBenefit).where(ReferralBenefit.user_id == user_id))).scalar_one_or_none()
        if benefit is None:
            return None
        coupon = await db.get(Coupon, benefit.coupon_id) if benefit.coupon_id else None
        rules = benefit.rules
        return {'title': benefit.title, 'expiresAt': benefit.expires_at.isoformat(),
            'expired': utc(benefit.expires_at) <= datetime.now(timezone.utc),
            'freeDelivery': rules['free_delivery'], 'deliveryUses': rules.get('delivery_uses'),
            'usedDeliveries': benefit.used_deliveries, 'minOrder': rules['min_order'],
            'maxDeliveryFee': rules['max_delivery_fee'], 'couponCode': coupon.code if coupon else None,
            'couponUses': rules['coupon_uses'] if coupon else 0}
