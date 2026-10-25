"""
The learner (roadmap P2): turn the model's repeated decisions into patterns,
and promote or demote them on evidence. No model calls.

1. **Mine.** Model-answered turns (layer L4) that no rule claimed are grouped
   by their scrubbed text key. A key seen at least ``MIN_EXAMPLES`` times
   whose plans share one shape at least ``CONSISTENCY`` of the time becomes a
   pattern in 'shadow', with the most common plan. Only replayable reads are
   learned (memory.REPLAYABLE); writes and anything tied to one person's
   order or to product ids are not.
2. **Promote.** A shadow pattern whose plan agreed with the model on at
   least ``PROMOTE_MIN`` live turns, ``PROMOTE_RATE`` of the time, becomes
   'active' -- from then on it answers by itself.
3. **Demote.** An active pattern whose canary re-checks disagree more than
   ``DEMOTE_RATE`` of the time (over ``DEMOTE_MIN`` checks) goes back to
   'shadow' with fresh counters. 'disabled' (set by an admin) is never touched.
"""

import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

from loguru import logger
from sqlalchemy import select

from app.services.hybrid.memory import invalidate, replayable
from app.services.search_text import tokenize

MIN_EXAMPLES = 3
CONSISTENCY = 0.95
PROMOTE_MIN = 20
PROMOTE_RATE = 0.98
DEMOTE_MIN = 5
DEMOTE_RATE = 0.05
WINDOW_DAYS = 30
WORKER_INTERVAL_SECONDS = 6 * 3600


def shape(steps: List[Dict[str, Any]]) -> Tuple:
    """What a plan does, ignoring order, repeats and spelling."""
    parts = set()
    for s in steps:
        args = s.get("args") or {}
        if s.get("name") == "search_products":
            parts.add(("search", tuple(sorted(set(tokenize(str(args.get("query", "")))))), str(args.get("size") or "")))
        elif s.get("name") == "app_help":
            parts.add(("help", args.get("topic"), bool(args.get("open_page"))))
        elif s.get("name") == "my_orders":
            parts.add(("orders", bool(args.get("recent"))))
        else:
            parts.add((s.get("name"),))
    return tuple(sorted(parts, key=repr))


async def mine(session_maker, now: datetime = None) -> Dict[str, int]:
    from app.models.assistant import AssistantPattern, AssistantTurn

    now = now or datetime.now(timezone.utc)
    stats = {"groups": 0, "created": 0, "promoted": 0, "demoted": 0}
    async with session_maker() as db:
        rows = (
            await db.execute(
                select(AssistantTurn.text_key, AssistantTurn.model_trace).where(
                    AssistantTurn.layer == "L4",
                    AssistantTurn.shadow_plan.is_(None),
                    AssistantTurn.created_at >= now - timedelta(days=WINDOW_DAYS),
                )
            )
        ).all()
        groups: Dict[str, List[list]] = defaultdict(list)
        for key, trace in rows:
            if key and trace:
                groups[key].append(trace)
        existing = {
            p.text_key: p for p in (await db.execute(select(AssistantPattern))).scalars().all()
        }
        for key, traces in groups.items():
            if len(traces) < MIN_EXAMPLES:
                continue
            stats["groups"] += 1
            shapes = Counter(shape(t) for t in traces)
            best, hits = shapes.most_common(1)[0]
            if hits / len(traces) < CONSISTENCY:
                continue
            plan_steps = next(t for t in traces if shape(t) == best)
            if not replayable(plan_steps):
                continue
            pattern = existing.get(key)
            if pattern is None:
                db.add(AssistantPattern(text_key=key, plan={"steps": plan_steps, "reply_kind": "list"}, status="shadow", examples=len(traces)))
                stats["created"] += 1
            elif pattern.status != "disabled":
                pattern.examples = len(traces)
        for pattern in existing.values():
            judged = pattern.shadow_agree + pattern.shadow_disagree
            if pattern.status == "shadow" and judged >= PROMOTE_MIN and pattern.shadow_agree / judged >= PROMOTE_RATE:
                pattern.status, pattern.promoted_at = "active", now
                stats["promoted"] += 1
            checks = pattern.canary_agree + pattern.canary_disagree
            if pattern.status == "active" and checks >= DEMOTE_MIN and pattern.canary_disagree / checks > DEMOTE_RATE:
                pattern.status = "shadow"
                pattern.shadow_agree = pattern.shadow_disagree = 0
                pattern.canary_agree = pattern.canary_disagree = 0
                stats["demoted"] += 1
        await db.commit()
    invalidate()
    return stats


async def run_learning_worker() -> None:
    """Background loop: mine and re-judge patterns every few hours while the fast path is on."""
    from app.config.database import async_session_maker
    from app.services.hybrid import engine

    while True:
        await asyncio.sleep(WORKER_INTERVAL_SECONDS)
        if engine.mode() == "off":
            continue
        try:
            stats = await mine(async_session_maker)
            if any(stats.values()):
                logger.info("Assistant learner: {}", stats)
        except Exception as exc:  # noqa: BLE001 - retried next round
            logger.warning("Assistant learner failed: {}", exc)
