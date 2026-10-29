"""Snapshot-based new-customer referral benefits and delivery reservations."""
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.config.database import Base


class ReferralPromotion(Base):
    __tablename__ = 'referral_promotions'
    promotion_id = sa.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = sa.Column(sa.String(120), nullable=False)
    partner_id = sa.Column(UUID(as_uuid=True), sa.ForeignKey('partners.partner_id', ondelete='RESTRICT'), nullable=True)
    # all = all Partner codes; master_team also covers a Master's direct Partners.
    scope = sa.Column(sa.String(20), nullable=False, default='all')
    rules = sa.Column(JSONB, nullable=False)
    starts_at = sa.Column(sa.DateTime(timezone=True), nullable=False)
    ends_at = sa.Column(sa.DateTime(timezone=True), nullable=False)
    active = sa.Column(sa.Boolean, nullable=False, default=False)
    max_customers = sa.Column(sa.Integer, nullable=False)
    enrolled_count = sa.Column(sa.Integer, nullable=False, default=0)
    delivery_budget = sa.Column(sa.Numeric(14, 2), nullable=False, default=0)
    delivery_spent = sa.Column(sa.Numeric(14, 2), nullable=False, default=0)
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


class ReferralBenefit(Base):
    __tablename__ = 'referral_benefits'
    benefit_id = sa.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = sa.Column(UUID(as_uuid=True), sa.ForeignKey('users.user_id', ondelete='RESTRICT'), nullable=False, unique=True)
    promotion_id = sa.Column(UUID(as_uuid=True), sa.ForeignKey('referral_promotions.promotion_id', ondelete='RESTRICT'), nullable=False)
    partner_id = sa.Column(UUID(as_uuid=True), sa.ForeignKey('partners.partner_id', ondelete='RESTRICT'), nullable=False)
    title = sa.Column(sa.String(120), nullable=False)
    rules = sa.Column(JSONB, nullable=False)
    expires_at = sa.Column(sa.DateTime(timezone=True), nullable=False)
    used_deliveries = sa.Column(sa.Integer, nullable=False, default=0)
    coupon_id = sa.Column(UUID(as_uuid=True), sa.ForeignKey('coupons.coupon_id', ondelete='RESTRICT'), nullable=True)
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


class ReferralDeliveryRedemption(Base):
    __tablename__ = 'referral_delivery_redemptions'
    order_id = sa.Column(UUID(as_uuid=True), sa.ForeignKey('orders.order_id', ondelete='RESTRICT'), primary_key=True)
    benefit_id = sa.Column(UUID(as_uuid=True), sa.ForeignKey('referral_benefits.benefit_id', ondelete='RESTRICT'), nullable=False)
    amount = sa.Column(sa.Numeric(12, 2), nullable=False)
    released = sa.Column(sa.Boolean, nullable=False, default=False)
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
