"""
SRIBEESonline - app pages whose words live in the database (app_content).

The owner edits these with plain SQL, straight in the database. That path
never passes through the ORM, so live_sync's session hooks cannot see it.
Instead a small watcher reads the table's fingerprint -- how many rows and the
newest updated_at, which the table's trigger stamps on every write -- and when
it moves:

  * bumps the ``content`` version that GET /app/sync-state returns, and
  * sends one silent ``{type: sync, groups: content}`` push to the ``sync``
    topic, so every open app checks its versions and re-reads the page.

Several workers run this loop. The fingerprint is swapped in Redis with
GETSET, so exactly one of them sees the change and announces it.
"""
import asyncio
from typing import Optional

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_content import AppContent
from app.services import live_sync

LANGUAGES = ("en", "si", "ta")
FALLBACK_LANG = "en"

# How often the watcher looks. An edit reaches open apps within this, plus
# the app's own 0-20 s push jitter.
POLL_SECONDS = 30

CONTENT_SCOPE = "content"
_FINGERPRINT_KEY = "app_content:fingerprint"


def normalize_lang(lang: Optional[str]) -> str:
    code = (lang or "").strip().lower()[:2]
    return code if code in LANGUAGES else FALLBACK_LANG


async def get_page(db: AsyncSession, page: str, lang: Optional[str]) -> Optional[AppContent]:
    """The page in ``lang``, else in English; None when there is no such
    active page. A language row switched off does not hide the page -- the
    customer gets English rather than nothing."""
    wanted = normalize_lang(lang)
    rows = (
        await db.execute(
            select(AppContent).where(
                AppContent.page == page,
                AppContent.is_active.is_(True),
                AppContent.lang.in_({wanted, FALLBACK_LANG}),
            )
        )
    ).scalars().all()
    by_lang = {r.lang: r for r in rows}
    return by_lang.get(wanted) or by_lang.get(FALLBACK_LANG)


async def fingerprint(db: AsyncSession) -> str:
    """Changes whenever any row is written, added or removed."""
    count, newest = (
        await db.execute(select(func.count(), func.max(AppContent.updated_at)).select_from(AppContent))
    ).one()
    return f"{count}:{newest.isoformat() if newest else '-'}"


def push_payload() -> dict[str, str]:
    return {"type": live_sync.PUSH_TYPE, "groups": CONTENT_SCOPE, "branches": ""}


async def check_once(db: AsyncSession) -> bool:
    """Look once; announce a change. True when this call announced one."""
    redis = live_sync._redis()
    if redis is None:
        return False
    current = await fingerprint(db)
    previous = await redis.getset(_FINGERPRINT_KEY, current)
    if isinstance(previous, bytes):
        previous = previous.decode()
    # No previous value: a fresh Redis or the first start. Nothing to compare
    # with, and every app re-reads anyway when its versions reset.
    if previous is None or previous == current:
        return False

    await redis.incr(f"sync:v:{CONTENT_SCOPE}")
    from app.services.fcm_service import FCMService

    await FCMService.send_data_to_topic(live_sync.PUSH_TOPIC, push_payload())
    logger.info(f"app_content changed ({previous} -> {current}); open apps told to re-read")
    return True


async def run_content_watcher() -> None:
    from app.config.database import async_session_maker

    while True:
        try:
            if live_sync.enabled:
                async with async_session_maker() as db:
                    await check_once(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "app_content watcher failed; check migration 074. Edits still "
                "show whenever a customer opens the page."
            )
        await asyncio.sleep(POLL_SECONDS)
