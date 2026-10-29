"""Acquisition-only grants and atomic delivery reservations; no live money."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.v1.referral_promotions import CampaignBody, Rules
from app.models.coupon import Coupon
from app.models.order import Order
from app.models.partner import Partner
from app.models.referral_promotion import ReferralBenefit, ReferralPromotion
from app.models.user import User
from app.services.pricing_service import DeliveryQuote, PricingService
from app.services.referral_promotion_service import ReferralPromotionService as Service
from app.services.referral_promotion_service import enabled
from tests.conftest import requires_db


def test_rules_require_real_benefit_caps_dates_and_scoped_partner():
    with pytest.raises(ValidationError):
        Rules()
    for values in [{'coupon_value': 101, 'coupon_type': 'percentage', 'coupon_cap': 200},
                   {'coupon_value': 100, 'coupon_cap': 0}, {'free_delivery': True, 'delivery_uses': 0}]:
        with pytest.raises(ValidationError):
            Rules(**values)
    assert Rules(free_delivery=True, delivery_uses=None, valid_days=30).delivery_uses is None
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError):
        CampaignBody(name='Offer', scope='partner', starts_at=now, ends_at=now + timedelta(days=1), rules=Rules(free_delivery=True), delivery_budget=1000)


def test_rollout_defaults_off_and_requires_finance_confirmation(monkeypatch):
    from app.config.settings import settings
    monkeypatch.delenv('REFERRAL_PROMOTIONS_ENABLED', raising=False)
    assert not enabled()
    monkeypatch.setenv('REFERRAL_PROMOTIONS_ENABLED', 'true')
    monkeypatch.setattr(settings, 'finance_outbox_enabled', True)
    monkeypatch.delenv('REFERRAL_PROMOTIONS_FINANCE_READY', raising=False)
    assert not enabled()
    monkeypatch.setenv('REFERRAL_PROMOTIONS_FINANCE_READY', 'true')
    assert enabled()


@pytest.fixture
async def promo_db(db_schema):
    from app.config.settings import settings

    engine = create_async_engine(settings.async_database_url, poolclass=NullPool)
    async with engine.connect() as connection:
        outer = await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False, join_transaction_mode='create_savepoint') as db:
            user = User(email=f'{uuid4().hex}@example.com', full_name='Promo Customer', is_active=True,
                is_verified=True, is_blocked=False)
            db.add(user)
            await db.flush()
            yield db, user
        await outer.rollback()
    await engine.dispose()


async def setup_offer(db, user, monkeypatch, **overrides):
    from app.config.settings import settings
    monkeypatch.setenv('REFERRAL_PROMOTIONS_ENABLED', 'true')
    monkeypatch.setattr(settings, 'finance_outbox_enabled', False)
    partner = Partner(email=f'{uuid4().hex}@example.com', full_name='Test Partner', password_hash='unused', role='partner', is_active=True, referral_code=uuid4().hex[:10])
    db.add(partner)
    now = datetime.now(timezone.utc)
    campaign = ReferralPromotion(name='Welcome', scope='all', active=True, max_customers=10, enrolled_count=0,
        rules=Rules(free_delivery=True, delivery_uses=1, coupon_value=100, coupon_cap=100).model_dump(mode='json'),
        starts_at=now - timedelta(hours=1), ends_at=now + timedelta(days=2),
        created_at=now - timedelta(hours=1), delivery_budget=1000, delivery_spent=0)
    for key, value in overrides.items():
        setattr(campaign, key, value)
    db.add(campaign)
    await db.flush()
    return campaign, partner


@requires_db
@pytest.mark.asyncio
async def test_new_customer_grant_is_once_private_coupon_and_snapshot(promo_db, monkeypatch):
    db, user = promo_db
    campaign, partner = await setup_offer(db, user, monkeypatch)
    benefit = await Service.enroll(db, user, partner)
    assert benefit is not None
    coupon = await db.get(Coupon, benefit.coupon_id)
    assert coupon.target_user_id == user.user_id and coupon.is_public is False
    assert coupon.usage_limit == coupon.per_user_limit == 1
    assert await Service.enroll(db, user, partner) is None
    campaign.rules = Rules(free_delivery=True, delivery_uses=5).model_dump(mode='json')
    await db.flush()
    assert benefit.rules['delivery_uses'] == 1
    assert campaign.enrolled_count == 1


@requires_db
@pytest.mark.asyncio
async def test_old_customers_and_existing_buyers_do_not_get_acquisition_offer(promo_db, monkeypatch):
    db, user = promo_db
    _, partner = await setup_offer(db, user, monkeypatch)
    original = user.created_at
    user.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=2)
    await db.flush()
    assert await Service.enroll(db, user, partner) is None
    user.created_at = original
    db.add(Order(user_id=user.user_id, order_number=f'TEST-{uuid4().hex}', subtotal=100, total_amount=100, status='cancelled'))
    await db.flush()
    assert await Service.enroll(db, user, partner) is None


@requires_db
@pytest.mark.asyncio
async def test_direct_offer_overrides_master_team_and_common_without_stacking(promo_db, monkeypatch):
    db, user = promo_db
    campaign, partner = await setup_offer(db, user, monkeypatch)
    master = Partner(email=f'{uuid4().hex}@example.com', full_name='Test Master', password_hash='unused', role='master_partner', is_active=True)
    db.add(master)
    await db.flush()
    partner.parent_partner_id = master.partner_id
    for scope, target in [('master_team', master.partner_id), ('partner', partner.partner_id)]:
        db.add(ReferralPromotion(name=scope, scope=scope, partner_id=target, active=True, max_customers=10,
            enrolled_count=0, rules=campaign.rules, starts_at=campaign.starts_at, ends_at=campaign.ends_at,
            created_at=campaign.created_at, delivery_budget=1000, delivery_spent=0))
    await db.flush()
    benefit = await Service.enroll(db, user, partner)
    assert benefit.title == 'partner'
    assert len((await db.execute(select(ReferralBenefit).where(ReferralBenefit.user_id == user.user_id))).scalars().all()) == 1


@requires_db
@pytest.mark.asyncio
async def test_quote_reservation_release_and_delivery_limits(promo_db, monkeypatch):
    db, user = promo_db
    campaign, partner = await setup_offer(db, user, monkeypatch)
    benefit = await Service.enroll(db, user, partner)
    raw = DeliveryQuote(status='quoted', fee=Decimal('250.00'), quote_id='courier-test')
    quoted = await Service.quote(db, user.user_id, Decimal('1000'), raw)
    assert quoted.fee == 0 and quoted.original_fee == 250 and raw.fee == 250
    assert quoted.quote_id == raw.quote_id
    price = await PricingService.quote(items=[{'price': 1000, 'quantity': 1}], delivery=quoted, tax_rate=Decimal(0), use_wallet=True, wallet_balance=Decimal(1200))
    assert price.wallet_deduction == 1000 and price.total == 0
    order = Order(user_id=user.user_id, order_number=f'TEST-{uuid4().hex}', subtotal=1000, total_amount=1000,
        status='pending', referral_delivery_discount=250)
    db.add(order)
    await db.flush()
    await Service.reserve(db, order, benefit.benefit_id, Decimal(250))
    await Service.reserve(db, order, benefit.benefit_id, Decimal(250))
    assert benefit.used_deliveries == 1 and campaign.delivery_spent == 250
    assert await Service.delivery(db, user.user_id, Decimal(1000), Decimal(250)) is None
    order.status = 'cancelled'
    await Service.release(db, order)
    await db.flush()
    await Service.release(db, order)
    assert benefit.used_deliveries == 0 and campaign.delivery_spent == 0


@requires_db
@pytest.mark.asyncio
async def test_expiry_budget_minimum_and_fee_cap_refuse_free_delivery(promo_db, monkeypatch):
    db, user = promo_db
    campaign, partner = await setup_offer(db, user, monkeypatch)
    benefit = await Service.enroll(db, user, partner)
    benefit.rules = {**benefit.rules, 'min_order': '500', 'max_delivery_fee': '300', 'delivery_uses': None}
    await db.flush()
    assert await Service.delivery(db, user.user_id, Decimal(400), Decimal(250)) is None
    assert await Service.delivery(db, user.user_id, Decimal(1000), Decimal(350)) is None
    campaign.delivery_spent = 900
    await db.flush()
    assert await Service.delivery(db, user.user_id, Decimal(1000), Decimal(250)) is None
    campaign.delivery_spent = 0
    benefit.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.flush()
    assert await Service.delivery(db, user.user_id, Decimal(1000), Decimal(250)) is None
