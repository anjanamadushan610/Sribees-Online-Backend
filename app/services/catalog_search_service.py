"""
Catalog search — the ranked, typo-tolerant, any-script lookup behind the
customer search bar.

How one query runs:

1. ``search_text.parse_query`` turns the shopper's text — Sinhala, Tamil,
   Singlish or English — into folded tokens (see that module).
2. Each token is **expanded in memory** against the catalog's vocabulary:
   the token itself, words it is a prefix of (the shopper is still typing),
   and words within one or two edits of it ("suger" → "sugar", "kehel" →
   "kesel"). This is a SymSpell-style delete index, so a lookup is a few
   dictionary probes — microseconds, no database round trip, no AI call.
3. **One indexed query** fetches the branch-visible products whose
   ``search_tokens`` overlap any expanded word (GIN on a text[] column).
4. The candidates are **ranked in Python**: how many of the query's words
   they matched first, then match quality (exact > prefix > typo, own name >
   alternate keyword, whole-phrase hits on top), then featured and
   popularity. When some product matches every word, only those are shown;
   when none does ("kiri packet"), the best partial matches are.

The vocabulary is each worker's own copy of every distinct token in the
catalog. It is rebuilt when the catalog's fingerprint changes, checked at
most every ``VOCAB_TTL_SECONDS`` — so a new product's exact words are
searchable immediately, and its prefix/typo matches within that window.

Product rows stay indexed through the mapper events at the bottom of this
module (every ORM write) and ``reindex_stale`` at startup (raw-SQL writes, and
every row after ``search_text.INDEX_VERSION`` is bumped).
"""
from __future__ import annotations

import asyncio
import bisect
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from loguru import logger
from sqlalchemy import event, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.product import Product
from app.services.search_text import (
    INDEX_VERSION,
    NAME_PHRASE_MARK,
    ParsedQuery,
    index_tokens,
    parse_query,
)

VOCAB_TTL_SECONDS = 30.0
MAX_PREFIX_EXPANSIONS = 50
MAX_CANDIDATES = 5000

# Match-quality weights. Only their order matters to shoppers; the gaps are
# sized so one exact hit always beats one typo hit.
W_EXACT = 1.0
W_PREFIX_BASE, W_PREFIX_SPAN = 0.55, 0.35   # 0.55..0.90 by how much was typed
W_TYPO = {1: 0.7, 2: 0.5}
KEYWORD_ONLY_FACTOR = 0.85                  # hit an alternate keyword, not the name
W_SOFT = 0.05                               # "5kg" nudges the 5 kg pack up
W_NAME_PHRASE = 0.6                         # query is the product's name
W_KEYWORD_PHRASE = 0.5                      # query is one of its keywords
W_STEM_FACTOR = 0.95                        # "හාල" read as "හාල්"

# Arbitrary, fixed: the advisory-lock id that keeps concurrent workers from
# reindexing the same rows at the same start-up.
_REINDEX_LOCK_KEY = 7_406_311_562


def max_edits(length: int) -> int:
    """Typos tolerated for a word of this length (Elasticsearch's AUTO)."""
    if length < 4:
        return 0
    return 1 if length < 8 else 2


def _deletes(word: str, depth: int) -> set[str]:
    found: set[str] = set()
    frontier = {word}
    for _ in range(depth):
        frontier = {w[:i] + w[i + 1:] for w in frontier for i in range(len(w))}
        found |= frontier
    return found


