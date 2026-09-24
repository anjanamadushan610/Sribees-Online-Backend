-- =============================================================================
-- 062_catalog_search_index.sql — Token index for the customer search bar.
--
-- 029 matched with similarity(whole keyword list, query) > 0.25. Similarity
-- against a list of seven terms is diluted by the other six: measured on dev,
-- "kesel" scored 0.12 against a product whose keywords literally contain
-- "kesel", so the fuzzy half never fired, and the function form could not use
-- the trigram indexes either (every search was a sequential scan).
--
-- The replacement tokenises each product once, on write, into a text[] of
-- folded tokens (Sinhala and Tamil script transliterated, Singlish spellings
-- folded — see app/services/search_text.py). A search is then one GIN
-- array-overlap lookup; typo tolerance happens in application memory against
-- the vocabulary of those tokens (app/services/catalog_search_service.py).
--
-- The columns are filled by the application: on every ORM write, and at start
-- up for any row whose search_index_version is missing or old. This migration
-- only creates the storage.
--
-- Idempotent: safe to re-run on every deploy.
-- =============================================================================

ALTER TABLE products
    ADD COLUMN IF NOT EXISTS search_tokens        TEXT[],
    ADD COLUMN IF NOT EXISTS search_name_tokens   TEXT[],
    ADD COLUMN IF NOT EXISTS search_phrases       TEXT[],
    ADD COLUMN IF NOT EXISTS search_index_version SMALLINT,
    ADD COLUMN IF NOT EXISTS search_indexed_at    TIMESTAMPTZ;

COMMENT ON COLUMN products.search_tokens IS
    'Derived search index (name + translations + search_keywords, folded). Written by the application only.';

-- The candidate lookup: search_tokens && ARRAY[...].
CREATE INDEX IF NOT EXISTS idx_products_search_tokens
    ON products USING gin (search_tokens);

-- Each worker checks max(search_indexed_at) to know when to rebuild its
-- in-memory vocabulary; this keeps that an index lookup.
CREATE INDEX IF NOT EXISTS idx_products_search_indexed_at
    ON products (search_indexed_at);

-- Nothing queries search_keywords by trigram any more. The name index stays:
-- the admin product list still filters with name ILIKE.
DROP INDEX IF EXISTS idx_products_keywords_trgm;

-- -----------------------------------------------------------------------------
-- A write that changes the searchable text but not the index (a raw-SQL seed,
-- a manual fix in psql) marks the row stale, so the next start re-tokenises
-- it. ORM writes recompute the index in the same UPDATE and are left alone.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION products_mark_search_index_stale() RETURNS trigger AS $$
BEGIN
    IF NEW.search_tokens IS NOT DISTINCT FROM OLD.search_tokens
       AND (NEW.name            IS DISTINCT FROM OLD.name
         OR NEW.name_si         IS DISTINCT FROM OLD.name_si
         OR NEW.name_ta         IS DISTINCT FROM OLD.name_ta
         OR NEW.search_keywords IS DISTINCT FROM OLD.search_keywords)
    THEN
        NEW.search_index_version := NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_products_search_index_stale ON products;
CREATE TRIGGER trg_products_search_index_stale
    BEFORE UPDATE ON products
    FOR EACH ROW EXECUTE FUNCTION products_mark_search_index_stale();
