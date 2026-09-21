"""Durable opt-in reminders. Row locks prevent duplicates across API workers."""

import asyncio
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import delete, select

from app.config.database import async_session_maker
from app.models.assistant import AssistantReceipt, AssistantReminder
from app.models.notification import Notification
from app.models.user import User
from app.services.assistant_personal import next_morning
from app.services.assistant_tools import tr
from app.services.notification_service import NotificationService


async def dispatch_due(db):
    now = datetime.now(timezone.utc)
    reminders = list(
        (
            await db.execute(
                select(AssistantReminder)
                .join(User, User.user_id == AssistantReminder.user_id)
                .where(
                    AssistantReminder.active.is_(True),
                    AssistantReminder.next_due_at <= now,
                    User.is_active.is_(True),
                    User.is_blocked.is_(False),
                )
                .order_by(AssistantReminder.next_due_at)
                .limit(50)
                .with_for_update(skip_locked=True, of=AssistantReminder)
            )
        )
        .scalars()
        .all()
    )
    pushes = []
    for reminder in reminders:
        lang = reminder.language
        title = tr(
            lang,
            "Time to check your supplies?",
            "ගෙදර බඩු ටික බලමුද?",
            "வீட்டுப் பொருட்களைச் சரிபார்க்கலாமா?",
        )
        message = tr(
            lang,
            f"Would you like to buy {reminder.product_name} again? Check what you have first. Manage or stop this reminder in the assistant.",
            f"{reminder.product_name} නැවත ගන්න අවශ්‍යද? මුලින් ගෙදර ඇති ප්‍රමාණය බලන්න. මේ මතක් කිරීම සහායකයාගෙන් වෙනස් කරන්න හෝ නවත්වන්න පුළුවන්.",
            f"{reminder.product_name} மீண்டும் வாங்க வேண்டுமா? முதலில் வீட்டில் இருப்பதைப் பாருங்கள். உதவியாளரில் இந்த நினைவூட்டலை மாற்றலாம் அல்லது நிறுத்தலாம்.",
        )
        data = {
            "type": "assistant_reminder",
            "product_id": str(reminder.product_id),
            "reminder_id": str(reminder.reminder_id),
        }
        db.add(
            Notification(
                user_id=reminder.user_id,
                type="system",
                title=title,
                message=message,
                reference_type="product",
                reference_id=reminder.product_id,
                data=data,
            )
        )
        reminder.last_sent_at = now
        reminder.next_due_at = next_morning(reminder.interval_days, now)
        pushes.append((reminder.user_id, title, message, data))
    # The notification and next due date commit together: no duplicate in-app alerts.
    await db.execute(
        delete(AssistantReceipt).where(
            AssistantReceipt.created_at < now - timedelta(days=7)
        )
    )
    await db.commit()
    for user_id, title, message, data in pushes:
        try:
            await NotificationService.send_push_notification(
                db, user_id, title, message, data
            )
        except Exception:
            logger.warning(
                "Assistant reminder push unavailable; in-app notification is saved"
            )
    return len(pushes)


async def run_reminder_worker():
    while True:
        try:
            async with async_session_maker() as db:
                await dispatch_due(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Assistant reminder sweep failed; check migration 053 and database connectivity"
            )
        await asyncio.sleep(60)
