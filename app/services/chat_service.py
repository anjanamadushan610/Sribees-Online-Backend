"""
AI Shopping Chat Service — Gemini-powered assistant over the real catalog.

A customer sends a message (text and/or a photo) — "I need to make a cake",
or a photo of a half-empty spice jar — and this asks Gemini to (a) reply
conversationally and (b) extract concrete product search terms, then resolves
those terms against the real, branch-visible catalog via the same fuzzy/
multilingual search Phase 1 built (``ProductService.get_all_for_branch``), so
a Sinhala/Tamil ingredient name or a typo in what Gemini extracted still
finds the right product.

Gemini-calling shape is deliberately identical to
``keyword_suggestion_service.py`` (retry loop, timeout, error handling) —
including the same maxOutputTokens=2048 lesson: current Gemini flash models
spend hundreds of tokens "thinking" before the visible answer, and a low cap
truncates the answer, not the reasoning.
"""
import asyncio
import json
import unicodedata
from typing import Any, Dict, List, Optional
from uuid import UUID

import httpx
from loguru import logger
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.redis import RedisKeys, RedisTTL
from app.config.settings import settings
from app.services.embedding_service import GeminiAPIError
from app.services.product_service import ProductService
from app.utils.i18n import DEFAULT_LANGUAGE

GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
REQUEST_TIMEOUT = 60.0
MAX_RETRIES = 2
RETRY_DELAY = 1.5

# Bounds on every call, independent of how long the conversation gets —
# these cap Gemini prompt size/cost and DB query count per message.
MAX_HISTORY_TURNS = 10
MAX_SEARCH_TERMS = 6
MAX_MATCHES_PER_TERM = 3

_SYSTEM_PROMPT = """You are SRIBEES Online's grocery shopping assistant, chatting with a customer in a Sri Lankan grocery delivery app.

The customer may write in English, Sinhala, Tamil, or Singlish, and may attach a photo (an ingredient, a dish they want to recreate, a product they're out of).

Reply in the SAME language style the customer used. Be warm and brief — this is a chat bubble, not an essay.

Also extract concrete, simple grocery product search terms for what they need (e.g. "a cake" -> ["flour","sugar","eggs","butter","baking powder","vanilla essence"]). Use plain, generic ingredient/product names (not brand names), at most 6 terms. If the message isn't about groceries at all, return an empty list.

Reply with ONLY a JSON object, no other text, in exactly this shape:
{"reply": "your conversational reply", "search_terms": ["term1", "term2"]}
"""


