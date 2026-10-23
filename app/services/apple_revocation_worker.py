"""Durable Apple revocation retries for closed accounts, never active accounts."""
import asyncio
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select

from app.config.database import async_session_maker
from app.models.user import User
from app.services import apple_tokens


async def retry_pending(db):
    if not apple_tokens.configured():
        return 0
    now = datetime.now(timezone.utc)
    users = list((await db.execute(
        select(User).where(
            User.deleted_at.is_not(None),
            User.apple_refresh_token.is_not(None),
            User.apple_revocation_retry_at <= now,
        ).order_by(User.apple_revocation_retry_at)
        .limit(10).with_for_update(skip_locked=True, of=User)
    )).scalars().all())
    completed = 0
    for user in users:
        if await apple_tokens.revoke(user.apple_refresh_token, user.apple_client_id or ""):
            user.apple_refresh_token = None
            user.apple_client_id = None
            user.apple_revocation_retry_at = None
            completed += 1
        else:
            user.apple_revocation_retry_at = datetime.now(timezone.utc) + timedelta(minutes=5)
            logger.warning("Apple revocation retry pending")
    await db.commit()
    return completed


async def run_apple_revocation_worker():
    while True:
        try:
            async with async_session_maker() as db:
                await retry_pending(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never log token contents or a response body here.
            logger.error("Apple revocation sweep failed; will retry")
        await asyncio.sleep(60)
