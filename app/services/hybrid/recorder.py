"""
Writes one scrubbed row per chat turn to ``assistant_turns`` (migration 089).

Scrubbing happens before anything is stored: phone numbers, e-mail
addresses, order references, ids and long digit runs are replaced, and what
remains is kept only as the folded search key ("mage order eka koheda" ->
"mage order eka koheda", "ගෙදර 0771234567" -> "gedara <phone>"). People are a
salted hash, never an id. The write runs in its own task and session: a
logging failure can never affect the customer's reply.
"""

import asyncio
import hashlib
import re
import uuid
from typing import Any, Dict, Optional, Set

from loguru import logger

from app.config.settings import settings
from app.services.search_text import tokenize

# Placeholder words chosen to pass through the search folding unchanged (no
# c, ph, th, w or doubled letters), then swapped for readable tags.
_SCRUB = (
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), " xmailx "),
    (re.compile(r"(?:\+?94|0)\s?7\d(?:[\s-]?\d){7}"), " xtelx "),
    (re.compile(r"\b(?:sb|ord)[-\s]?\d{3,}\b", re.I), " xordx "),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), " xidx "),
    (re.compile(r"\d{6,}"), " xnumx "),
)
_PLACEHOLDERS = {
    "xmailx": "<email>",
    "xtelx": "<phone>",
    "xordx": "<order>",
    "xidx": "<id>",
    "xnumx": "<number>",
}
MAX_KEY = 500

_pending: Set[asyncio.Task] = set()


def scrub(text: Optional[str]) -> str:
    """The message as a folded search key with personal details replaced."""
    value = text or ""
    for pattern, replacement in _SCRUB:
        value = pattern.sub(replacement, value)
    tokens = [_PLACEHOLDERS.get(t, t) for t in tokenize(value)]
    return " ".join(tokens)[:MAX_KEY]


def person_hash(identity: Optional[str]) -> Optional[str]:
    if not identity:
        return None
    salt = getattr(settings, "jwt_secret_key", "") or ""
    return hashlib.sha256(f"{salt}|assistant|{identity}".encode()).hexdigest()[:32]


async def _write(row: Dict[str, Any]) -> None:
    from app.config.database import async_session_maker
    from app.models.assistant import AssistantTurn

    try:
        async with async_session_maker() as db:
            db.add(AssistantTurn(turn_id=uuid.uuid4(), **row))
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - statistics must never break a turn
        logger.warning("Assistant turn not recorded: {}", exc)


def record(
    *,
    message: str,
    layer: str,
    branch_id=None,
    identity: Optional[str] = None,
    language: Optional[str] = None,
    plan: Optional[Dict[str, Any]] = None,
    shadow_plan: Optional[Dict[str, Any]] = None,
    model_trace: Optional[list] = None,
    signature: Optional[str] = None,
    reply_kind: Optional[str] = None,
    latency_ms: Optional[int] = None,
    usage: Optional[list] = None,
) -> None:
    """Queue one turn for the log (no-op when ASSISTANT_TURN_LOG_ENABLED is off)."""
    if not settings.assistant_turn_log_enabled:
        return
    usage = usage or []
    row = {
        "branch_id": branch_id,
        "person_hash": person_hash(identity),
        "language": language,
        "text_key": scrub(message),
        "signature": (signature or "")[:200] or None,
        "layer": layer,
        "plan": plan or {},
        "shadow_plan": shadow_plan,
        "model_trace": model_trace,
        "reply_kind": reply_kind,
        "latency_ms": latency_ms,
        "model_calls": len(usage),
        "prompt_tokens": sum(int(u.get("promptTokenCount", 0) or 0) for u in usage),
        "output_tokens": sum(
            int(u.get("candidatesTokenCount", 0) or 0) + int(u.get("thoughtsTokenCount", 0) or 0) for u in usage
        ),
    }
    try:
        task = asyncio.get_running_loop().create_task(_write(row))
    except RuntimeError:
        return
    _pending.add(task)
    task.add_done_callback(_pending.discard)
