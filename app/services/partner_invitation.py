"""Email password setup links bound to the current Partner credentials.

Links have their own JWT type and no authentication subject. Changing the
password consumes every outstanding invitation without a separate token store.
"""
import hashlib
import hmac
import html
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

INVITATION_TYPE = 'partner_password_setup'
INVITATION_LIFETIME = timedelta(hours=24)


def credential_stamp(partner):
    return hashlib.sha256(f'{partner.email}\0{partner.password_hash}'.encode()).hexdigest()


def setup_url(partner):
    base = settings.partner_portal_url.rstrip('/')
    parsed = urlsplit(base)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
        raise HTTPException(503, 'Partner Portal email links are not configured correctly.')
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


async def accept_invitation(db, token, password):
    payload = decode_token(token)
    invalid = HTTPException(400, 'This invitation is invalid or expired. Request a new invitation or use Forgot Password.')
    if not payload or payload.get('type') != INVITATION_TYPE:
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
    if partner.user_id:
        from app.models.user import User
        user = await db.get(User, partner.user_id)
        if not user or not user.is_active or user.is_blocked or user.deleted_at:
            raise invalid
    partner.password_hash = hash_password(password)
    partner.password_changed_at = datetime.now(timezone.utc)
    await db.flush()
