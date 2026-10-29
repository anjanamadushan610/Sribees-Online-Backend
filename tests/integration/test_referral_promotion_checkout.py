"""The complete referral-free-delivery checkout with only the courier faked."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import delete, select

from app.config.settings import settings
from app.models.coupon import Coupon
from app.models.order import Order
from app.models.partner import Partner
from app.models.referral_promotion import (
    ReferralBenefit,
    ReferralDeliveryRedemption,
    ReferralPromotion,
)
from tests.integration.test_checkout_journey import _add, _order_body, _set_branch


async def test_free_delivery_quote_order_final_slot_and_cancellation(api, db, app_redis, world, make_customer, make_product, fake_courier, monkeypatch):
    monkeypatch.setenv('REFERRAL_PROMOTIONS_ENABLED', 'true')
    monkeypatch.setattr(settings, 'finance_outbox_enabled', False)
    customer = await make_customer()
    product = await make_product(price='250.00', stock=20)
    await _set_branch(app_redis, customer, world)
    now = datetime.now(timezone.utc)
    partner = Partner(email=f'{uuid4().hex}@example.com', full_name='Promo Test Partner', password_hash='unused',
        role='partner', is_active=True, referral_code=uuid4().hex[:10].upper())
    campaign = ReferralPromotion(name='Welcome delivery', scope='all', active=True, max_customers=10, enrolled_count=0,
        rules={'valid_days': 30, 'free_delivery': True, 'delivery_uses': 1, 'min_order': '100', 'max_delivery_fee': '500',
            'coupon_type': 'fixed', 'coupon_value': '50', 'coupon_cap': '50', 'coupon_uses': 1},
        starts_at=now-timedelta(hours=1), ends_at=now+timedelta(days=1), created_at=now-timedelta(hours=1), delivery_budget=1000, delivery_spent=0)
    db.add_all([partner, campaign])
    await db.commit()
    campaign_id, partner_id = campaign.promotion_id, partner.partner_id
    benefit_id = coupon_id = None
    try:
        bindings = await asyncio.gather(*(api.post('/api/v1/referrals/redeem',
            json={'referral_code': partner.referral_code}, headers=customer.headers) for _ in range(2)))
        assert sorted(r.status_code for r in bindings) == [200, 400], [r.text for r in bindings]
        benefit = (await db.execute(select(ReferralBenefit).where(ReferralBenefit.user_id == customer.user_id))).scalar_one()
        benefit_id, coupon_id = benefit.benefit_id, benefit.coupon_id
        response = await api.get('/api/v1/coupons/collected', headers=customer.headers)
        assert response.status_code == 200 and str(coupon_id) in response.text
        await _add(api, customer, product, qty=2)
        response = await api.post('/api/v1/orders/quote', json={'payment_method': 'COD', 'delivery_address_id': str(customer.address_id)}, headers=customer.headers)
        assert response.status_code == 200, response.text
        quote = response.json()['data']
        assert quote['delivery_fee'] == 0 and quote['delivery']['original_fee'] == 350
        assert quote['delivery']['promotion']
        # A raw courier cache entry must never leak one customer's benefit.
        other = await make_customer()
        response = await api.post('/api/v1/orders/quote', json={
            'payment_method': 'COD', 'delivery_address_id': str(other.address_id),
            'items': [{'product_id': str(product), 'quantity': 2, 'price': 1}]}, headers=other.headers)
        assert response.status_code == 200, response.text
        assert response.json()['data']['delivery_fee'] == 350
        body = _order_body(customer, expected_referral_free_delivery=True, is_buy_now=True,
            items=[{'product_id': str(product), 'quantity': 2, 'price': 1, 'name': 'Test'}])
        responses = await asyncio.gather(*(api.post('/api/v1/orders', json=body, headers=customer.headers) for _ in range(2)))
        assert sorted(r.status_code for r in responses) == [201, 400], [r.text for r in responses]
        created = next(r for r in responses if r.status_code == 201).json()['data']
        assert created['shipping_amount'] == 0 and created['total_amount'] == quote['total']
        db.expire_all()
        order = await db.get(Order, UUID(created['order_id']))
        assert order.courier_quote_amount == Decimal(350)
        assert order.referral_delivery_discount == Decimal(350)
        response = await api.post(f"/api/v1/orders/{order.order_id}/cancel?reason=promo+test", headers=customer.headers)
        assert response.status_code == 200, response.text
        db.expire_all()
        benefit = await db.get(ReferralBenefit, benefit_id)
        assert benefit.used_deliveries == 0
    finally:
        # These are disposable integration fixtures in the guarded test DB.
        if benefit_id:
            await db.execute(delete(ReferralDeliveryRedemption).where(ReferralDeliveryRedemption.benefit_id == benefit_id))
            await db.execute(delete(ReferralBenefit).where(ReferralBenefit.benefit_id == benefit_id))
        if coupon_id:
            await db.execute(delete(Coupon).where(Coupon.coupon_id == coupon_id))
        await db.execute(delete(ReferralPromotion).where(ReferralPromotion.promotion_id == campaign_id))
        # The world fixture owns customer/order cleanup; detach only this fixture's referrer.
        from app.models.user import User
        user = await db.get(User, customer.user_id)
        user.referred_by_partner_id = None
        await db.flush()
        from app.models.referral import ReferralCommission, ReferralCommissionLine
        commissions = select(ReferralCommission.commission_id).where(ReferralCommission.beneficiary_partner_id == partner_id)
        await db.execute(delete(ReferralCommissionLine).where(ReferralCommissionLine.commission_id.in_(commissions)))
        await db.execute(delete(ReferralCommission).where(ReferralCommission.beneficiary_partner_id == partner_id))
        await db.execute(delete(Partner).where(Partner.partner_id == partner_id))
        await db.commit()
