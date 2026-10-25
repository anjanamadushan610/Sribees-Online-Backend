"""
Canary re-checks (roadmap P2): a small share of fast answers is also put to
the model, in the background, after the customer already has the fast reply.

The verdict is logged as a 'canary' turn (so the report shows each rule's live
agreement) and, for learned patterns, counted -- the miner demotes a pattern
whose canaries disagree too often. HYBRID_CANARY_RATE sets the share (the
owner chose 2 %); 0 switches it off. Each check costs one model turn.
"""

import asyncio
import random
from typing import Any, Dict, Set

from loguru import logger

from app.config.settings import settings
from app.services.hybrid import memory, recorder
from app.services.hybrid.agreement import agree
from app.services.hybrid.plan import Plan

_pending: Set[asyncio.Task] = set()


async def _check(plan: Plan, run_kwargs: Dict[str, Any], record: Dict[str, Any]) -> None:
    from app.config.database import async_session_maker
    from app.services import assistant_agent
    from app.services.chat_service import _TURN_USAGE

    usage: list = []
    _TURN_USAGE.set(usage)  # this task's own context: its cost is its own

    async def emit(stage, **detail):
        return None

    try:
        async with async_session_maker() as db:
            out = await asyncio.wait_for(assistant_agent.run(db=db, emit=emit, **run_kwargs), timeout=45)
    except Exception as exc:  # noqa: BLE001 - a failed check is no verdict
        logger.warning("Canary check skipped: {}", exc)
        return
    trace = out.get("plan_trace") or []
    verdict = agree(plan.as_log(), trace)
    recorder.record(**record, layer="canary", plan=plan.as_log(), model_trace=trace, usage=usage)
    if verdict is not None and plan.layer == "L1":
        await memory.count(plan.rule, "canary_agree" if verdict else "canary_disagree")


def maybe_check(plan: Plan, run_kwargs: Dict[str, Any], record: Dict[str, Any]) -> bool:
    """Schedule a background re-check of a fast answer with probability HYBRID_CANARY_RATE."""
    rate = float(getattr(settings, "hybrid_canary_rate", 0) or 0)
    if rate <= 0 or random.random() >= rate:
        return False
    try:
        task = asyncio.get_running_loop().create_task(_check(plan, run_kwargs, record))
    except RuntimeError:
        return False
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return True
