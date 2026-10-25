"""
Meaning-based product lookup for the shopping assistant (SEMANTIC_SEARCH_ENABLED).

The keyword index (catalog_search_service) finds what a product is *called*;
it cannot connect "සෙම්ප්‍රතිශ්‍යාවට" to cough syrup or "something sweet for
tea" to a tea bun. Here every product's names, category and search keywords
are embedded once with Gemini, each API worker holds the vectors in memory,
and a query is embedded and compared against them.

Only the assistant's search tool uses this, and only when the keyword search
came back thin -- the customer search bar is unchanged.

Measured 2026-10-25 on the production catalogue: gemini-embedding-2 put cough
syrups first for "සෙම්ප්‍රතිශ්‍යාවට මොනවද තියෙන්නේ" and hair oils for a Tamil
"hair fall" query, where gemini-embedding-001 returned curry powder. Romanised
Sinhala is weaker; the agent usually rewrites the need into product words
before it searches, which is what this sees.

No pgvector (production has no vector extension): the vectors live in a plain
REAL[] column, read and written with raw SQL so ordinary product queries never
load them.
"""

import asyncio
import hashlib
import math
import time
from typing import Dict, List, Optional, Tuple

import httpx
from loguru import logger
from sqlalchemy import text

from app.config.settings import settings
from app.services.embedding_service import GeminiAPIError

API = "https://generativelanguage.googleapis.com/v1beta"
BATCH = 100
INDEX_TTL_SECONDS = 300
WORKER_INTERVAL_SECONDS = 600
REQUEST_TIMEOUT = 20.0
# Results must score at least this share of the best hit -- the absolute
# scale differs between models, the ranking does not.
RELATIVE_CUTOFF = 0.85
_LOCK_ID = 880088  # pg advisory lock: one API worker embeds at a time

_ROWS = text(
    """
    SELECT p.product_id, p.name, p.name_si, p.name_ta, p.search_keywords,
           c.name AS category, s.name AS subcategory, p.search_embedding_key
    FROM products p
    LEFT JOIN categories c ON c.category_id = p.category_id
    LEFT JOIN categories s ON s.category_id = p.subcategory_id
    WHERE p.is_active
    """
)
_VECTORS = text(
    "SELECT product_id, search_embedding FROM products "
    "WHERE is_active AND search_embedding IS NOT NULL"
)
_SAVE = text(
    "UPDATE products SET search_embedding = :vector, search_embedding_key = :key "
    "WHERE product_id = :product_id"
)


def document_text(row) -> str:
    """What a product is embedded as: every name it goes by, and where it sits."""
    parts = (row.name, row.name_si, row.name_ta, row.category, row.subcategory, row.search_keywords)
    return " | ".join(p for p in parts if p)


def embedding_key(document: str) -> str:
    model = settings.gemini_embedding_model
    dims = settings.gemini_embedding_dimensions
    return hashlib.sha256(f"{model}|{dims}|{document}".encode("utf-8")).hexdigest()


def _normalise(vector: List[float]) -> List[float]:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


async def embed(texts: List[str], task: str) -> List[List[float]]:
    """Normalised embeddings, in order. ``task`` is RETRIEVAL_DOCUMENT or RETRIEVAL_QUERY."""
    api_key = getattr(settings, "gemini_api_key", None)
    if not api_key:
        raise GeminiAPIError("GEMINI_API_KEY not configured")
    model = settings.gemini_embedding_model
    out: List[List[float]] = []
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        for start in range(0, len(texts), BATCH):
            body = {
                "requests": [
                    {
                        "model": f"models/{model}",
                        "content": {"parts": [{"text": t}]},
                        "taskType": task,
                        "outputDimensionality": settings.gemini_embedding_dimensions,
                    }
                    for t in texts[start : start + BATCH]
                ]
            }
            try:
                response = await client.post(
                    f"{API}/models/{model}:batchEmbedContents",
                    json=body,
                    headers={"x-goog-api-key": api_key},
                )
            except httpx.RequestError as exc:
                raise GeminiAPIError(f"Embedding request failed: {type(exc).__name__}") from exc
            if response.status_code != 200:
                raise GeminiAPIError(
                    f"Embedding returned {response.status_code}",
                    status_code=response.status_code,
                    retryable=response.status_code in (429, 500, 502, 503, 504),
                )
            out += [_normalise(e["values"]) for e in response.json()["embeddings"]]
    return out


