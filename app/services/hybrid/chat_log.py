"""
The lasting record of customer chats (migration 097): what they wrote, what
came back, which products they saw, and whether they then added or ordered one.

Nothing here deletes by age. Redis keeps only the last turns for 7 days so the
assistant can follow a conversation; this table is what stays. Writes run in
their own task and session, so a logging failure can never affect a customer.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional
from uuid import UUID

from loguru import logger
from sqlalchemy import text

from app.config.database import async_session_maker
from app.models.assistant import AssistantChatLog
from app.services.hybrid.recorder import background

# A chat gets the credit for a purchase this long after it showed the product.
ATTRIBUTION_DAYS = 7
_COLUMNS = {"added_to_cart", "ordered"}


async def _write(row: Dict[str, Any]) -> None:
    try:
        async with async_session_maker() as db:
            db.add(AssistantChatLog(log_id=uuid.uuid4(), **row))
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - logging must never break a turn
        logger.warning("Chat turn not logged: {}", exc)


def record(
    *,
    session_id: str,
    user_id: Optional[UUID],
    branch_id: Optional[UUID],
    message: str,
    reply: str,
    language: Optional[str],
    answered_by: Optional[str],
    products: Iterable[dict],
    had_image: bool,
) -> None:
    background(_write({
        "session_id": session_id[:200],
        "user_id": user_id,
        "branch_id": branch_id,
        "message": message,
        "reply": reply,
        "language": language,
        "answered_by": answered_by,
        "had_image": had_image,
        "product_ids": [str(p["product_id"]) for p in products if p.get("product_id")],
    }))


async def _mark(column: str, user_id: UUID, product_ids: Iterable[str], shown_only: bool) -> None:
    """Note ``product_ids`` on the customer's latest chat turn that led to them."""
    if column not in _COLUMNS:
        raise ValueError(column)
    since = datetime.now(timezone.utc) - timedelta(days=ATTRIBUTION_DAYS)
    shown = "AND product_ids @> CAST(:one AS jsonb)" if shown_only else ""
    statement = text(f"""
        UPDATE assistant_chat_log SET {column} = {column} || CAST(:one AS jsonb)
        WHERE log_id = (
            SELECT log_id FROM assistant_chat_log
            WHERE user_id = :user_id AND created_at >= :since {shown}
            ORDER BY created_at DESC LIMIT 1
        ) AND NOT {column} @> CAST(:one AS jsonb)
    """)
    try:
        async with async_session_maker() as db:
            for product_id in dict.fromkeys(str(p) for p in product_ids):
                await db.execute(statement, {"one": f'["{product_id}"]', "user_id": user_id, "since": since})
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - logging must never break a cart or an order
        logger.warning("Chat outcome not logged: {}", exc)


def note_added(user_id, product_ids: Iterable[str], *, from_chat: bool = False) -> None:
    """Products went into the basket; ``from_chat`` when the chat's own preview put them there."""
    background(_mark("added_to_cart", user_id, list(product_ids), not from_chat))


def note_ordered(user_id, product_ids: Iterable[str]) -> None:
    background(_mark("ordered", user_id, list(product_ids), True))
