"""
Search insights — learning shoppers' words from the searches that fail.

Every zero-result customer search is a shopper telling us a word the catalog
doesn't know yet. This module records them (``note_search``, run as a
background task after the response has gone out, so search latency is
untouched) and serves the admin inbox that turns each one into a search
keyword on the right product with one click (``attach``). No AI is involved
at any step — the shopper supplies the word, a person supplies the judgement.

Noise control, because a search box is typed into one letter at a time:

- a miss counts only once the shopper has stopped typing — if they search
  again within ``SETTLE_SECONDS`` the earlier query was a word on its way
  somewhere, not a request;
- each shopper counts once per query per day (Redis), so ``shoppers`` means
  people, not retries;
- misses are grouped by their folded words, spaces ignored, so "Seeni",
  "seeni" and "සීනි" are one inbox row, and so are "rasa kavili" and
  "rasakavili";
- one-shopper misses nobody acted on are pruned after ``PRUNE_AFTER_DAYS``.

No shopper identity reaches the database; Redis holds only a truncated hash
of the session id, and only for a day.
"""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from loguru import logger
from redis.asyncio import Redis
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.product import Product
from app.models.search_miss import SearchMiss, SearchMissStatus
from app.services import catalog_search_service as catalog_search
from app.services.search_text import normalize_keywords, parse_query

SETTLE_SECONDS = 2.5
MAX_QUERY_CHARS = 80
MAX_WORDS = 6
PRUNE_AFTER_DAYS = 45
_COLOMBO = ZoneInfo("Asia/Colombo")


def miss_key(raw_query: str) -> Optional[str]:
    """The inbox grouping key for a query, or None if it isn't worth recording."""
    if not raw_query or len(raw_query.strip()) > MAX_QUERY_CHARS:
        return None
    parsed = parse_query(raw_query)
    if not parsed.required or len(parsed.required) > MAX_WORDS:
        return None
    if not any(word.isalpha() for word in parsed.required):
        return None  # "500", "kg": nothing an admin could attach to a product
    # Run together, so "rasa kavili" and "rasakavili" are one row too.
    key = parsed.phrase
    return key if len(key) >= 3 else None


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


async def note_search(
    session_maker: async_sessionmaker,
    redis: Optional[Redis],
    session_id: str,
    raw_query: str,
    total: int,
    branch_id: Optional[UUID],
) -> None:
    """
    Background task after every customer search. Never raises.

    Every search marks itself as the shopper's latest; a zero-result one then
    waits ``SETTLE_SECONDS`` and records itself only if it is still the latest.
    """
    try:
        shopper = _digest(session_id)
        latest_key = f"search:latest:{shopper}"
        stamp = f"{datetime.now(timezone.utc).timestamp():.6f}:{_digest(raw_query)}"
        if redis is not None:
            await redis.set(latest_key, stamp, ex=60)

        key = miss_key(raw_query) if total == 0 else None
        if key is None:
            return

        await asyncio.sleep(SETTLE_SECONDS)
        if redis is not None:
            if await redis.get(latest_key) != stamp:
                return  # they kept typing
            today = datetime.now(_COLOMBO).date().isoformat()
            first_today = await redis.set(
                f"search:miss:{today}:{shopper}:{_digest(key)}", 1, nx=True, ex=2 * 86400
            )
            if not first_today:
                return

        async with session_maker() as db:
            upsert = insert(SearchMiss).values(
                query_key=key,
                sample_query=raw_query.strip(),
                shoppers=1,
                last_branch_id=branch_id,
            )
            await db.execute(upsert.on_conflict_do_update(
                index_elements=[SearchMiss.query_key],
                set_={
                    "shoppers": SearchMiss.shoppers + 1,
                    "sample_query": upsert.excluded.sample_query,
                    "last_seen": func.now(),
                    "last_branch_id": upsert.excluded.last_branch_id,
                },
            ))
            await db.commit()
    except Exception as e:  # a lost insight must never surface to a shopper
        logger.warning(f"Search miss not recorded: {e}")


