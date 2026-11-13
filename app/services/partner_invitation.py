"""Partner password setup links delivered by email or verified-phone SMS.

Email links use purpose-scoped JWTs. SMS links use short opaque credentials
whose hashes are stored in Redis. A password change invalidates both kinds.
"""
import hashlib
import hmac
import html
import json
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import UUID

import jwt
from fastapi import HTTPException
from sqlalchemy import select

from app.config.settings import settings
from app.core.security import decode_token, hash_password
from app.models.partner import Partner
from app.services import email_sender
from app.services.sms_gateway import SmsSendError, UnconfiguredGateway, get_sms_gateway
from app.utils.identity import normalize_mobile

INVITATION_TYPE = 'partner_password_setup'
INVITATION_LIFETIME = timedelta(hours=24)


def credential_stamp(partner):
    return hashlib.sha256(f'{partner.email}\0{partner.password_hash}'.encode()).hexdigest()


def portal_base():
    base = settings.partner_portal_url.rstrip('/')
    parsed = urlsplit(base)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
        raise HTTPException(503, 'Partner Portal invitation links are not configured correctly.')
    return base


def setup_url(partner):
    base = portal_base()
    token = jwt.encode({
        'type': INVITATION_TYPE,
        'partner_id': str(partner.partner_id),
        'credential_stamp': credential_stamp(partner),
        'exp': datetime.now(timezone.utc) + INVITATION_LIFETIME,
    }, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    # A fragment keeps the credential out of HTTP access logs and referrers.
    return f'{base}/set-password#token={token}'


async def send_invitation(partner):
    url = setup_url(partner)
    name = html.escape(partner.full_name)
    subject = 'Set up your Sribees Partner account'
    text = (f'Hello {partner.full_name},\n\nYour Sribees Partner account is ready. '
            f'Verify your email and choose your own password:\n\n{url}\n\n'
            'This link expires in 24 hours and can be used once. '
            'Sribees staff will never ask for your password.\n\n'
            'Need help? support@sribeesonline.com')
    body = (f'<html><body style="font-family:Arial,sans-serif;color:#202027">'
            f'<h1 style="color:#D81B60">Sribees Online</h1><h2>Partner Portal</h2>'
            f'<p>Hello {name},</p><p>Your Partner account is ready. '
            'Verify your email and choose your own password.</p>'
            f'<p><a href="{html.escape(url, quote=True)}" style="background:#D81B60;'
            'color:white;padding:14px 22px;display:inline-block;border-radius:10px">'
            'Set Password</a></p><p>This link expires in 24 hours and can be used once.</p>'
            '<p>Sribees staff will never ask for your password.</p>'
            '<p>Need help? support@sribeesonline.com</p></body></html>')
    await email_sender.send_email(partner.email, subject, text, body)


def verified_sms_phone(user):
    if not user.phone or not user.is_phone_verified:
        raise HTTPException(409, 'Verify the customer primary phone number in the app before sending a Partner invitation by SMS.')
    try:
        return normalize_mobile(user.phone)
    except ValueError:
        raise HTTPException(409, 'The customer primary phone number is invalid.') from None


def sms_gateway():
    gateway = get_sms_gateway()
    # A console provider would log the live invitation credential instead of
    # delivering it. Never permit that for password setup.
    if gateway.name != 'textlk' or isinstance(gateway, UnconfiguredGateway):
        raise HTTPException(503, 'Partner invitation SMS delivery is not configured.')
    return gateway


def sms_token_key(token):
    return 'partner:invitation:sms:' + hashlib.sha256(token.encode()).hexdigest()


async def send_sms_invitation(partner, user, redis):
    phone = verified_sms_phone(user)
    gateway = sms_gateway()
    token = 'sms_' + secrets.token_urlsafe(32)
    payload = {
        'type': INVITATION_TYPE,
        'partner_id': str(partner.partner_id),
        'credential_stamp': credential_stamp(partner),
        'phone_stamp': hashlib.sha256(phone.encode()).hexdigest(),
    }
    url = f'{portal_base()}/set-password#token={token}'
    key = sms_token_key(token)
    await redis.set(key, json.dumps(payload), ex=int(INVITATION_LIFETIME.total_seconds()))
    try:
        await gateway.send(phone, f'Sribees Partner Portal: set your password {url} '
                           'Valid 24 hours. Keep this link private.')
    except SmsSendError:
        await redis.delete(key)
        raise


async def accept_invitation(db, token, password, redis=None):
    if token.startswith('sms_'):
        raw = await redis.get(sms_token_key(token)) if redis is not None else None
        try:
            payload = json.loads(raw) if raw else None
        except (ValueError, TypeError):
            payload = None
    else:
        payload = decode_token(token)
    invalid = HTTPException(400, 'This invitation is invalid or expired. Request a new invitation or use Forgot Password.')
    if not isinstance(payload, dict) or payload.get('type') != INVITATION_TYPE:
        raise invalid
    try:
        partner_id = UUID(payload.get('partner_id', ''))
    except (ValueError, TypeError, AttributeError):
        raise invalid from None
    partner = (await db.execute(select(Partner).where(
        Partner.partner_id == partner_id).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if not partner or not partner.is_active or not hmac.compare_digest(
        str(payload.get('credential_stamp', '')), credential_stamp(partner)
    ):
        raise invalid
    if payload.get('phone_stamp') and not partner.user_id:
        raise invalid
    if partner.user_id:
        from app.models.user import User
        user = await db.get(User, partner.user_id)
        if not user or not user.is_active or user.is_blocked or user.deleted_at:
            raise invalid
        if payload.get('phone_stamp'):
            phone = verified_sms_phone(user)
            if not hmac.compare_digest(payload['phone_stamp'], hashlib.sha256(phone.encode()).hexdigest()):
                raise invalid
    partner.password_hash = hash_password(password)
    partner.password_changed_at = datetime.now(timezone.utc)
    await db.flush()
