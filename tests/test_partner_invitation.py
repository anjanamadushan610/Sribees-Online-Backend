"""Invitation credentials stay separate from authentication and are single-use."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import jwt
import pytest
from fastapi import HTTPException

from app.config.settings import settings
from app.core.security import verify_password, verify_token
from app.services import partner_invitation as service


def partner():
    return SimpleNamespace(partner_id=uuid4(), user_id=None, email='partner@example.test',
                           full_name='Test <Partner>', password_hash='old-credential', is_active=True)


def token_for(row):
    return parse_qs(urlsplit(service.setup_url(row)).fragment)['token'][0]


def database(row):
    result = Mock()
    result.scalar_one_or_none.return_value = row
    return SimpleNamespace(execute=AsyncMock(return_value=result), flush=AsyncMock())


def test_invitation_is_not_an_access_token():
    row = partner()
    url = service.setup_url(row)
    assert urlsplit(url).path == '/set-password'
    assert not urlsplit(url).query
    assert verify_token(token_for(row)) is None
    assert row.email not in url


@pytest.mark.asyncio
async def test_accept_changes_password_and_consumes_link():
    row = partner()
    token = token_for(row)
    db = database(row)
    password = 'Customer chosen password 19!'
    await service.accept_invitation(db, token, password)
    assert verify_password(password, row.password_hash)
    assert row.password_changed_at is not None
    with pytest.raises(HTTPException, match='invalid or expired'):
        await service.accept_invitation(db, token, password)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['email', 'password', 'inactive', 'missing'])
async def test_changed_account_invalidates_invitation(change):
    row = partner()
    token = token_for(row)
    if change == 'email':
        row.email = 'different@example.test'
    elif change == 'password':
        row.password_hash = 'different-credential'
    elif change == 'inactive':
        row.is_active = False
    db = database(None if change == 'missing' else row)
    with pytest.raises(HTTPException):
        await service.accept_invitation(db, token, 'Customer password 19!')
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_and_wrong_purpose_tokens_are_rejected():
    db = database(partner())
    for payload in [
        {'type': service.INVITATION_TYPE, 'exp': datetime.now(timezone.utc) - timedelta(seconds=1)},
        {'type': 'access', 'exp': datetime.now(timezone.utc) + timedelta(hours=1)},
    ]:
        token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
        with pytest.raises(HTTPException):
            await service.accept_invitation(db, token, 'Customer password 19!')
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_email_uses_portal_link_and_escapes_customer_name(monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(service.email_sender, 'send_email', send)
    row = partner()
    await service.send_invitation(row)
    to, subject, text, body = send.call_args.args
    assert to == row.email
    assert 'Set up' in subject
    assert '/set-password#token=' in text
    assert '&lt;Partner&gt;' in body
    assert row.password_hash not in text


def test_rejects_insecure_portal_configuration(monkeypatch):
    monkeypatch.setattr(settings, 'partner_portal_url', 'http://example.test')
    with pytest.raises(HTTPException):
        service.setup_url(partner())


@pytest.mark.asyncio
async def test_upgrade_sends_link_and_retry_reuses_linked_account(monkeypatch):
    from app.api.v1.linked_partner_wallet import AdminUpgradeBody, upgrade_customer
    from app.services.partner_wallet_service import PartnerWalletService

    row = partner()
    row.role = 'partner'
    row.referral_code = 'TESTCODE'
    row.parent_partner_id = None
    db = database(row)
    db.commit = AsyncMock()
    redis = SimpleNamespace(set=AsyncMock(return_value=True), delete=AsyncMock())
    lock = AsyncMock(return_value=SimpleNamespace(email=row.email))
    upgrade = AsyncMock()
    send = AsyncMock()
    monkeypatch.setattr(PartnerWalletService, 'user_lock', lock)
    monkeypatch.setattr(PartnerWalletService, 'admin_upgrade', upgrade)
    monkeypatch.setattr(service.email_sender, 'is_configured', lambda: True)
    monkeypatch.setattr(service, 'send_invitation', send)
    body = AdminUpgradeBody(customer_consent_confirmed=True, note='Verified customer request')
    response = await upgrade_customer(uuid4(), body, db=db, redis=redis,
                                      admin=SimpleNamespace(admin_id=uuid4()), ready=None)
    assert response['invitation_sent'] is True
    upgrade.assert_not_awaited()
    send.assert_awaited_once_with(row)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_upgrade_reports_email_failure_without_losing_upgrade(monkeypatch):
    from app.api.v1.linked_partner_wallet import AdminUpgradeBody, upgrade_customer
    from app.services.partner_wallet_service import PartnerWalletService

    row = partner()
    row.role = 'partner'
    row.referral_code = 'TESTCODE'
    row.parent_partner_id = None
    db = database(None)
    db.commit = AsyncMock()
    redis = SimpleNamespace(set=AsyncMock(return_value=True), delete=AsyncMock())
    monkeypatch.setattr(PartnerWalletService, 'user_lock', AsyncMock())
    upgrade = AsyncMock(return_value=row)
    monkeypatch.setattr(PartnerWalletService, 'admin_upgrade', upgrade)
    monkeypatch.setattr(service.email_sender, 'is_configured', lambda: True)
    monkeypatch.setattr(service, 'send_invitation', AsyncMock(side_effect=service.email_sender.EmailSendError('Test send failure')))
    body = AdminUpgradeBody(customer_consent_confirmed=True, note='Verified customer request')
    response = await upgrade_customer(uuid4(), body, db=db, redis=redis,
                                      admin=SimpleNamespace(admin_id=uuid4()), ready=None)
    assert response['invitation_sent'] is False
    db.commit.assert_awaited_once()
    redis.delete.assert_awaited_once()
    generated_password = upgrade.call_args.args[3]
    assert len(generated_password) >= 48


@pytest.mark.asyncio
async def test_linked_customer_status_is_checked(monkeypatch):
    row = partner()
    row.user_id = uuid4()
    db = database(row)
    db.get = AsyncMock(return_value=SimpleNamespace(is_active=True, is_blocked=True, deleted_at=None))
    with pytest.raises(HTTPException):
        await service.accept_invitation(db, token_for(row), 'Customer password 19!')
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_mail_configuration_failure_does_not_upgrade(monkeypatch):
    from app.api.v1.linked_partner_wallet import AdminUpgradeBody, upgrade_customer
    from app.services.partner_wallet_service import PartnerWalletService

    upgrade = AsyncMock()
    monkeypatch.setattr(PartnerWalletService, 'admin_upgrade', upgrade)
    monkeypatch.setattr(service.email_sender, 'is_configured', lambda: False)
    db = SimpleNamespace(commit=AsyncMock())
    with pytest.raises(HTTPException) as error:
        await upgrade_customer(uuid4(), AdminUpgradeBody(customer_consent_confirmed=True, note='Verified request'),
                               db=db, redis=AsyncMock(), admin=SimpleNamespace(admin_id=uuid4()), ready=None)
    assert error.value.status_code == 503
    upgrade.assert_not_awaited()
    db.commit.assert_not_awaited()
