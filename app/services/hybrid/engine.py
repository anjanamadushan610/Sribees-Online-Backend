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
from app.services import assistant_agent, catalog_search_service
from app.services.hybrid import compose
from app.services.hybrid.normalize import parse
from app.services.hybrid.plan import Plan, ToolCall
from app.services.hybrid.rules import rescue_query, route

MODES = ("off", "shadow", "active")


def mode() -> str:
    value = (settings.hybrid_mode or "off").lower()
    return value if value in MODES else "off"


# Rules an admin switched off at runtime (the admin page); added to the
# HYBRID_DISABLED_RULES setting, so switching needs no restart.
DISABLED_RULES_KEY = "assistant:hybrid:disabled_rules"


async def disabled_rules(redis) -> set:
    rules = {r.strip() for r in (settings.hybrid_disabled_rules or "").split(",") if r.strip()}
    if redis is not None:
        try:
            members = await redis.smembers(DISABLED_RULES_KEY)
            rules |= {m.decode() if isinstance(m, bytes) else str(m) for m in members or ()}
        except Exception:  # noqa: BLE001 - no Redis = only the setting applies
            pass
    return rules


def rule_enabled(plan: Plan, disabled: Optional[set] = None) -> bool:
    if disabled is None:
        disabled = {r.strip() for r in (settings.hybrid_disabled_rules or "").split(",") if r.strip()}
    return plan.rule not in disabled and plan.rule.split(".")[0] not in disabled


def answer_allowed(plan: Plan, disabled: Optional[set] = None) -> bool:
    """Whether ``plan`` may answer by itself (HYBRID_MODE active is checked by the caller)."""
    if plan.layer == "L1":
        return plan.pattern_status == "active"
    return rule_enabled(plan, disabled)


async def plan_for(db, message: str, fallback_lang: str) -> Optional[Plan]:
    """The fast path's plan for ``message`` -- rules first, then learned memory -- or None."""
    from app.services.catalog_search_service import get_vocabulary
    from app.services.hybrid import memory
    from app.services.hybrid.rules import _language

    msg = parse(message)
    if msg is None:
        return None
    plan = route(msg, await get_vocabulary(db), fallback_lang)
    if plan is not None:
        return plan
    return await memory.lookup(db, message, _language(msg, fallback_lang))


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
    if searched and branch_id:
        # Search also returns partial matches ("sudu kiri" -> any "kiri"). A
        # reply saying "yes, we have it" needs every word found in one product.
        for step, out in outputs:
            if step in searched and not await catalog_search_service.any_covers_all(
                db, [p["product_id"] for p in out["products"]], step.args["query"]
            ):
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


async def rescue(
    message: str,
    fallback_lang: str,
    *,
    db,
    redis,
    user_id: Optional[UUID],
    branch_id: Optional[UUID],
    history: List[Dict[str, Any]],
    emit,
) -> Optional[Dict[str, Any]]:
    """
    The model is down: show what the catalogue has for the product words in
    ``message``, saying plainly that this is only a search. None when the
    message names no product, or is about anything but products.
    """
    from app.services.catalog_search_service import get_vocabulary
    from app.services.hybrid.rules import _language

    if not branch_id:
        return None
    msg = parse(message)
    query = rescue_query(msg, await get_vocabulary(db)) if msg else None
    if not query:
        return None
    lang = _language(msg, fallback_lang)
    turn = assistant_agent.Turn(lang)
    ctx = {"db": db, "redis": redis, "user_id": user_id, "branch_id": branch_id, "history": history, "emit": emit}
    out = await assistant_agent._run_tool("search_products", {"query": query}, ctx, turn)
    if not out.get("products"):
        return None
    shown = list(dict.fromkeys(turn.search_order))[: assistant_agent.MAX_SHOWN_PRODUCTS]
    return {
        "reply": compose.rescued(lang),
        "products": [turn.products[pid] for pid in shown],
        "orders": [],
        "actions": [],
        "guide": None,
        "panels": [],
        "language": lang,
        "search_context": turn.search_context,
        "briefing_note": None,
        "briefing_ids": [],
    }
