"""
SRIBEESonline — marketing push campaigns.

A marketing manager writes a message once, in English and (optionally)
Sinhala and Tamil, picks offers / promotions / general, and either sends it
now or schedules it. This module decides who gets it and delivers it.

**Who.** Live customers -- active, not blocked, not deleted -- who have that
category switched on. A branch's campaign goes to the customers that branch
has served (the same rule as the customer directory); only a Super Admin can
address every customer.

**How.** Nothing is sent inside the admin's request. The campaign row is
saved as ``scheduled``; :func:`run_due` (the worker, and a nudge right after
a "send now") claims it with ``FOR UPDATE SKIP LOCKED`` so two API workers
never both send it, writes one in-app row per customer in their language,
then pushes to every device in that device's language, 500 tokens per FCM
call. Counters are saved as it goes, so the dashboard shows progress.

A campaign interrupted mid-send (the process died) is marked ``failed``
rather than resumed: resuming cannot know who already got it, and sending a
promotion twice is worse than a manager pressing send again.
"""
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from uuid import UUID, uuid4

from loguru import logger
from sqlalchemy import and_, func, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import async_session_maker
from app.core.media import media_url
from app.models.notification import (
    CampaignStatus,
    Notification,
    NotificationCampaign,
    PushToken,
)
from app.models.order import Order
from app.models.user import User
from app.services import notification_i18n as i18n
from app.services.fcm_service import FCMService

POLL_SECONDS = 20
INBOX_BATCH = 1000
# A send that has been "sending" this long was interrupted, not slow.
STALE_AFTER = timedelta(minutes=30)


def _column_for(category: str):
    return {
        "offers": User.notify_offers,
        "promotions": User.notify_promotions,
        "general": User.notify_general,
    }[category]


def audience_filter(category: Optional[str], branch_id: Optional[UUID]):
    """WHERE clause over ``users`` for a campaign's recipients."""
    clauses = [
        User.is_active.is_(True),
        User.is_blocked.is_(False),
        User.deleted_at.is_(None),
    ]
    if category is not None:
        clauses.append(_column_for(category).is_(True))
    if branch_id is not None:
        served = select(Order.user_id).where(Order.branch_id == branch_id).distinct()
        clauses.append(User.user_id.in_(served))
    return and_(*clauses)


def _device_language():
    return func.coalesce(PushToken.language, User.preferred_language, literal("en"))


async def estimate(db: AsyncSession, category: str, branch_id: Optional[UUID]) -> dict:
    """How many customers and devices a campaign would reach, by language."""
    reachable = await db.scalar(
        select(func.count(User.user_id)).where(audience_filter(category, branch_id))
    ) or 0
    everyone = await db.scalar(
        select(func.count(User.user_id)).where(audience_filter(None, branch_id))
    ) or 0

    lang = _device_language()
    rows = (
        await db.execute(
            select(lang, func.count(PushToken.token_id))
            .select_from(PushToken)
            .join(User, User.user_id == PushToken.user_id)
            .where(PushToken.is_active.is_(True), audience_filter(category, branch_id))
            .group_by(lang)
        )
    ).all()
    by_language = {code: 0 for code in i18n.LANGUAGES}
    for code, count in rows:
        by_language[i18n.normalize_lang(code)] += count

    return {
        "customers": reachable,
        "opted_out": max(everyone - reachable, 0),
        "devices": sum(by_language.values()),
        "devices_by_language": by_language,
    }


