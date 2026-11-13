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


def verified_customer():
    return SimpleNamespace(phone='+94770000000', is_phone_verified=True,
                           is_active=True, is_blocked=False, deleted_at=None)


@pytest.mark.asyncio
async def test_sms_link_is_short_hashed_expiring_and_single_use(monkeypatch):
    from fakeredis.aioredis import FakeRedis

    redis = FakeRedis()
    row = partner()
    row.user_id = uuid4()
    user = verified_customer()
    gateway = SimpleNamespace(name='textlk', send=AsyncMock())
    monkeypatch.setattr(service, 'get_sms_gateway', lambda: gateway)
    await service.send_sms_invitation(row, user, redis)
    phone, message = gateway.send.call_args.args
    assert phone == user.phone
    url = next(word for word in message.split() if word.startswith('https://'))
    token = parse_qs(urlsplit(url).fragment)['token'][0]
    assert token.startswith('sms_') and len(token) < 64
    key = service.sms_token_key(token)
    assert token not in key
    assert 0 < await redis.ttl(key) <= 86400
    stored = await redis.get(key)
    assert token.encode() not in stored
    assert user.phone.encode() not in stored
    db = database(row)
    db.get = AsyncMock(return_value=user)
    await service.accept_invitation(db, token, 'Customer password 19!', redis=redis)
    assert verify_password('Customer password 19!', row.password_hash)
    with pytest.raises(HTTPException):
        await service.accept_invitation(db, token, 'Different password 19!', redis=redis)
    await redis.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['phone', 'verification', 'expired'])
async def test_sms_invitation_rejects_changed_phone_and_expired_token(monkeypatch, change):
    from fakeredis.aioredis import FakeRedis

    redis = FakeRedis()
    row = partner()
    row.user_id = uuid4()
    user = verified_customer()
    gateway = SimpleNamespace(name='textlk', send=AsyncMock())
    monkeypatch.setattr(service, 'get_sms_gateway', lambda: gateway)
    await service.send_sms_invitation(row, user, redis)
    message = gateway.send.call_args.args[1]
    url = next(word for word in message.split() if word.startswith('https://'))
    token = parse_qs(urlsplit(url).fragment)['token'][0]
    if change == 'phone':
        user.phone = '+94770000001'
    elif change == 'verification':
        user.is_phone_verified = False
    else:
        await redis.delete(service.sms_token_key(token))
    db = database(row)
    db.get = AsyncMock(return_value=user)
    with pytest.raises(HTTPException):
        await service.accept_invitation(db, token, 'Customer password 19!', redis=redis)
    db.flush.assert_not_awaited()
    await redis.aclose()


@pytest.mark.asyncio
async def test_sms_send_failure_burns_token(monkeypatch):
    from fakeredis.aioredis import FakeRedis

    redis = FakeRedis()
    gateway = SimpleNamespace(name='textlk', send=AsyncMock(side_effect=service.SmsSendError('Test failure')))
    monkeypatch.setattr(service, 'get_sms_gateway', lambda: gateway)
    with pytest.raises(service.SmsSendError):
        await service.send_sms_invitation(partner(), verified_customer(), redis)
    assert await redis.keys('partner:invitation:sms:*') == []
    await redis.aclose()


def test_sms_never_uses_console_or_unverified_number(monkeypatch):
    from app.services.sms_gateway import ConsoleSmsGateway

    monkeypatch.setattr(service, 'get_sms_gateway', lambda: ConsoleSmsGateway())
    with pytest.raises(HTTPException) as error:
        service.sms_gateway()
    assert error.value.status_code == 503
    user = verified_customer()
    user.is_phone_verified = False
    with pytest.raises(HTTPException):
        service.verified_sms_phone(user)


@pytest.mark.asyncio
async def test_sms_upgrade_works_when_email_is_unavailable(monkeypatch):
    from app.api.v1.linked_partner_wallet import AdminUpgradeBody, upgrade_customer
    from app.services.partner_wallet_service import PartnerWalletService

    row = partner()
    row.role = 'partner'
    row.referral_code = 'TESTCODE'
    row.parent_partner_id = None
    user = verified_customer()
    user.email = row.email
    db = database(row)
    db.commit = AsyncMock()
    redis = SimpleNamespace(set=AsyncMock(return_value=True), delete=AsyncMock())
    monkeypatch.setattr(PartnerWalletService, 'user_lock', AsyncMock(return_value=user))
    monkeypatch.setattr(service.email_sender, 'is_configured', lambda: False)
    monkeypatch.setattr(service, 'sms_gateway', lambda: SimpleNamespace(name='textlk'))
    send_sms = AsyncMock()
    send_email = AsyncMock()
    monkeypatch.setattr(service, 'send_sms_invitation', send_sms)
    monkeypatch.setattr(service, 'send_invitation', send_email)
    body = AdminUpgradeBody(invitation_channel='sms', customer_consent_confirmed=True, note='Verified request')
    response = await upgrade_customer(uuid4(), body, db=db, redis=redis,
                                      admin=SimpleNamespace(admin_id=uuid4()), ready=None)
    assert response['invitation_sent'] is True
    assert response['invitation_channel'] == 'sms'
    send_sms.assert_awaited_once_with(row, user, redis)
    send_email.assert_not_awaited()


@pytest.mark.asyncio
async def test_unverified_sms_upgrade_does_not_mutate_account(monkeypatch):
    from app.api.v1.linked_partner_wallet import AdminUpgradeBody, upgrade_customer
    from app.services.partner_wallet_service import PartnerWalletService

    user = verified_customer()
    user.is_phone_verified = False
    monkeypatch.setattr(PartnerWalletService, 'user_lock', AsyncMock(return_value=user))
    upgrade = AsyncMock()
    monkeypatch.setattr(PartnerWalletService, 'admin_upgrade', upgrade)
    db = SimpleNamespace(commit=AsyncMock())
    body = AdminUpgradeBody(invitation_channel='sms', customer_consent_confirmed=True, note='Verified request')
    with pytest.raises(HTTPException) as error:
        await upgrade_customer(uuid4(), body, db=db, redis=AsyncMock(),
                               admin=SimpleNamespace(admin_id=uuid4()), ready=None)
    assert error.value.status_code == 409
    upgrade.assert_not_awaited()
    db.commit.assert_not_awaited()
