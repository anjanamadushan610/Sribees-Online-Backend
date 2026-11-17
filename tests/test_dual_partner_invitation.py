"""Email and SMS attempts must not prevent each other from succeeding."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.api.v1.linked_partner_wallet import rollout_enabled, router
from app.config.database import get_db
from app.config.redis import get_redis
from app.core.dependencies import get_current_partner
from app.services import partner_invitation
from app.services.email_sender import EmailSendError
from app.services.partner_wallet_service import PartnerWalletService
from app.services.sms_gateway import SmsSendError


@pytest.mark.asyncio
@pytest.mark.parametrize('sms_error,email_error', [
    (None, None),
    (None, EmailSendError('Provider failure')),
    (SmsSendError('Provider failure'), None),
    (SmsSendError('Provider failure'), EmailSendError('Provider failure')),
    (HTTPException(409, 'Phone not verified'), None),
    (RuntimeError('Redis unavailable'), None),
])
async def test_dual_delivery_reports_each_channel(monkeypatch, sms_error, email_error):
    sms = AsyncMock(side_effect=sms_error)
    email = AsyncMock(side_effect=email_error)
    monkeypatch.setattr(partner_invitation, 'send_sms_invitation', sms)
    monkeypatch.setattr(partner_invitation, 'send_invitation', email)
    partner, user, redis = SimpleNamespace(), SimpleNamespace(), AsyncMock()
    result = await partner_invitation.send_dual_invitation(partner, user, redis)
    assert result == {'sms': sms_error is None, 'email': email_error is None}
    sms.assert_awaited_once_with(partner, user, redis)
    email.assert_awaited_once_with(partner)


@pytest.mark.asyncio
@pytest.mark.parametrize('sms,email', [(True, True), (True, False), (False, True), (False, False)])
async def test_upgrade_reports_both_channel_outcomes(monkeypatch, sms, email):
    master_id, user_id = uuid4(), uuid4()
    master = SimpleNamespace(role='master_partner', partner_id=master_id)
    user = SimpleNamespace(referred_by_partner_id=master_id)
    upgraded = SimpleNamespace(partner_id=uuid4(), role='partner', referral_code='TEST',
                               is_active=True, parent_partner_id=master_id)
    db, redis = AsyncMock(), AsyncMock()
    redis.set.return_value = True
    monkeypatch.setattr(PartnerWalletService, 'user_lock', AsyncMock(return_value=user))
    upgrade = AsyncMock(return_value=upgraded)
    monkeypatch.setattr(PartnerWalletService, 'master_upgrade', upgrade)
    monkeypatch.setattr(partner_invitation, 'portal_base', lambda: 'https://portal.example.test')
    send = AsyncMock(return_value={'sms': sms, 'email': email})
    monkeypatch.setattr(partner_invitation, 'send_dual_invitation', send)
    app = FastAPI()
    app.include_router(router, prefix='/api/v1')
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_redis] = lambda: redis
    app.dependency_overrides[get_current_partner] = lambda: master
    app.dependency_overrides[rollout_enabled] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post(f'/api/v1/partner/customers/{user_id}/upgrade',
                                     json={'customer_consent_confirmed': True})
    assert response.status_code == 200
    result = response.json()
    assert result['invitation_delivery'] == {'sms': sms, 'email': email}
    assert result['invitation_sent'] is (sms or email)
    assert result['invitation_channel'] == 'email_and_sms'
    upgrade.assert_awaited_once()
    send.assert_awaited_once_with(upgraded, user, redis)
    db.commit.assert_awaited_once()