async def list_misses(db: AsyncSession, status: str, limit: int = 100) -> list[dict]:
    """
    The inbox, busiest first, each row with the catalog's own best guesses.

    ``catalog_matches`` runs the shopper's query against the whole catalog,
    ignoring branch stock. Matches on an open miss usually mean the product
    exists but wasn't in stock where they searched — a stock problem, not a
    words problem — or that someone has since added the keyword.
    """
    rows = (await db.execute(
        select(SearchMiss, Product.name)
        .outerjoin(Product, Product.product_id == SearchMiss.resolved_product_id)
        .where(SearchMiss.status == status)
        .order_by(SearchMiss.shoppers.desc(), SearchMiss.last_seen.desc())
        .limit(limit)
    )).all()
    if not rows:
        return []

    vocabulary = await catalog_search.get_vocabulary(db)
    plans = {miss.query_key: catalog_search.make_plan(vocabulary, miss.sample_query)
             for miss, _ in rows}
    terms = sorted({t for plan in plans.values() if plan for t in plan.terms})
    candidates = []
    if terms:
        candidates = (await db.execute(
            select(*catalog_search.CANDIDATE_COLUMNS)
            .where(Product.is_active.is_(True), Product.search_tokens.overlap(terms))
            .limit(catalog_search.MAX_CANDIDATES)
        )).all()
    names = {row[0]: row[-1] for row in candidates}

    items = []
    for miss, resolved_name in rows:
        plan = plans[miss.query_key]
        matches = catalog_search.rank(candidates, plan)[:3] if plan else []
        items.append({
            "query_key": miss.query_key,
            "sample_query": miss.sample_query,
            "shoppers": miss.shoppers,
            "first_seen": miss.first_seen.isoformat() if miss.first_seen else None,
            "last_seen": miss.last_seen.isoformat() if miss.last_seen else None,
            "status": miss.status,
            "resolved_product_id": str(miss.resolved_product_id) if miss.resolved_product_id else None,
            "resolved_product_name": resolved_name,
            "resolved_at": miss.resolved_at.isoformat() if miss.resolved_at else None,
            # Resolved, yet shoppers still come up empty: the product is out
            # of stock where they are, or the keyword went on the wrong one.
            "missed_since_resolved": bool(
                miss.resolved_at and miss.last_seen and miss.last_seen > miss.resolved_at
            ),
            "catalog_matches": [{"product_id": str(pid), "name": names[pid]} for pid in matches],
        })
    return items


async def attach(db: AsyncSession, query_key: str, product_id: UUID, admin_id: UUID) -> SearchMiss:
    """
    Teach search the shopper's word: add it to the product's keywords as they
    typed it, and close the inbox row. The product's search index updates in
    the same flush (catalog_search_service's mapper events).
    """
    miss = await db.get(SearchMiss, query_key)
    if miss is None:
        raise LookupError("search miss not found")
    product = await db.get(Product, product_id)
    if product is None:
        raise LookupError("product not found")

    product.search_keywords = normalize_keywords(
        ", ".join(filter(None, (product.search_keywords, miss.sample_query)))
    )
    miss.status = SearchMissStatus.RESOLVED
    miss.resolved_product_id = product.product_id
    miss.resolved_by = admin_id
    miss.resolved_at = datetime.now(timezone.utc)
    await db.flush()
    return miss


async def set_status(db: AsyncSession, query_key: str, status: str) -> SearchMiss:
    """Ignore a miss, or put one back in the inbox. Resolving goes through ``attach``."""
    if status not in (SearchMissStatus.OPEN, SearchMissStatus.IGNORED):
        raise ValueError("status must be 'open' or 'ignored'")
    miss = await db.get(SearchMiss, query_key)
    if miss is None:
        raise LookupError("search miss not found")
    miss.status = status
    if status == SearchMissStatus.OPEN:
        miss.resolved_product_id = miss.resolved_by = miss.resolved_at = None
    await db.flush()
    return miss


async def prune(session_maker: async_sessionmaker) -> int:
    """Drop one-shopper misses nobody acted on. Runs at startup."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=PRUNE_AFTER_DAYS)
    async with session_maker() as db:
        result = await db.execute(
            delete(SearchMiss).where(
                SearchMiss.status != SearchMissStatus.RESOLVED,
                SearchMiss.shoppers <= 1,
                SearchMiss.last_seen < cutoff,
            )
        )
        await db.commit()
    return result.rowcount or 0
