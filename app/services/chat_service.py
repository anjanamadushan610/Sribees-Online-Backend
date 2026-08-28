"""
AI Shopping Chat Service — Gemini-powered assistant over the real catalog.

A customer sends a message (text and/or a photo) — "I need to make a cake",
or a photo of a half-empty spice jar — and this asks Gemini to extract intent
+ structured search terms (brand/variant/price bounds), resolves those against
the real, branch-visible catalog via the same fuzzy/multilingual search
Phase 1 built (``ProductService.get_all_for_branch``), and then GROUNDS the
final reply in those real results rather than trusting whatever Gemini wrote.

Gemini has no inventory visibility, so its own "reply" text can't be trusted
for availability facts — that's what caused this service's original
hallucination bugs (confidently confirming out-of-stock brands/sizes, or
inventing an apology when a search silently returned nothing). See
``_ground_reply`` and ``docs/CHATBOT_BUG_FIX_PLAN.md`` for the fix.

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

CRITICAL — you do NOT have access to live stock/inventory. A separate system checks the real database AFTER you reply, and will correct or override your reply if it disagrees with real stock. So:
- NEVER state or imply that a specific item, brand, or size IS or ISN'T in stock (no "Ow thiyenawa", "yes we have it", "sorry we don't have it", etc.).
- Keep your reply to a brief, neutral acknowledgement only, e.g. "Sure, checking now!" / "බලන්නම්!" / "One sec, let me look."

Classify the customer's intent as exactly one of:
- "buy_product": they want to buy a finished/packaged product (e.g. "do you have cake", "l'oreal shampoo 330ml thiyenwd", "vaseline body lotion nadda"). Search for that PRODUCT itself.
- "recipe_ingredients": they explicitly say they want to COOK/BAKE/MAKE a dish themselves (e.g. "I want to bake a cake", "mata cake ekak hadanna one"). Only in this case, search for the RAW INGREDIENTS (flour, sugar, eggs, butter, etc.), not the finished dish.
- "chitchat": no product intent at all (greeting, thanks, unrelated small talk).
IMPORTANT: a request to BUY a cake/cookie/dessert is "buy_product" with search_terms like ["chocolate cake"] — never "recipe_ingredients". Only use "recipe_ingredients" when the customer's own words say they want to make/cook/bake it themselves.

Also extract, when present in the message (or clearly implied by a short follow-up like "ko" / "eeka" / "that one" referring to what was just being discussed):
- search_terms: concrete, plain generic product names (not brand names), at most 6. Use the SAME specific category word the customer used — do not broaden or substitute a different (even if related) category. Keep "face wash" as "face wash", never substitute "cleanser" or "moisturizer"; keep "cleanser" as "cleanser".
- brand: a brand name if the customer named one (e.g. "L'Oreal", "Kotmale"), else null.
- variant: a specific size/volume/flavor/color if named (e.g. "330ml", "500g"), else null.
- max_price / min_price: a price ceiling/floor in LKR if the customer gave one (e.g. "under 2000" -> max_price=2000), else null.
- is_followup: true if this message has no new product info of its own and is just referring back to what was already being discussed (e.g. "ko", "ko nane", "where is it", "show me"), else false.

If the message isn't about groceries/products at all, use intent "chitchat" and return an empty search_terms list.

Reply with ONLY a JSON object, no other text, in exactly this shape:
{"reply": "your conversational reply", "intent": "buy_product", "search_terms": ["term1"], "brand": null, "variant": null, "max_price": null, "min_price": null, "is_followup": false}
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
        redis: Redis,
        session_id: str,
        history: List[Dict[str, Any]],
        user_message: str,
        reply: str,
        search_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        assistant_turn: Dict[str, Any] = {"role": "assistant", "text": reply}
        if search_context:
            # Carried forward so a pronoun-only follow-up ("ko", "ko nane")
            # can reuse what was actually being searched for instead of
            # coming back empty — see _last_search_context.
            assistant_turn["search_context"] = search_context
        history = history + [
            {"role": "user", "text": user_message},
            assistant_turn,
        ]
        history = history[-(MAX_HISTORY_TURNS * 2):]
        await redis.set(
            RedisKeys.chat_history(session_id),
            json.dumps(history),
            ex=RedisTTL.CHAT_HISTORY,
        )

    @staticmethod
    def _last_search_context(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Most recent assistant turn's search context, if any."""
        for turn in reversed(history):
            if turn.get("role") == "assistant" and turn.get("search_context"):
                return turn["search_context"]
        return None

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
                intent = str(data.get("intent") or "buy_product").strip().lower()
                if intent not in ("buy_product", "recipe_ingredients", "chitchat"):
                    intent = "buy_product"

                def _num(value):
                    try:
                        return float(value) if value is not None else None
                    except (TypeError, ValueError):
                        return None

                brand = data.get("brand")
                variant = data.get("variant")
                return {
                    "reply": reply,
                    "search_terms": terms,
                    "intent": intent,
                    "brand": str(brand).strip() if brand else None,
                    "variant": str(variant).strip() if variant else None,
                    "max_price": _num(data.get("max_price")),
                    "min_price": _num(data.get("min_price")),
                    "is_followup": bool(data.get("is_followup")),
                }
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass

        logger.warning("Chat: Gemini reply was not the expected JSON shape, using raw text")
        return {
            "reply": cleaned,
            "search_terms": [],
            "intent": "buy_product",
            "brand": None,
            "variant": None,
            "max_price": None,
            "min_price": None,
            "is_followup": False,
        }

    # ------------------------------------------------------------------
    # Product matching
    # ------------------------------------------------------------------

    @staticmethod
    async def _match_products(
        db: AsyncSession,
        branch_id: Optional[UUID],
        search_terms: List[str],
        brand: Optional[str] = None,
        variant: Optional[str] = None,
        max_price: Optional[float] = None,
        min_price: Optional[float] = None,
        lang: str = DEFAULT_LANGUAGE,
    ) -> "tuple[List[dict], List[dict]]":
        """
        Returns (strict_matches, broad_matches).

        ``broad_matches`` is the relevance-filtered candidate pool *before*
        brand/variant are enforced — kept so a reply can say "we don't have
        the 330ml, here's the 440ml we do have" grounded in a REAL product,
        instead of the LLM guessing or the chat coming back empty.
        """
        if not branch_id or not search_terms:
            return [], []

        from app.api.v1.products import format_product

        def _norm(value: Optional[str]) -> str:
            return unicodedata.normalize("NFC", (value or "").strip()).lower()

        def _tight(value: Optional[str]) -> str:
            return _norm(value).replace(" ", "")

        brand_norm = _norm(brand)
        variant_tight = _tight(variant)

        seen: set = set()
        broad_matches: List[dict] = []
        strict_matches: List[dict] = []

        for term in search_terms:
            # Folding the brand into the query string leans on the existing
            # trigram/ILIKE search for better ranking; the brand/variant
            # checks below are the actual hard filter.
            query = f"{brand} {term}".strip() if brand else term
            items, _total = await ProductService.get_all_for_branch(
                db,
                branch_id=branch_id,
                search=query,
                max_price=max_price,
                min_price=min_price,
                limit=MAX_MATCHES_PER_TERM * 3,
            )

            term_tokens = [t for t in _norm(term).split() if len(t) > 2]

            for item in items:
                product = item["product"]
                pid = product.product_id
                if pid in seen:
                    continue

                name_norm = _norm(product.name)
                cat_norm = _norm(product.category.name if product.category else None)
                subcat_norm = _norm(product.subcategory.name if product.subcategory else None)

                # Category-relevance guard: the shared fuzzy/trigram search
                # can surface a loosely-related product (e.g. a moisturizer
                # whose description happens to mention "wash"). Only accept
                # it here if the searched word genuinely appears in its name
                # or category — otherwise a single generic search_term ends
                # up mixing unrelated product types into one chat reply.
                if term_tokens and not any(
                    tok in name_norm or tok in cat_norm or tok in subcat_norm
                    for tok in term_tokens
                ):
                    continue

                seen.add(pid)
                # compute_effective_data already resolves branch-vs-global
                # cashback/price/discount, so this is the same effective
                # data every other product listing shows — no separate
                # cashback lookup needed.
                formatted = format_product(
                    product, branch_overrides=item["effective"], lang=lang
                )
                broad_matches.append(formatted)

                if brand_norm and brand_norm not in name_norm:
                    continue
                if variant_tight and variant_tight not in name_norm.replace(" ", ""):
                    continue
                strict_matches.append(formatted)

        return strict_matches, broad_matches

    # ------------------------------------------------------------------
    # Reply grounding
    # ------------------------------------------------------------------

    @staticmethod
    def _ground_reply(
        llm_reply: str,
        intent: str,
        search_terms: List[str],
        brand: Optional[str],
        variant: Optional[str],
        strict_matches: List[dict],
        broad_matches: List[dict],
    ) -> str:
        """
        Overrides the LLM's reply with a deterministic, DB-grounded one
        whenever the LLM has no way of knowing the real answer. The system
        prompt tells Gemini never to assert availability, but a code-level
        guard makes that guaranteed rather than merely requested — this is
        exactly what the reported hallucination ("Ow, thiyenawa!") and fake
        "podi delay ekakata" apology bugs came from: an LLM reply written
        with zero visibility into real stock.
        """
        if intent == "chitchat" or not search_terms:
            return llm_reply

        asked = " ".join(x for x in (brand, search_terms[0], variant) if x)

        if not strict_matches and not broad_matches:
            return f"Sorry, we don't have {asked} in stock right now." if asked else llm_reply

        if not strict_matches and broad_matches:
            alt_names = list(dict.fromkeys(m["name"] for m in broad_matches))[:MAX_MATCHES_PER_TERM]
            alt_text = ", ".join(alt_names)
            if asked:
                return f"Sorry, we don't have {asked} right now — but we do have {alt_text}."
            return llm_reply

        return llm_reply

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

        intent = result["intent"]
        search_terms = result["search_terms"]
        brand = result["brand"]
        variant = result["variant"]
        max_price = result["max_price"]
        min_price = result["min_price"]

        # Pronoun-only follow-ups ("ko", "ko nane") often carry no new
        # product info — reuse the last turn's search context instead of
        # coming back with no cards and an invented excuse.
        if (result["is_followup"] or not search_terms) and intent != "chitchat":
            last_context = ChatService._last_search_context(history)
            if last_context:
                search_terms = search_terms or last_context.get("search_terms") or []
                brand = brand or last_context.get("brand")
                variant = variant or last_context.get("variant")
                if max_price is None:
                    max_price = last_context.get("max_price")
                if min_price is None:
                    min_price = last_context.get("min_price")

        strict_matches, broad_matches = await ChatService._match_products(
            db,
            branch_id,
            search_terms,
            brand=brand,
            variant=variant,
            max_price=max_price,
            min_price=min_price,
            lang=lang,
        )

        reply = ChatService._ground_reply(
            result["reply"], intent, search_terms, brand, variant, strict_matches, broad_matches
        )
        products = strict_matches or broad_matches[:MAX_MATCHES_PER_TERM]

        search_context = (
            {
                "search_terms": search_terms,
                "brand": brand,
                "variant": variant,
                "max_price": max_price,
                "min_price": min_price,
            }
            if search_terms
            else None
        )
        await ChatService._append_turn(redis, session_id, history, message, reply, search_context)

        return {"reply": reply, "products": products}
