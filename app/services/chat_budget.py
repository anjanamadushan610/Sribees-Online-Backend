"""
A daily ceiling on what one customer's chat may cost in model calls.

Every Gemini call of a turn is priced from its usageMetadata and added to a
per-customer counter for the Asia/Colombo day. Once the counter passes
ASSISTANT_DAILY_BUDGET_LKR the model is not called again until midnight: the
free fast path and catalogue search still answer, anything else gets a quiet
"resting" reply. The customer is never told an amount or that a limit exists
(owner, 2026-11-02).

A customer is their user id, a guest their device id -- the chat session id.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from loguru import logger

from app.config.settings import settings
from app.services.embedding_service import GeminiAPIError

USD_PER_M = {"input": 0.75, "output": 3.75}  # gemini-3.8-flash list price, 2026
USD_LKR = 300.0

_COLOMBO = ZoneInfo("Asia/Colombo")
# Counters are kept in hundredths of a rupee so INCRBY stays exact.
_KEY_TTL = 2 * 24 * 60 * 60


class ChatBudgetExceeded(GeminiAPIError):
    """No model call for this customer today; handled like a model outage."""

    def __init__(self) -> None:
        super().__init__("Daily chat budget reached")


def cost_lkr(usage: List[Dict[str, Any]]) -> float:
    """What the calls in ``usage`` cost, in rupees (thinking is billed as output)."""
    prompt = sum(int(u.get("promptTokenCount", 0) or 0) for u in usage)
    output = sum(
        int(u.get("candidatesTokenCount", 0) or 0) + int(u.get("thoughtsTokenCount", 0) or 0) for u in usage
    )
    usd = (prompt * USD_PER_M["input"] + output * USD_PER_M["output"]) / 1_000_000
    return usd * USD_LKR


def _key(identity: str) -> str:
    return f"chat:spend:{datetime.now(_COLOMBO).date().isoformat()}:{identity}"


async def ensure_allowed(redis, identity: Optional[str]) -> None:
    """Raise ChatBudgetExceeded when ``identity`` has used up today's budget."""
    limit = settings.assistant_daily_budget_lkr
    if not identity or limit <= 0:
        return
    try:
        spent = int(await redis.get(_key(identity)) or 0)
    except Exception as exc:  # noqa: BLE001 - a Redis hiccup must not stop the chat
        logger.warning("Chat budget not checked: {}", exc)
        return
    if spent >= round(limit * 100):
        raise ChatBudgetExceeded()


async def charge(redis, identity: Optional[str], usage: List[Dict[str, Any]]) -> None:
    """Add the cost of ``usage`` to today's counter for ``identity``."""
    if not identity or not usage:
        return
    cents = round(cost_lkr(usage) * 100)
    if cents <= 0:
        return
    key = _key(identity)
    try:
        total = await redis.incrby(key, cents)
        if total == cents:
            await redis.expire(key, _KEY_TTL)
    except Exception as exc:  # noqa: BLE001 - accounting must never fail a reply
        logger.warning("Chat spend not recorded: {}", exc)
        return
    limit = settings.assistant_daily_budget_lkr
    if limit > 0 and total >= round(limit * 100) > total - cents:
        logger.info("Chat daily budget reached for {}", identity)
