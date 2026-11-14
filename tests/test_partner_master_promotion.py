"""Promotion preserves ownership and pays only the order's immediate referrers."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from app.api.v1.admin_partners import router
from app.config.database import get_db
from app.core.dependencies import get_current_admin
from app.models.admin import AdminRole
from app.models.user import User
from app.schemas.partner import PartnerPromotionRequest
from app.services.partner_service import PartnerService
from app.services.referral_commission import LevelPolicy, SaleLine
from app.services.referral_service import ReferralService
from tests.test_linked_partner_wallet import money_db as money_db


@pytest.mark.asyncio
async def test_promotion_rejects_non_super_admin_before_mutating(monkeypatch):
    app = FastAPI()
    app.include_router(router, prefix="/admin/partners")
    db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_admin] = lambda: SimpleNamespace(role=AdminRole.CUSTOMER_SUPPORT, admin_id=uuid4())
    promote = AsyncMock()
    monkeypatch.setattr(PartnerService, 'promote_to_master', promote)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post(f'/admin/partners/{uuid4()}/promote', json={'note': 'Approved after review'})
    assert response.status_code == 403
    promote.assert_not_awaited()
    db.commit.assert_not_awaited()


def test_promotion_requires_a_real_reason():
    with pytest.raises(ValidationError):
        PartnerPromotionRequest(note='       ')


@pytest.mark.asyncio
async def test_promotion_is_idempotent_and_audits_once():
    parent_id, partner_id, admin_id = uuid4(), uuid4(), uuid4()
    partner = SimpleNamespace(partner_id=partner_id, role='partner', is_active=True, user_id=None, parent_partner_id=parent_id)
    db = AsyncMock()
    db.execute.return_value = MagicMock(scalar_one_or_none=lambda: partner)
    db.add = MagicMock()
    result = await PartnerService.promote_to_master(db, partner_id, admin_id, 'Approved after review')
    assert result.role == 'master_partner'
    assert result.parent_partner_id == parent_id
    event = db.add.call_args.args[0]
    assert event.action == 'partner.promote_to_master' and event.actor_id == admin_id
    assert event.event_metadata['parent_partner_id'] == str(parent_id)
    await PartnerService.promote_to_master(db, partner_id, admin_id, 'Retried after review')
    assert db.add.call_count == 1


@pytest.mark.asyncio
async def test_inactive_partner_cannot_be_promoted():
    db = AsyncMock()
    db.execute.return_value = MagicMock(scalar_one_or_none=lambda: SimpleNamespace(is_active=False))
    with pytest.raises(HTTPException) as error:
        await PartnerService.promote_to_master(db, uuid4(), uuid4(), 'Approved after review')
    assert error.value.status_code == 409
    db.flush.assert_not_awaited()


@pytest.mark.parametrize('direct_role', ['partner', 'master_partner'])
@pytest.mark.parametrize('share,expected', [('0.25', '25.00'), ('0.30', '30.00')])
@pytest.mark.asyncio
async def test_customer_order_pays_direct_referrer_and_immediate_parent_only(monkeypatch, direct_role, share, expected):
    # With A -> B (Master) -> C, C's customer pays C + B. A earns nothing.
    # With A -> B (Master), B's own customer pays B + A.
    buyer_id, direct_id, parent_id, grandparent_id = uuid4(), uuid4(), uuid4(), uuid4()
    buyer = SimpleNamespace(user_id=buyer_id, referred_by_partner_id=direct_id, referred_by_user_id=None)
    direct = SimpleNamespace(partner_id=direct_id, role=direct_role, parent_partner_id=parent_id)
    db = AsyncMock()
    db.execute.return_value = MagicMock(scalar_one_or_none=lambda: None)
    db.get.side_effect = lambda model, identity: buyer if model is User else direct
    sale = SaleLine(uuid4(), uuid4(), 'Test groceries', 1, Decimal('1000'), Decimal('0'), Decimal('10'))
    monkeypatch.setattr(ReferralService, '_sale_lines', AsyncMock(return_value=[sale]))
    add = AsyncMock(side_effect=lambda *args, **kwargs: kwargs)
    monkeypatch.setattr(ReferralService, '_add_commission', add)
    rows = await ReferralService.accrue_for_order(db, SimpleNamespace(order_id=uuid4(), user_id=buyer_id), policy=LevelPolicy(Decimal(share)))
    assert [(row['beneficiary_partner_id'], row['level']) for row in rows] == [(direct_id, 1), (parent_id, 2)]
    assert sum(line.amount for line in rows[0]['lines']) == Decimal('100.00')
    assert sum(line.amount for line in rows[1]['lines']) == Decimal(expected)
    assert all(row['beneficiary_partner_id'] != grandparent_id for row in rows)
    assert db.get.await_count == 2  # The ancestor chain is never traversed.