# ---------------------------------------------------------------------------
# Keeping the column filled
# ---------------------------------------------------------------------------


async def refresh_embeddings(session_maker, limit: Optional[int] = None) -> int:
    """Embed every active product whose text changed since it was embedded. Returns the count."""
    async with session_maker() as db:
        locked = (await db.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": _LOCK_ID})).scalar()
        if not locked:
            return 0
        try:
            rows = (await db.execute(_ROWS)).all()
            stale: List[Tuple[str, str, str]] = []
            for row in rows:
                document = document_text(row)
                key = embedding_key(document)
                if key != row.search_embedding_key:
                    stale.append((row.product_id, document, key))
            stale = stale[:limit] if limit else stale
            done = 0
            for start in range(0, len(stale), BATCH):
                chunk = stale[start : start + BATCH]
                vectors = await embed([doc for _, doc, _ in chunk], "RETRIEVAL_DOCUMENT")
                for (product_id, _, key), vector in zip(chunk, vectors, strict=True):
                    await db.execute(_SAVE, {"vector": vector, "key": key, "product_id": product_id})
                await db.commit()
                done += len(chunk)
            return done
        finally:
            await db.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": _LOCK_ID})
            await db.commit()


async def run_embedding_worker() -> None:
    """Background loop: keeps embeddings current while the switch is on."""
    from app.config.database import async_session_maker

    while True:
        if settings.semantic_search_enabled:
            try:
                done = await refresh_embeddings(async_session_maker)
                if done:
                    logger.info("Semantic search: embedded {} products", done)
            except Exception as exc:  # noqa: BLE001 - retried next round
                logger.warning("Semantic search refresh failed: {}", exc)
        await asyncio.sleep(WORKER_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# The in-memory index and the lookup
# ---------------------------------------------------------------------------

_index: Dict[str, List[float]] = {}
_loaded_at = 0.0
_load_lock = asyncio.Lock()


async def _vectors(db) -> Dict[str, List[float]]:
    global _index, _loaded_at
    if _index and time.monotonic() - _loaded_at < INDEX_TTL_SECONDS:
        return _index
    async with _load_lock:
        if not _index or time.monotonic() - _loaded_at >= INDEX_TTL_SECONDS:
            rows = (await db.execute(_VECTORS)).all()
            _index = {str(r.product_id): list(r.search_embedding) for r in rows}
            _loaded_at = time.monotonic()
    return _index


def rank(query_vector: List[float], vectors: Dict[str, List[float]], limit: int) -> List[str]:
    # A row embedded at another size (before the worker caught up with a
    # settings change) cannot be compared, so it sits this one out.
    scored = sorted(
        (
            (sum(a * b for a, b in zip(query_vector, v, strict=True)), pid)
            for pid, v in vectors.items()
            if len(v) == len(query_vector)
        ),
        reverse=True,
    )
    if not scored or scored[0][0] <= 0:
        return []
    floor = scored[0][0] * RELATIVE_CUTOFF
    return [pid for score, pid in scored[:limit] if score >= floor]


async def related_product_ids(db, query: str, limit: int = 6) -> List[str]:
    """Active product IDs closest in meaning to ``query``, best first (not branch-filtered)."""
    vectors = await _vectors(db)
    if not vectors:
        return []
    (query_vector,) = await embed([query], "RETRIEVAL_QUERY")
    return rank(query_vector, vectors, limit)
