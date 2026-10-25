"""
Admin view of the hybrid assistant (roadmap P3).

Prefix "/admin/assistant-insights" is applied by app/api/v1/router.py.

* ``GET ""`` -- what the assistant did over N days: share answered free (L1
  memory, L2 rules) versus by the model, latency, model cost, and each rule's
  or learned pattern's agreement with the model; plus the live settings.
* ``GET /patterns`` -- what was learned, with its evidence.
* ``PUT /patterns/{id}`` -- set a pattern active, back to shadow, or disabled
  (disabled is never changed by the learner).
* ``PUT /rules/{rule}`` -- switch a fast-path rule or rule family off or on at
  runtime, no restart.

Reading needs analytics:read; changing what answers customers is for Super
Admins only. Nothing here calls the model.
"""

from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.redis import get_redis
from app.config.settings import settings
from app.core.dependencies import require_permission, require_roles
from app.models.assistant import AssistantPattern
from app.services.hybrid import engine, insights, memory

router = APIRouter(tags=["Admin AI Assistant"])

KNOWN_RULES = (
    "products", "products.list", "products.want", "products.price",
    "orders", "orders.track", "orders.history",
    "help", "help.how", "help.open", "help.order",
    "social", "social.greeting", "social.thanks",
    "cart", "cart.add", "account", "account.wallet", "account.offers",
)


class PatternUpdate(BaseModel):
    status: Literal["active", "shadow", "disabled"]


class RuleUpdate(BaseModel):
    enabled: bool


def _pattern_json(p: AssistantPattern) -> dict:
    judged = p.shadow_agree + p.shadow_disagree
    checks = p.canary_agree + p.canary_disagree
    return {
        "pattern_id": str(p.pattern_id),
        "text_key": p.text_key,
        "plan": p.plan,
        "status": p.status,
        "source": p.source,
        "examples": p.examples,
        "shadow": {"agree": p.shadow_agree, "disagree": p.shadow_disagree, "rate": round(p.shadow_agree / judged, 3) if judged else None},
        "canary": {"agree": p.canary_agree, "disagree": p.canary_disagree, "rate": round(p.canary_agree / checks, 3) if checks else None},
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "promoted_at": p.promoted_at.isoformat() if p.promoted_at else None,
    }


@router.get("", response_model=dict)
async def get_insights(
    days: int = Query(14, ge=1, le=180),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    _admin=Depends(require_permission("analytics", "read")),
):
    data = await insights.summary(db, days)
    data["config"] = {
        "hybrid_mode": engine.mode(),
        "agent": bool(settings.assistant_agent_v2),
        "canary_rate": settings.hybrid_canary_rate,
        "disabled_rules": sorted(await engine.disabled_rules(redis)),
        "known_rules": list(KNOWN_RULES),
    }
    return {"success": True, "data": data}


@router.get("/patterns", response_model=dict)
async def list_patterns(
    status: Optional[Literal["active", "shadow", "disabled"]] = None,
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_permission("analytics", "read")),
):
    query = select(AssistantPattern).order_by(AssistantPattern.examples.desc()).limit(limit)
    if status:
        query = query.where(AssistantPattern.status == status)
    rows = (await db.execute(query)).scalars().all()
    return {"success": True, "data": {"patterns": [_pattern_json(p) for p in rows]}}


@router.put("/patterns/{pattern_id}", response_model=dict)
async def update_pattern(
    pattern_id: UUID,
    body: PatternUpdate,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_roles("super_admin")),
):
    pattern = await db.get(AssistantPattern, pattern_id)
    if pattern is None:
        raise HTTPException(404, "PATTERN_NOT_FOUND")
    pattern.status = body.status
    await db.commit()
    memory.invalidate()
    return {"success": True, "data": _pattern_json(pattern)}


@router.put("/rules/{rule}", response_model=dict)
async def set_rule(
    rule: str,
    body: RuleUpdate,
    redis: Redis = Depends(get_redis),
    _admin=Depends(require_roles("super_admin")),
):
    if rule not in KNOWN_RULES:
        raise HTTPException(404, "UNKNOWN_RULE")
    if body.enabled:
        await redis.srem(engine.DISABLED_RULES_KEY, rule)
    else:
        await redis.sadd(engine.DISABLED_RULES_KEY, rule)
    return {"success": True, "data": {"disabled_rules": sorted(await engine.disabled_rules(redis))}}