def serialize(c: NotificationCampaign, branch_name: Optional[str] = None) -> dict:
    return {
        "campaign_id": str(c.campaign_id),
        "category": c.category,
        "title": c.title or {},
        "message": c.message or {},
        "image_url": media_url(c.image_url),
        "link_type": c.link_type,
        "link_value": c.link_value,
        "branch_id": str(c.branch_id) if c.branch_id else None,
        "branch_name": branch_name,
        "status": c.status,
        "scheduled_at": c.scheduled_at.isoformat() if c.scheduled_at else None,
        "started_at": c.started_at.isoformat() if c.started_at else None,
        "finished_at": c.finished_at.isoformat() if c.finished_at else None,
        "recipients": c.recipients or 0,
        "devices": c.devices or 0,
        "delivered": c.delivered or 0,
        "failed": c.failed or 0,
        "opened": c.opened or 0,
        # Of the customers who got it, how many opened it. None until sent.
        "open_rate": (
            round((c.opened or 0) / c.recipients, 4) if c.recipients else None
        ),
        "error": c.error,
        "created_by_name": c.created_by_name,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


# ---------------------------------------------------------------------------
# Opens
# ---------------------------------------------------------------------------

async def count_open(db: AsyncSession, campaign_id) -> None:
    """+1 open. The caller commits, and only calls this on an unread → read change."""
    try:
        cid = UUID(str(campaign_id))
    except ValueError:
        return
    await db.execute(
        update(NotificationCampaign)
        .where(NotificationCampaign.campaign_id == cid)
        .values(opened=NotificationCampaign.opened + 1)
    )


async def mark_opened(db: AsyncSession, user_id: UUID, campaign_id: UUID) -> bool:
    """A push tap: mark this customer's copy read and count them once."""
    notification = (
        await db.execute(
            select(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.data["campaign_id"].astext == str(campaign_id),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if notification is None or notification.is_read:
        return False
    notification.is_read = True
    notification.read_at = datetime.now(timezone.utc)
    await count_open(db, campaign_id)
    await db.commit()
    return True


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def _payload(c: NotificationCampaign) -> dict:
    """What the app needs to open the right screen from the push or the feed."""
    data = {
        "type": "promotion" if c.category != "general" else "system",
        "category": c.category,
        "campaign_id": str(c.campaign_id),
    }
    image = media_url(c.image_url)
    if image:
        data["image_url"] = image
    if c.link_type == "product" and c.link_value:
        data["product_id"] = c.link_value
        data["screen"] = "product"
    elif c.link_type == "offers":
        data["screen"] = "offers"
    return data


async def _claim_due() -> Optional[UUID]:
    now = datetime.now(timezone.utc)
    async with async_session_maker() as db:
        campaign = (
            await db.execute(
                select(NotificationCampaign)
                .where(
                    NotificationCampaign.status == CampaignStatus.SCHEDULED.value,
                    NotificationCampaign.scheduled_at <= now,
                )
                .order_by(NotificationCampaign.scheduled_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if campaign is None:
            return None
        campaign.status = CampaignStatus.SENDING.value
        campaign.started_at = now
        await db.commit()
        return campaign.campaign_id


async def _fail_stale() -> None:
    cutoff = datetime.now(timezone.utc) - STALE_AFTER
    async with async_session_maker() as db:
        result = await db.execute(
            update(NotificationCampaign)
            .where(
                NotificationCampaign.status == CampaignStatus.SENDING.value,
                NotificationCampaign.started_at < cutoff,
            )
            .values(
                status=CampaignStatus.FAILED.value,
                finished_at=datetime.now(timezone.utc),
                error="Interrupted while sending. Some customers may have received it.",
            )
        )
        if result.rowcount:
            await db.commit()


async def deliver(campaign_id: UUID) -> None:
    """Send one claimed campaign: in-app rows, then pushes. Records its own failure."""
    async with async_session_maker() as db:
        campaign = await db.get(NotificationCampaign, campaign_id)
        if campaign is None:
            return
        try:
            await _deliver(db, campaign)
        except Exception as exc:
            logger.exception(f"Campaign {campaign_id} failed")
            await db.rollback()
            campaign = await db.get(NotificationCampaign, campaign_id)
            if campaign is not None:
                campaign.status = CampaignStatus.FAILED.value
                campaign.finished_at = datetime.now(timezone.utc)
                campaign.error = str(exc)[:500]
                await db.commit()


async def _deliver(db: AsyncSession, c: NotificationCampaign) -> None:
    texts = i18n.localized_all(c.title or {}, c.message or {})
    data = _payload(c)
    where = audience_filter(c.category, c.branch_id)

    # 1. The in-app feed, one row per customer, in their language.
    users = (
        await db.execute(select(User.user_id, User.preferred_language).where(where))
    ).all()
    notif_type = data["type"]
    for start in range(0, len(users), INBOX_BATCH):
        rows = []
        for user_id, preferred in users[start:start + INBOX_BATCH]:
            title, message = texts[i18n.normalize_lang(preferred)]
            rows.append(
                {
                    "notification_id": uuid4(),
                    "user_id": user_id,
                    "type": notif_type,
                    "title": title[:255],
                    "message": message,
                    "reference_type": "campaign",
                    "reference_id": c.campaign_id,
                    "data": data,
                    "is_read": False,
                    "push_sent": False,
                }
            )
        await db.execute(insert(Notification), rows)
        c.recipients = start + len(rows)
        await db.commit()
    c.recipients = len(users)

    # 2. Pushes, grouped by each device's language.
    lang = _device_language()
    token_rows = (
        await db.execute(
            select(PushToken.token, lang)
            .select_from(PushToken)
            .join(User, User.user_id == PushToken.user_id)
            .where(PushToken.is_active.is_(True), where)
        )
    ).all()
    groups: Dict[str, List[str]] = defaultdict(list)
    for token, code in token_rows:
        groups[i18n.normalize_lang(code)].append(token)
    c.devices = len(token_rows)
    await db.commit()

    image = data.get("image_url")
    image = image if image and image.startswith(("http://", "https://")) else None
    push_data = {k: str(v) for k, v in data.items()}
    for code, tokens in groups.items():
        title, message = texts[code]
        result = await FCMService.send_to_tokens(
            tokens=tokens,
            title=i18n.BRAND_TITLE,
            body=i18n.push_body(title, message, standalone=False),
            data=push_data,
            image_url=image,
        )
        c.delivered = (c.delivered or 0) + result.get("success_count", 0)
        c.failed = (c.failed or 0) + result.get("failure_count", 0)
        dead = result.get("invalid_tokens", [])
        if dead:
            await db.execute(
                update(PushToken).where(PushToken.token.in_(dead)).values(is_active=False)
            )
        await db.commit()

    c.status = CampaignStatus.SENT.value
    c.finished_at = datetime.now(timezone.utc)
    await db.commit()
    logger.info(
        f"Campaign {c.campaign_id} sent: {c.recipients} customers, "
        f"{c.delivered}/{c.devices} devices"
    )


async def run_due() -> int:
    """Send every campaign that is due. Returns how many were sent."""
    await _fail_stale()
    sent = 0
    while True:
        campaign_id = await _claim_due()
        if campaign_id is None:
            return sent
        await deliver(campaign_id)
        sent += 1


async def run_campaign_worker():
    while True:
        try:
            await run_due()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Campaign sweep failed; check migration 071 and database "
                "connectivity. Scheduled campaigns wait until it recovers."
            )
        await asyncio.sleep(POLL_SECONDS)
