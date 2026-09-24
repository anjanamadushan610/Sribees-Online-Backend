"""
Gemini Keyword Suggestion Service — AI-assisted multilingual search keywords.

A one-shot text-generation call (not embeddings): given a product's name and
description, asks Gemini for the other *words* Sri Lankan shoppers use for it
in Sinhala, Tamil, Singlish, Tamilish and English. The admin panel calls this
once, when a product is being created or edited, and the admin reviews/edits
the result before it's saved to ``Product.search_keywords``.

Deliberately never on the customer search path (see
``catalog_search_service``) — a live search request never waits on or pays
for an AI call. This only runs when an admin clicks "Suggest" on the product
form, so cost and latency stay bounded to roughly once per product, not once
per shopper query.

Spelling variation is NOT asked for. The search normaliser already folds
"biththara"/"bittara"/"බිත්තර" onto one key and tolerates one or two typos,
so AI-generated misspellings would only be noise. What the AI is good at, and
an algorithm is not, is knowing that banana is also "kesel", "kehel" and
"வாழைப்பழம்".
"""
import asyncio
import hashlib
import json
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from loguru import logger
from redis.asyncio import Redis

from app.config.settings import settings
from app.services.embedding_service import GeminiAPIError
from app.services.search_text import normalize_keywords, split_keywords

GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
REQUEST_TIMEOUT = 30.0
MAX_RETRIES = 2
RETRY_DELAY = 1.5
MAX_TERM_LENGTH = 60
CACHE_TTL_SECONDS = 30 * 86400
_COLOMBO = ZoneInfo("Asia/Colombo")


class SuggestionBudgetExceeded(Exception):
    """Today's ``gemini_keyword_daily_limit`` is used up."""

    def __init__(self, limit: int):
        super().__init__(f"daily limit of {limit} AI keyword suggestions reached")
        self.limit = limit

_PROMPT_TEMPLATE = """You help a Sri Lankan grocery app's shoppers find products no matter which language or script they search in.

Product name: {name}
Description: {description}
Category: {category}

List the other WORDS a Sri Lankan shopper would type to find this product:
- sinhala: the Sinhala name(s) in Sinhala script, including colloquial names
- tamil: the Tamil name(s) in Tamil script
- singlish: those Sinhala names written in English letters as people really type them (e.g. banana: kesel, kehel, ambul kesel)
- tamilish: those Tamil names written in English letters (e.g. banana: vazhaipazham)
- english: other English names, British/American variants and common generic or brand-as-generic names (e.g. curd, yoghurt)

Rules:
- Do NOT list spelling variants that differ only by doubled letters, th/t, dh/d, ee/i or one wrong letter; the search engine already handles those.
- Do NOT repeat the product name itself, pack sizes, prices or marketing words.
- Leave a list empty if the product genuinely has no common name in that language.
- At most 8 terms per list.
"""

_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        key: {"type": "ARRAY", "items": {"type": "STRING"}}
        for key in ("sinhala", "tamil", "singlish", "tamilish", "english")
    },
    "required": ["sinhala", "tamil", "singlish", "tamilish", "english"],
}


def _flatten(answer: str) -> str:
    """Gemini's JSON (or, if it ignored the schema, plain text) → keyword list."""
    try:
        data = json.loads(answer)
    except json.JSONDecodeError:
        terms = split_keywords(answer)
    else:
        if not isinstance(data, dict):
            raise GeminiAPIError("Unexpected keyword suggestion shape")
        terms = [
            term
            for key in _RESPONSE_SCHEMA["properties"]
            for term in (data.get(key) or [])
            if isinstance(term, str)
        ]
    terms = [t.strip() for t in terms if t and len(t.strip()) <= MAX_TERM_LENGTH]
    return normalize_keywords(", ".join(terms)) or ""


