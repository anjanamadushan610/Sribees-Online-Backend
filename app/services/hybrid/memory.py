"""
L1: exact memory of what the model did for a message (roadmap P2).

The miner (hybrid/miner.py) turns repeated, consistent model turns into rows
of ``assistant_patterns``: the scrubbed, folded text key of a message and the
plan the model chose. Here those rows are held in memory per worker and
matched in a dictionary lookup. A pattern only *answers* when it is 'active'
-- promoted after agreeing with the model on live shadow turns -- and its
plan is replayed against live tools like any other, so a remembered message
gets today's prices and stock, never yesterday's reply.
"""

import asyncio
import time
from typing import Dict, Optional, Tuple

from loguru import logger
from sqlalchemy import select, update

from app.services.hybrid import recorder
from app.services.hybrid.plan import Plan, ToolCall

REFRESH_SECONDS = 60.0
# Tools a remembered plan may replay: reads, and nothing tied to one person's
# order number or to product ids that come and go. Writes are never learned.
REPLAYABLE = frozenset({
    "search_products", "my_orders", "app_help", "wallet_and_points",
    "my_offers", "my_wishlist", "my_notifications", "my_referrals",
})
PERSONAL_ARGS = frozenset({"order_reference", "product_id", "product_ids", "operations"})

_cache: Dict[str, Tuple[str, str, dict]] = {}  # text_key -> (pattern_id, status, plan)
_loaded_at = float("-inf")
_lock = asyncio.Lock()


def replayable(steps) -> bool:
    return bool(steps) and all(
        s.get("name") in REPLAYABLE and not (set((s.get("args") or {})) & PERSONAL_ARGS) for s in steps
    )


async def _refresh(db) -> None:
    global _cache, _loaded_at
    from app.models.assistant import AssistantPattern

    rows = (
        await db.execute(
            select(AssistantPattern.pattern_id, AssistantPattern.text_key, AssistantPattern.status, AssistantPattern.plan)
            .where(AssistantPattern.status.in_(("shadow", "active")))
        )
    ).all()
    _cache = {r.text_key: (str(r.pattern_id), r.status, r.plan) for r in rows}
    _loaded_at = time.monotonic()


def invalidate() -> None:
    global _loaded_at
    _loaded_at = float("-inf")


async def lookup(db, message: str, language: Optional[str]) -> Optional[Plan]:
    """The remembered plan for ``message``, if any (shadow or active)."""
    if time.monotonic() - _loaded_at > REFRESH_SECONDS:
        async with _lock:
            if time.monotonic() - _loaded_at > REFRESH_SECONDS:
                try:
                    await _refresh(db)
                except Exception as exc:  # noqa: BLE001 - no memory is fine, the model answers
                    logger.warning("Pattern memory not loaded: {}", exc)
                    return None
    key = recorder.scrub(message)
    hit = _cache.get(key)
    if hit is None:
        return None
    pattern_id, status, plan = hit
    steps = [ToolCall(s["name"], dict(s.get("args") or {})) for s in plan.get("steps", [])]
    return Plan(
        steps=steps,
        reply_kind=plan.get("reply_kind") or "list",
        layer="L1",
        rule=f"pattern:{pattern_id}",
        signature=key,
        language=language,
        pattern_status=status,
    )


async def count(pattern_rule: str, field: str) -> None:
    """Add one to a pattern's shadow/canary agree/disagree counter, in its own session."""
    from app.config.database import async_session_maker
    from app.models.assistant import AssistantPattern

    if not pattern_rule.startswith("pattern:") or field not in (
        "shadow_agree", "shadow_disagree", "canary_agree", "canary_disagree"
    ):
        return
    try:
        async with async_session_maker() as db:
            column = getattr(AssistantPattern, field)
            await db.execute(
                update(AssistantPattern)
                .where(AssistantPattern.pattern_id == pattern_rule.split(":", 1)[1])
                .values({field: column + 1})
            )
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - statistics never break a turn
        logger.warning("Pattern counter not updated: {}", exc)
