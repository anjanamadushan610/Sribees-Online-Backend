"""
The fast path's entry points: plan a message without the model, and run a
plan through the agent's own tools.

``plan_for`` is pure lookups (lexicon, catalogue vocabulary) -- no network
beyond the cached vocabulary. ``run_plan`` executes with the same tool
functions, argument validation and UI assembly the model agent uses, so a
fast answer has exactly the agent's safety properties and response shape.
"""

from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from app.config.settings import settings
from app.services import assistant_agent
from app.services.hybrid import compose
from app.services.hybrid.normalize import parse
from app.services.hybrid.plan import Plan, ToolCall
from app.services.hybrid.rules import route

MODES = ("off", "shadow", "active")


def mode() -> str:
    value = (settings.hybrid_mode or "off").lower()
    return value if value in MODES else "off"


def rule_enabled(plan: Plan) -> bool:
    disabled = {r.strip() for r in (settings.hybrid_disabled_rules or "").split(",") if r.strip()}
    return plan.rule not in disabled and plan.rule.split(".")[0] not in disabled


async def plan_for(db, message: str, fallback_lang: str) -> Optional[Plan]:
    """The fast path's plan for ``message``, or None when the model should answer."""
    from app.services.catalog_search_service import get_vocabulary

    msg = parse(message)
    if msg is None:
        return None
    return route(msg, await get_vocabulary(db), fallback_lang)


async def run_plan(
    plan: Plan,
    *,
    db,
    redis,
    user_id: Optional[UUID],
    branch_id: Optional[UUID],
    history: List[Dict[str, Any]],
    emit,
) -> Optional[Dict[str, Any]]:
    """
    Execute ``plan`` and return the chat response pieces (the agent's shape),
    or None when the results show the fast path was not sure after all.
    """
    lang = plan.language or "si"
    turn = assistant_agent.Turn(lang)
    ctx = {"db": db, "redis": redis, "user_id": user_id, "branch_id": branch_id, "history": history, "emit": emit}
    outputs: List[Tuple[ToolCall, Dict[str, Any]]] = []
    for step in plan.steps:
        out = await assistant_agent._run_tool(step.name, dict(step.args), ctx, turn)
        outputs.append((step, out))

    searched = [step for step, _ in outputs if step.name == "search_products"]
    if searched and branch_id and any(
        not out.get("products") or out.get("exact_match") is False for step, out in outputs if step in searched
    ):
        # Found nothing (or not the asked size/brand) for a word the rules took
        # for a product: the model can try other names, explain the nearest
        # match or work out what was meant. Answer only when sure.
        return None
    if any("error" in out for _, out in outputs) and not (searched and not branch_id):
        return None
    if plan.reply_kind == "greeting":
        reply = compose.greeting(lang)
    elif plan.reply_kind == "thanks":
        reply = compose.thanks(lang)
    elif searched and not branch_id:
        reply = compose.choose_location(lang)
    elif searched:
        reply = compose.products_reply(plan, outputs, lang)
    else:
        reply = "\n\n".join(dict.fromkeys(turn.server_messages)) or compose.greeting(lang)

    shown = list(dict.fromkeys(turn.search_order + turn.auto_products))[: assistant_agent.MAX_SHOWN_PRODUCTS]
    return {
        "reply": reply,
        "products": [turn.products[pid] for pid in shown],
        "orders": turn.orders,
        "actions": turn.actions,
        "guide": turn.guide,
        "panels": turn.panels,
        "language": lang,
        "search_context": turn.search_context,
        "briefing_note": None,
        "briefing_ids": [],
    }
