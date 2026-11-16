"""Persist the pool split and freeze it across promotion and replay."""
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.order import Order
from app.models.partner import Partner
from app.models.referral import ReferralCommission
from app.models.user import User
from app.services.referral_commission import LevelPolicy, SaleLine
from app.services.referral_service import ReferralService
from tests.conftest import requires_db
from tests.test_linked_partner_wallet import money_db  # noqa: F401


@requires_db
@pytest.mark.asyncio
@pytest.mark.parametrize('share', ['0', '0.25', '0.5', '1'])
async def test_only_the_immediate_parent_shares_the_product_budget(
    money_db,  # noqa: F811 - shared pytest fixture
    monkeypatch,
    share,
):
    db, _ = money_db
    def partner(name, parent=None):
        return Partner(full_name=name, email=f'{uuid4().hex}@example.test', password_hash='unused',
            referral_code=uuid4().hex[:10], role='master_partner' if name != 'C' else 'partner',
            parent_partner_id=parent, is_active=True)
    a = partner('A')
    db.add(a)
    await db.flush()
    b = partner('B', a.partner_id)
    db.add(b)
    await db.flush()
    c = partner('C', b.partner_id)
    db.add(c)
    await db.flush()
    sale = SaleLine(order_item_id=None, product_id=uuid4(), product_name='Pool product',
        quantity=1, gross=Decimal('1000'), coupon_discount=Decimal('0'), rate=Decimal('10'))
    monkeypatch.setattr(ReferralService, '_sale_lines', AsyncMock(return_value=[sale]))
    for direct, parent in [(b, a), (c, b)]:
        buyer = User(full_name='Test customer', email=f'{uuid4().hex}@example.test',
            referred_by_partner_id=direct.partner_id)
        db.add(buyer)
        await db.flush()
        order = Order(user_id=buyer.user_id, order_number=f'POOL-{uuid4().hex[:12]}',
            subtotal=1000, total_amount=1000, status='pending')
        db.add(order)
        await db.flush()
        rows = await ReferralService.accrue_for_order(db, order, policy=LevelPolicy(Decimal(share)))
        ratio = Decimal(share)
        pool_master_share = ratio / (Decimal('1') + ratio)
        expected_master = Decimal('100') * pool_master_share
        assert sum((row.amount for row in rows), Decimal('0')) == Decimal('100')
        by_partner = {row.beneficiary_partner_id: row.amount for row in rows}
        assert by_partner.get(direct.partner_id, Decimal('0')) == Decimal('100') - expected_master
        assert by_partner.get(parent.partner_id, Decimal('0')) == expected_master
        assert set(by_partner) <= {direct.partner_id, parent.partner_id}
        if direct == c:
            assert a.partner_id not in by_partner
        for row in rows:
            for line in row.lines:
                assert line.pool_rate_percentage == Decimal('10')
                assert line.pool_master_share == pool_master_share
        # Replays stay idempotent even if the configured ratio changes.
        assert await ReferralService.accrue_for_order(db, order, policy=LevelPolicy(Decimal('0.3'))) == []
        persisted = (await db.scalars(select(ReferralCommission).where(
            ReferralCommission.order_id == order.order_id).options(selectinload(ReferralCommission.lines)))).all()
        assert sum((row.amount for row in persisted), Decimal('0')) == Decimal('100')