class ChatService:
    """AI shopping chat: conversation state + Gemini + real product matching."""

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    @staticmethod
    async def get_history(redis: Redis, session_id: str) -> List[Dict[str, Any]]:
        raw = await redis.get(RedisKeys.chat_history(session_id))
        if not raw:
            return []
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []

    @staticmethod
    async def clear_history(redis: Redis, session_id: str) -> None:
        await redis.delete(RedisKeys.chat_history(session_id))

    @staticmethod
    async def _append_turn(
        redis: Redis, session_id: str, history: List[Dict[str, Any]], user_message: str, reply: str
    ) -> None:
        history = history + [
            {"role": "user", "text": user_message},
            {"role": "assistant", "text": reply},
        ]
        history = history[-(MAX_HISTORY_TURNS * 2):]
        await redis.set(
            RedisKeys.chat_history(session_id),
            json.dumps(history),
            ex=RedisTTL.CHAT_HISTORY,
        )

    # ------------------------------------------------------------------
    # Gemini call
    # ------------------------------------------------------------------

    @staticmethod
    async def _ask_gemini(
        message: str,
        history: List[Dict[str, Any]],
        image_base64: Optional[str],
        image_mime_type: Optional[str],
    ) -> Dict[str, Any]:
        api_key = getattr(settings, "gemini_api_key", None)
        if not api_key:
            raise GeminiAPIError("GEMINI_API_KEY not configured")

        # History as prior turns, then the new message (+ optional image) as
        # the final user turn — Gemini's multi-turn contents shape.
        contents = [{"role": "user", "parts": [{"text": _SYSTEM_PROMPT}]}]
        for turn in history[-(MAX_HISTORY_TURNS * 2):]:
            role = "model" if turn.get("role") == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": turn.get("text", "")}]})

        parts: List[Dict[str, Any]] = [{"text": message}]
        if image_base64 and image_mime_type:
            parts.append(
                {"inline_data": {"mime_type": image_mime_type, "data": image_base64}}
            )
        contents.append({"role": "user", "parts": parts})

        model = getattr(settings, "gemini_generation_model", "gemini-flash-latest")
        url = f"{GEMINI_API_BASE_URL}/models/{model}:generateContent"
        payload = {
            "contents": contents,
            # 2048: see keyword_suggestion_service.py — current flash models
            # spend a chunk of the output budget "thinking" before the
            # visible reply, and a low cap truncates the reply, not that.
            "generationConfig": {
                "temperature": 0.6,
                "maxOutputTokens": 2048,
                "response_mime_type": "application/json",
            },
        }

        last_error: Optional[Exception] = None

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            for attempt in range(MAX_RETRIES + 1):
                try:
                    response = await client.post(url, json=payload, params={"key": api_key})
                except httpx.RequestError as exc:
                    last_error = exc
                    logger.warning(f"Chat Gemini request error: {exc}")
                else:
                    if response.status_code == 200:
                        data = response.json()
                        try:
                            text = data["candidates"][0]["content"]["parts"][0]["text"]
                        except (KeyError, IndexError):
                            raise GeminiAPIError("Unexpected Gemini response shape")
                        return ChatService._parse_reply(text)

                    if response.status_code in (429, 500, 502, 503, 504):
                        last_error = GeminiAPIError(
                            f"Gemini returned {response.status_code}",
                            status_code=response.status_code,
                            retryable=True,
                        )
                        logger.warning(
                            f"Chat Gemini error (attempt {attempt + 1}/{MAX_RETRIES + 1}): "
                            f"{response.status_code}"
                        )
                    else:
                        raise GeminiAPIError(
                            f"Gemini returned {response.status_code}: {response.text[:200]}",
                            status_code=response.status_code,
                        )

                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY)

        raise last_error or GeminiAPIError("Chat request failed after retries")

    @staticmethod
    def _parse_reply(text: str) -> Dict[str, Any]:
        """
        Parse Gemini's JSON reply. Falls back to "just chat, no products"
        instead of a 500 if the model didn't return valid JSON — a malformed
        AI response should degrade gracefully, not break the chat.
        """
        cleaned = unicodedata.normalize("NFC", text.strip())
        try:
            data = json.loads(cleaned)
            reply = str(data.get("reply") or "").strip()
            terms = data.get("search_terms") or []
            terms = [str(t).strip() for t in terms if str(t).strip()][:MAX_SEARCH_TERMS]
            if reply:
                return {"reply": reply, "search_terms": terms}
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass

        logger.warning("Chat: Gemini reply was not the expected JSON shape, using raw text")
        return {"reply": cleaned, "search_terms": []}

    # ------------------------------------------------------------------
    # Product matching
    # ------------------------------------------------------------------

    @staticmethod
    async def _match_products(
        db: AsyncSession,
        branch_id: Optional[UUID],
        search_terms: List[str],
        lang: str = DEFAULT_LANGUAGE,
    ) -> List[dict]:
        if not branch_id or not search_terms:
            return []

        from app.api.v1.products import format_product

        seen: set = set()
        matches: List[dict] = []

        for term in search_terms:
            items, _total = await ProductService.get_all_for_branch(
                db, branch_id=branch_id, search=term, limit=MAX_MATCHES_PER_TERM
            )
            for item in items:
                pid = item["product"].product_id
                if pid in seen:
                    continue
                seen.add(pid)
                # compute_effective_data already resolves branch-vs-global
                # cashback/price/discount, so this is the same effective
                # data every other product listing shows — no separate
                # cashback lookup needed.
                matches.append(
                    format_product(
                        item["product"],
                        branch_overrides=item["effective"],
                        lang=lang,
                    )
                )

        return matches

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    @staticmethod
    async def chat(
        db: AsyncSession,
        redis: Redis,
        session_id: str,
        branch_id: Optional[UUID],
        message: str,
        image_base64: Optional[str] = None,
        image_mime_type: Optional[str] = None,
        lang: str = DEFAULT_LANGUAGE,
    ) -> Dict[str, Any]:
        history = await ChatService.get_history(redis, session_id)

        result = await ChatService._ask_gemini(message, history, image_base64, image_mime_type)
        products = await ChatService._match_products(
            db, branch_id, result["search_terms"], lang=lang
        )

        await ChatService._append_turn(redis, session_id, history, message, result["reply"])

        return {"reply": result["reply"], "products": products}
