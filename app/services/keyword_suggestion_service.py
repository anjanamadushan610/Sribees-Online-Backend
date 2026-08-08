"""
Gemini Keyword Suggestion Service — AI-assisted multilingual search keywords.

A one-shot text-generation call (not embeddings): given a product's name and
description, asks Gemini for Sinhala/Tamil/Singlish/Tamilish alternate names
and common misspellings as a single comma-separated string. The admin panel
calls this once, when a product is being created or edited, and the admin
reviews/edits the result before it's saved to ``Product.search_keywords``.

Deliberately never on the customer search path (see
``ProductService.get_all_for_branch``) — a live search request never waits on
or pays for an AI call. This only runs when an admin clicks "Suggest" on the
product form, so cost and latency stay bounded to roughly once per product,
not once per shopper query.
"""
import asyncio
import unicodedata
from typing import Optional

import httpx
from loguru import logger

from app.config.settings import settings
from app.services.embedding_service import GeminiAPIError

GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
# Current Gemini flash models "think" before answering (observed: several
# hundred tokens of internal reasoning even for this short a prompt), which
# can take noticeably longer than a typical API call — 30s was measured to
# be too tight for some products.
REQUEST_TIMEOUT = 60.0
MAX_RETRIES = 2
RETRY_DELAY = 1.5

_PROMPT_TEMPLATE = """You help a Sri Lankan grocery app's shoppers find products no matter how they search.

Product name: {name}
Description: {description}
Category: {category}

Give alternate search terms a Sri Lankan shopper might type for this product:
- Sinhala script (native spelling)
- Tamil script (native spelling)
- Singlish (Sinhala words written in English/Latin letters)
- Tamilish (Tamil words written in English/Latin letters)
- Common English misspellings

Reply with ONLY a single comma-separated list of terms — no headings, no
numbering, no explanation, no quotes around the whole list. Skip a language
if the product genuinely has no common name in it. Keep it under 30 terms.
"""


async def suggest_search_keywords(
    name: str,
    description: Optional[str] = None,
    category: Optional[str] = None,
) -> str:
    """
    Ask Gemini for Sinhala/Tamil/Singlish/Tamilish search keywords for one
    product.

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
        description=(description or "").strip() or "(none)",
        category=(category or "").strip() or "(none)",
    )

    model = getattr(settings, "gemini_generation_model", "gemini-flash-latest")
    url = f"{GEMINI_API_BASE_URL}/models/{model}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        # 2048, not a couple hundred: current Gemini flash models spend part
        # of the output budget on an internal "thinking" pass before the
        # visible answer (~500-600 tokens for this prompt, observed) — too
        # low a cap here truncates the actual keyword list, not the
        # reasoning. Billed the same either way; this is a once-per-product
        # admin action, not a per-search cost.
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 2048},
    }

    last_error: Optional[Exception] = None

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = await client.post(url, json=payload, params={"key": api_key})
            except httpx.RequestError as exc:
                last_error = exc
                logger.warning(f"Gemini keyword suggestion request error: {exc}")
            else:
                if response.status_code == 200:
                    data = response.json()
                    try:
                        text = data["candidates"][0]["content"]["parts"][0]["text"]
                    except (KeyError, IndexError):
                        raise GeminiAPIError("Unexpected Gemini response shape")
                    return unicodedata.normalize("NFC", text.strip())

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