async def suggest_search_keywords(
    name: str,
    description: Optional[str] = None,
    category: Optional[str] = None,
) -> str:
    """
    Ask Gemini for Sinhala/Tamil/Singlish/Tamilish/English alternate names for
    one product.

    Returns a comma-separated string ready to drop into (or merge with)
    ``Product.search_keywords`` — the admin reviews it before saving, so this
    does not need to be perfect, just a useful starting point.

    Raises ``GeminiAPIError`` if no API key is configured or the call fails
    after retries.
    """
    api_key = getattr(settings, "gemini_api_key", None)
    if not api_key:
        raise GeminiAPIError("GEMINI_API_KEY not configured")

    prompt = _PROMPT_TEMPLATE.format(
        name=name.strip(),
        description=(description or "").strip()[:1000] or "(none)",
        category=(category or "").strip() or "(none)",
    )

    model = getattr(settings, "gemini_generation_model", "gemini-flash-latest")
    url = f"{GEMINI_API_BASE_URL}/models/{model}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 1024,
            "responseMimeType": "application/json",
            "responseSchema": _RESPONSE_SCHEMA,
            # Recalling names is lookup, not reasoning: thinking would only
            # add latency and billed output tokens.
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    # The key goes in a header, not the query string, so it can never end up
    # in a logged URL.
    headers = {"x-goog-api-key": api_key}

    last_error: Optional[Exception] = None

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = await client.post(url, json=payload, headers=headers)
            except httpx.RequestError as exc:
                last_error = exc
                logger.warning(f"Gemini keyword suggestion request error: {type(exc).__name__}")
            else:
                if response.status_code == 200:
                    data = response.json()
                    try:
                        answer = data["candidates"][0]["content"]["parts"][0]["text"]
                    except (KeyError, IndexError):
                        raise GeminiAPIError("Unexpected Gemini response shape")
                    return _flatten(answer)

                if response.status_code in (429, 500, 502, 503, 504):
                    last_error = GeminiAPIError(
                        f"Gemini returned {response.status_code}",
                        status_code=response.status_code,
                        retryable=True,
                    )
                    logger.warning(
                        f"Gemini keyword suggestion error "
                        f"(attempt {attempt + 1}/{MAX_RETRIES + 1}): {response.status_code}"
                    )
                else:
                    raise GeminiAPIError(
                        f"Gemini returned {response.status_code}: {response.text[:200]}",
                        status_code=response.status_code,
                    )

            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)

    raise last_error or GeminiAPIError("Keyword suggestion failed after retries")


async def suggest_within_budget(
    redis: Optional[Redis],
    name: str,
    description: Optional[str] = None,
    category: Optional[str] = None,
) -> dict:
    """
    ``suggest_search_keywords`` behind a cache and a daily ceiling.

    The same product asked about twice (a second click, a re-opened form) is
    answered from Redis for free. Only calls that actually reach Gemini count
    toward ``settings.gemini_keyword_daily_limit``, and a failed call gives its
    slot back. There is deliberately no bulk path: one click, one product.

    Returns ``{"keywords", "cached", "remaining_today"}``; raises
    ``SuggestionBudgetExceeded`` once the day's allowance is spent.
    """
    limit = settings.gemini_keyword_daily_limit
    fingerprint = json.dumps(
        [name.strip().casefold(), (description or "").strip()[:1000],
         (category or "").strip().casefold()],
        ensure_ascii=False,
    )
    cache_key = "kw-suggest:v2:" + hashlib.sha256(fingerprint.encode()).hexdigest()
    count_key = f"kw-suggest:count:{datetime.now(_COLOMBO).date().isoformat()}"

    if redis is not None:
        cached = await redis.get(cache_key)
        if cached is not None:
            used = int(await redis.get(count_key) or 0)
            return {"keywords": cached, "cached": True,
                    "remaining_today": max(0, limit - used)}

    if limit <= 0:
        raise SuggestionBudgetExceeded(limit)
    used = 0
    if redis is not None:
        used = await redis.incr(count_key)
        if used == 1:
            await redis.expire(count_key, 2 * 86400)
        if used > limit:
            await redis.decr(count_key)
            raise SuggestionBudgetExceeded(limit)
    else:
        logger.warning("Redis unavailable: AI keyword suggestion is not rate-limited")

    try:
        keywords = await suggest_search_keywords(name, description, category)
    except Exception:
        if redis is not None:
            await redis.decr(count_key)
        raise

    if redis is not None and keywords:
        await redis.set(cache_key, keywords, ex=CACHE_TTL_SECONDS)
    return {"keywords": keywords, "cached": False,
            "remaining_today": max(0, limit - used)}
