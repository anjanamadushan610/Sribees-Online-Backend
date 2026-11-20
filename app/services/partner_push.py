"""
SRIBEESonline — pushes to the SRIBEES Partner app.

Owner, 2026-11-20: a partner hears about three things, and only these:

* ``customer_joined``  — someone joined with their invite code;
* ``earnings_ready``   — referral earnings became claimable (one push per
  vesting sweep per partner, with the total, never one per commission);
* ``withdrawal_paid`` / ``withdrawal_rejected`` — an admin decided a
  withdrawal request.

Never raises. Every caller has already committed the money or the binding;
a push is news about it, and a failure here must not look like a failure of
the thing it reports. The customer's name is never sent — a lock screen is
not private — only counts and amounts.
"""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Iterable, Optional
from uuid import UUID

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.notification_i18n import money, normalize_lang

# Same name as the Android channel the app creates (lib/core/push.dart).
PARTNER_CHANNEL_ID = "sribees_partner"
BRAND_TITLE = "SRIBEES Partner"

TEMPLATES: Dict[str, Dict[str, str]] = {
    "customer_joined": {
        "en": "A new customer joined with your invite code. Their orders now earn you commission.",
        "si": "ඔබේ invite code එකෙන් අලුත් customer කෙනෙක් එකතු වුණා. ඔවුන්ගේ ඇණවුම් වලින් දැන් ඔබට commission ලැබෙනවා.",
        "ta": "உங்கள் அழைப்புக் குறியீட்டுடன் புதிய வாடிக்கையாளர் இணைந்துள்ளார். அவர்களின் ஆர்டர்கள் உங்களுக்கு கமிஷன் தரும்.",
    },
    "earnings_ready": {
        "en": "{amount} from {orders} order(s) is now ready in your Partner wallet.",
        "si": "ඇණවුම් {orders} කින් {amount} දැන් ඔබේ Partner wallet එකේ ගන්න පුළුවන්.",
        "ta": "{orders} ஆர்டர்களிலிருந்து {amount} இப்போது உங்கள் Partner wallet-இல் தயாராக உள்ளது.",
    },
    "withdrawal_paid": {
        "en": "Your withdrawal of {amount} has been paid to your bank account.",
        "si": "ඔබේ {amount} මුදල් ආපසු ගැනීම ඔබේ බැංකු ගිණුමට ගෙව්වා.",
        "ta": "உங்கள் {amount} பணம் எடுப்பு உங்கள் வங்கிக் கணக்கில் செலுத்தப்பட்டது.",
    },
    "withdrawal_rejected": {
        "en": "Your withdrawal of {amount} was not approved. The money is back in your Partner wallet.",
        "si": "ඔබේ {amount} මුදල් ආපසු ගැනීම අනුමත කළේ නැහැ. මුදල නැවත ඔබේ Partner wallet එකට දැම්මා.",
        "ta": "உங்கள் {amount} பணம் எடுப்பு அங்கீகரிக்கப்படவில்லை. பணம் மீண்டும் உங்கள் Partner wallet-இல் உள்ளது.",
    },
}

# Where a tap lands in the app (lib/core/push.dart routes on it).
SCREENS = {
    "customer_joined": "customers",
    "earnings_ready": "wallet",
    "withdrawal_paid": "wallet",
    "withdrawal_rejected": "wallet",
}


def render(kind: str, lang: Optional[str], **values) -> str:
    template = TEMPLATES[kind]
    return template.get(normalize_lang(lang), template["en"]).format(**values)


async def register_device(
    db: AsyncSession,
    partner_id: UUID,
    token: str,
    platform: str,
    locale: str,
    app_version: Optional[str],
) -> None:
    """Upsert by token: a phone belongs to whoever signed in on it last."""
    from app.models.partner_device import PartnerDevice

    now = datetime.now(timezone.utc)
    row = (
        await db.execute(
            select(PartnerDevice).where(PartnerDevice.token == token).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        db.add(
            PartnerDevice(
                partner_id=partner_id, token=token, platform=platform,
                locale=normalize_lang(locale), app_version=app_version,
                is_active=True, created_at=now, last_seen_at=now,
            )
        )
    else:
        row.partner_id = partner_id
        row.platform = platform
        row.locale = normalize_lang(locale)
        row.app_version = app_version
        row.is_active = True
        row.last_seen_at = now
    await db.flush()


async def unregister_device(db: AsyncSession, partner_id: UUID, token: str) -> None:
    """Sign-out. Only the caller's own row: a token is not proof of ownership."""
    from app.models.partner_device import PartnerDevice

    await db.execute(
        update(PartnerDevice)
        .where(PartnerDevice.token == token, PartnerDevice.partner_id == partner_id)
        .values(is_active=False)
    )
    await db.flush()


async def notify_partner(
    db: AsyncSession, partner_id: Optional[UUID], kind: str, **values
) -> int:
    """Push one message to every active phone of a partner. Returns phones reached."""
    if partner_id is None:
        return 0
    try:
        from app.models.partner_device import PartnerDevice
        from app.services.fcm_service import FCMService

        devices = (
            await db.execute(
                select(PartnerDevice.token, PartnerDevice.locale).where(
                    PartnerDevice.partner_id == partner_id, PartnerDevice.is_active.is_(True)
                )
            )
        ).all()
        if not devices:
            return 0

        by_lang: Dict[str, list] = {}
        for token, locale in devices:
            by_lang.setdefault(normalize_lang(locale), []).append(token)

        data = {"type": kind, "screen": SCREENS[kind]}
        reached = 0
        dead: list = []
        for lang, tokens in by_lang.items():
            result = await FCMService.send_to_tokens(
                tokens,
                title=BRAND_TITLE,
                body=render(kind, lang, **values),
                data=data,
                channel_id=PARTNER_CHANNEL_ID,
                tag=f"{kind}:{partner_id}" if kind == "earnings_ready" else None,
            )
            reached += int(result.get("success_count", 0))
            dead.extend(result.get("invalid_tokens", []))

        if dead:
            await db.execute(
                update(PartnerDevice).where(PartnerDevice.token.in_(dead)).values(is_active=False)
            )
            await db.commit()
        return reached
    except Exception:
        logger.exception(f"Partner push {kind} failed for partner={partner_id}")
        return 0


async def notify_earnings_ready(db: AsyncSession, commissions: Iterable) -> int:
    """One push per partner per vesting sweep, with the total."""
    totals: Dict[UUID, Decimal] = {}
    orders: Dict[UUID, set] = {}
    for row in commissions:
        partner_id = getattr(row, "beneficiary_partner_id", None)
        amount = Decimal(str(getattr(row, "amount", 0) or 0))
        if partner_id is None or amount <= 0:
            continue
        totals[partner_id] = totals.get(partner_id, Decimal("0")) + amount
        orders.setdefault(partner_id, set()).add(getattr(row, "order_id", None) or id(row))

    sent = 0
    for partner_id, amount in totals.items():
        sent += await notify_partner(
            db, partner_id, "earnings_ready",
            amount=money(amount), orders=len(orders[partner_id]),
        )
    return sent


async def notify_withdrawal_decided(db: AsyncSession, withdrawal) -> int:
    kind = "withdrawal_paid" if withdrawal.status == "paid" else "withdrawal_rejected"
    return await notify_partner(
        db, withdrawal.partner_id, kind, amount=money(withdrawal.amount)
    )
