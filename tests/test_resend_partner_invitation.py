"""Existing-account invitations are scoped, rate limited and never recreate accounts."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api.v1.linked_partner_wallet import rollout_enabled, router
from app.config.database import get_db
from app.config.redis import get_redis
from app.core.dependencies import get_current_partner
from app.services import partner_invitation
from app.services.partner_wallet_service import PartnerWalletService


@pytest.fixture
def resend_context(monkeypatch):
    master_id, user_id = uuid4(), uuid4()
    master = SimpleNamespace(role='master_partner', partner_id=master_id)
    user = SimpleNamespace(referred_by_partner_id=master_id)
    linked = SimpleNamespace(partner_id=uuid4(), parent_partner_id=master_id, role='partner',
                             is_active=True, referral_code='EXISTING', password_hash='existing-hash',
                             password_changed_at=None, last_login=None)
    db, redis = AsyncMock(), AsyncMock()
    redis.set.return_value = True
    monkeypatch.setattr(PartnerWalletService, 'user_lock', AsyncMock(return_value=user))
    lookup = AsyncMock(return_value=linked)
    monkeypatch.setattr(PartnerWalletService, 'linked', lookup)
    upgrade = AsyncMock()
    monkeypatch.setattr(PartnerWalletService, 'master_upgrade', upgrade)
    monkeypatch.setattr(partner_invitation, 'portal_base', lambda: 'https://portal.example.test')
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(partner_invitation, 'send_invitation_channel', send)
    app = FastAPI()
    app.include_router(router, prefix='/api/v1')
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_redis] = lambda: redis
    app.dependency_overrides[get_current_partner] = lambda: master
    app.dependency_overrides[rollout_enabled] = lambda: None
    return SimpleNamespace(app=app, db=db, redis=redis, master=master, user=user, linked=linked,
                           lookup=lookup, upgrade=upgrade, send=send, user_id=user_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('role', ['partner', 'master_partner'])
@pytest.mark.parametrize('channel', ['sms', 'email'])
@pytest.mark.parametrize('sent', [True, False])
async def test_resend_uses_existing_account(resend_context, role, channel, sent):
    ctx = resend_context
    ctx.linked.role = role
    ctx.send.return_value = sent
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ctx.app), base_url='http://test') as client:
        response = await client.post(f'/api/v1/partner/customers/{ctx.user_id}/invitation',
                                     json={'invitation_channel': channel})
    assert response.status_code == 200
    assert response.json()['invitation_delivery'] == {
        'sms': sent if channel == 'sms' else False, 'email': sent if channel == 'email' else False,
    }
    assert response.json()['invitation_channel'] == channel
    assert response.json()['invitation_sent'] is sent
    ctx.send.assert_awaited_once_with(ctx.linked, ctx.user, ctx.redis, channel)
    ctx.upgrade.assert_not_awaited()
    assert ctx.linked.password_hash == 'existing-hash'
    ctx.redis.set.assert_awaited_once_with(
        f'partner:invitation:wait:{ctx.master.partner_id}:{ctx.user_id}', '1', ex=60, nx=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('case,status', [
    ('ordinary_partner', 403), ('other_customer', 404), ('other_team', 404),
    ('unlinked', 404), ('inactive', 409), ('cooldown', 429),
    ('completed', 409), ('logged_in', 409),
])
async def test_resend_enforces_access_and_cooldown(resend_context, case, status):
    ctx = resend_context
    if case == 'ordinary_partner':
        ctx.master.role = 'partner'
    elif case == 'other_customer':
        ctx.user.referred_by_partner_id = uuid4()
    elif case == 'other_team':
        ctx.linked.parent_partner_id = uuid4()
    elif case == 'unlinked':
        ctx.lookup.return_value = None
    elif case == 'inactive':
        ctx.linked.is_active = False
    elif case == 'completed':
        ctx.linked.password_changed_at = datetime.now(timezone.utc)
    elif case == 'logged_in':
        ctx.linked.last_login = datetime.now(timezone.utc)
    elif case == 'cooldown':
        ctx.redis.set.return_value = False
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ctx.app), base_url='http://test') as client:
        response = await client.post(f'/api/v1/partner/customers/{ctx.user_id}/invitation',
                                     json={'invitation_channel': 'sms'})
    assert response.status_code == status
    ctx.send.assert_not_awaited()
    ctx.upgrade.assert_not_awaited()
    ctx.db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [{}, {'invitation_channel': 'both'}, {'invitation_channel': 'fax'}])
async def test_resend_requires_one_supported_channel(resend_context, payload):
    ctx = resend_context
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ctx.app), base_url='http://test') as client:
        response = await client.post(f'/api/v1/partner/customers/{ctx.user_id}/invitation', json=payload)
    assert response.status_code == 422
    ctx.send.assert_not_awaited()