def osa_distance(a: str, b: str, limit: int) -> int:
    """Edit distance counting a swap of neighbours as one edit; > limit exits early."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


class Vocabulary:
    """
    Every distinct search token in the catalog, indexed for prefix and typo lookup.

    The typo index holds each word's one-letter deletes only; the query side
    generates up to two, so a long word still tolerates two edits (most
    shapes of them) at a fraction of the memory. Run-together pairs
    ("poltel") stay out of it: they match exactly or by prefix, and fuzzing
    them would multiply the index for no one's benefit.
    """

    def __init__(self, terms: Iterable[str]):
        self.terms = frozenset(t for t in terms if t)
        self._sorted = sorted(self.terms)
        index: dict[str, list[str]] = {}
        for term in self.terms:
            if term.isdigit() or not max_edits(len(term)) or self._is_compound(term):
                continue
            for key in _deletes(term, 1) | {term}:
                index.setdefault(key, []).append(term)
        self._by_delete = index

    def _is_compound(self, term: str) -> bool:
        return any(
            term[:i] in self.terms and term[i:] in self.terms
            for i in range(2, len(term) - 1)
        )

    def expand(self, token: str) -> dict[str, float]:
        """Catalog words this query token should match, with match-quality weights."""
        # The token itself always, even if unseen: a product saved seconds
        # ago on another worker matches exactly before this copy refreshes.
        out = {token: W_EXACT}
        if token.isdigit():
            return out

        completions: list[str] = []
        for i in range(bisect.bisect_left(self._sorted, token), len(self._sorted)):
            term = self._sorted[i]
            if not term.startswith(token):
                break
            if term != token:
                completions.append(term)
        completions.sort(key=len)
        for term in completions[:MAX_PREFIX_EXPANSIONS]:
            out[term] = W_PREFIX_BASE + W_PREFIX_SPAN * len(token) / len(term)

        # Typo matching only for a word the catalog doesn't already know:
        # "miris" is a real word and must not also pull in "maris".
        edits = max_edits(len(token))
        if edits and token not in self.terms:
            candidates: set[str] = set()
            for key in _deletes(token, edits) | {token}:
                candidates.update(self._by_delete.get(key, ()))
            for term in candidates:
                allowed = min(edits, max_edits(len(term)))
                distance = osa_distance(token, term, allowed)
                if 0 < distance <= allowed:
                    out.setdefault(term, W_TYPO[distance])
        return out


@dataclass(frozen=True)
class SearchPlan:
    query: ParsedQuery
    expansions: dict[str, dict[str, float]]   # required token → {catalog word: weight}
    terms: list[str]                           # every word worth fetching candidates for


def make_plan(vocabulary: Vocabulary, raw_query: str) -> SearchPlan | None:
    parsed = parse_query(raw_query)
    if not parsed.required:
        return None
    expansions = {}
    for tok in parsed.required:
        words = vocabulary.expand(tok)
        stem = parsed.stems.get(tok)
        if stem:
            for word, weight in vocabulary.expand(stem).items():
                words[word] = max(words.get(word, 0.0), weight * W_STEM_FACTOR)
        expansions[tok] = words
    terms = sorted({term for words in expansions.values() for term in words})
    return SearchPlan(parsed, expansions, terms)


# Columns ``rank`` needs from each candidate row, in this order.
CANDIDATE_COLUMNS = (
    Product.product_id,
    Product.search_tokens,
    Product.search_name_tokens,
    Product.search_phrases,
    Product.is_featured,
    Product.view_count,
    Product.name,
)


def rank(rows: Sequence[Sequence], plan: SearchPlan) -> list[UUID]:
    """Order candidate rows (``CANDIDATE_COLUMNS``) best-first; drop weak partial matches."""
    required = plan.query.required
    soft = plan.query.soft
    name_phrase = NAME_PHRASE_MARK + plan.query.phrase
    scored: list[tuple[int, float, bool, int, int, UUID]] = []

    for product_id, tokens, name_tokens, phrases, is_featured, view_count, name in rows:
        tokens = set(tokens or ())
        name_tokens = set(name_tokens or ())
        coverage, score = 0, 0.0
        for tok in required:
            best = 0.0
            for term, weight in plan.expansions[tok].items():
                if term in tokens:
                    if term not in name_tokens:
                        weight *= KEYWORD_ONLY_FACTOR
                    best = max(best, weight)
            if best:
                coverage += 1
                score += best
        if not coverage:
            continue
        score += W_SOFT * sum(1 for tok in soft if tok in tokens)
        phrases = phrases or ()
        if name_phrase in phrases:
            score += W_NAME_PHRASE
        elif plan.query.phrase in phrases:
            score += W_KEYWORD_PHRASE
        scored.append((
            coverage, round(score, 4), bool(is_featured), view_count or 0,
            len(name or ""), product_id,
        ))

    if not scored:
        return []
    best_coverage = max(s[0] for s in scored)
    kept = [s for s in scored if s[0] == best_coverage]
    # Higher score, then featured, then popular, then the shorter (more
    # specific) name; the id only makes ties deterministic across pages.
    kept.sort(key=lambda s: (-s[1], not s[2], -s[3], s[4], str(s[5])))
    return [s[5] for s in kept]


# ---------------------------------------------------------------------------
# Per-worker vocabulary
# ---------------------------------------------------------------------------

_vocabulary = Vocabulary(())
_fingerprint: tuple | None = None
_checked_at = float("-inf")
_refresh_lock = asyncio.Lock()


def invalidate_vocabulary() -> None:
    """Make this worker re-check the catalog on its next search."""
    global _checked_at
    _checked_at = float("-inf")


async def get_vocabulary(db: AsyncSession) -> Vocabulary:
    global _vocabulary, _fingerprint, _checked_at
    if time.monotonic() - _checked_at < VOCAB_TTL_SECONDS:
        return _vocabulary
    async with _refresh_lock:
        if time.monotonic() - _checked_at < VOCAB_TTL_SECONDS:
            return _vocabulary
        fingerprint = tuple(
            (await db.execute(
                select(func.count(), func.max(Product.search_indexed_at))
            )).one()
        )
        if fingerprint != _fingerprint:
            words = (await db.execute(
                select(func.unnest(Product.search_tokens)).distinct()
            )).scalars().all()
            # Building the delete index is pure CPU; keep it off the event loop.
            _vocabulary = await asyncio.to_thread(Vocabulary, words)
            _fingerprint = fingerprint
            logger.info(f"Search vocabulary rebuilt: {len(_vocabulary.terms)} words")
        _checked_at = time.monotonic()
    return _vocabulary


async def plan_search(db: AsyncSession, raw_query: str) -> SearchPlan | None:
    return make_plan(await get_vocabulary(db), raw_query)


# ---------------------------------------------------------------------------
# Keeping products indexed
# ---------------------------------------------------------------------------

def _index_fields(name, name_si, name_ta, search_keywords) -> dict:
    tokens, name_tokens, phrases = index_tokens(name, name_si, name_ta, search_keywords)
    return {
        "search_tokens": tokens,
        "search_name_tokens": name_tokens,
        "search_phrases": phrases,
        "search_index_version": INDEX_VERSION,
    }


def _stored(target: Product, key: str):
    value = getattr(target, key)
    return list(value) if isinstance(value, (list, tuple)) else value


@event.listens_for(Product, "before_insert")
@event.listens_for(Product, "before_update")
def _keep_search_index_current(_mapper, _connection, target: Product) -> None:
    fields = _index_fields(
        target.name, target.name_si, target.name_ta, target.search_keywords
    )
    if all(_stored(target, key) == value for key, value in fields.items()):
        return
    for key, value in fields.items():
        setattr(target, key, value)
    target.search_indexed_at = datetime.now(timezone.utc)
    invalidate_vocabulary()


async def reindex_stale(session_maker: async_sessionmaker) -> int:
    """
    Tokenise every product whose index is missing or from an older version.

    Runs at startup. Only one worker does the work (advisory lock); the rest
    return immediately and pick the result up through the vocabulary refresh.
    Written with plain SQL so ``updated_at`` is not bumped on every product.
    """
    async with session_maker() as db:
        locked = (await db.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _REINDEX_LOCK_KEY}
        )).scalar()
        if not locked:
            return 0
        rows = (await db.execute(
            select(
                Product.product_id, Product.name, Product.name_si,
                Product.name_ta, Product.search_keywords,
            ).where(or_(
                Product.search_index_version.is_(None),
                Product.search_index_version != INDEX_VERSION,
            ))
        )).all()
        if not rows:
            return 0
        now = datetime.now(timezone.utc)
        params = [
            {"product_id": r.product_id, "indexed_at": now, **_index_fields(
                r.name, r.name_si, r.name_ta, r.search_keywords
            )}
            for r in rows
        ]
        statement = text(
            "UPDATE products SET search_tokens = :search_tokens, "
            "search_name_tokens = :search_name_tokens, "
            "search_phrases = :search_phrases, "
            "search_index_version = :search_index_version, "
            "search_indexed_at = :indexed_at "
            "WHERE product_id = :product_id"
        )
        for start in range(0, len(params), 500):
            await db.execute(statement, params[start:start + 500])
        await db.commit()
    invalidate_vocabulary()
    return len(rows)
