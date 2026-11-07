-- =============================================================================
-- 029_search_fuzzy_multilingual.sql — Typo-tolerant, multilingual product search.
--
-- The home-screen search bar (GET /products/search) matched only an exact
-- substring of `name`/`description`. A single misspelled letter, or a query
-- typed in Sinhala/Tamil script against a catalog stored in English/Singlish
-- names, returned nothing.
--
-- pg_trgm gives self-hosted, no-AI-call fuzzy matching (trigram similarity),
-- and `search_keywords` is where a product's Sinhala/Tamil/Singlish alternate
-- names live so a script mismatch isn't fatal either. Both are plugged into
-- ProductService.get_all_for_branch's search clause, not a separate search
-- system — see that function for the query side of this.
--
-- Idempotent: safe to re-run on every deploy.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Trigram similarity — the fuzzy-match engine, no external service needed.
-- -----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- -----------------------------------------------------------------------------
-- 2. Free-text alternate names per product: Sinhala script, Tamil script,
--    Singlish spellings, common misspellings. A flat column rather than a
--    translations table — this catalog has no per-language structured need,
--    just "other ways a shopper might type this."
-- -----------------------------------------------------------------------------
ALTER TABLE products
    ADD COLUMN IF NOT EXISTS search_keywords TEXT;

COMMENT ON COLUMN products.search_keywords IS
    'Alternate names for search only (Sinhala/Tamil/Singlish/common misspellings). Never shown to customers as product content.';

-- -----------------------------------------------------------------------------
-- 3. GIN trigram indexes — keeps fuzzy matching fast as the catalog grows
--    past the current demo set.
-- -----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_products_name_trgm
    ON products USING gin (name gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_products_keywords_trgm
    ON products USING gin (search_keywords gin_trgm_ops)
    WHERE search_keywords IS NOT NULL;

-- Data write REMOVED 2026-11-07 (owner's decision): every deploy replays every
-- migration, so this keyword seed (for demo products that no longer exist) ran again on each release and could change live
-- data. Schema statements in this file are unchanged.
