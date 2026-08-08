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

-- -----------------------------------------------------------------------------
-- 4. Seed keywords for the demo catalog (021_seed_catalog.sql's fixed ids).
--
-- Best-effort first pass on common Sri Lankan grocery terms; admins can add
-- to these (or correct them) per product via the admin panel going forward
-- as the real catalog grows past this demo set.
--
-- WHERE search_keywords IS NULL so this never overwrites anything an admin
-- has already edited by hand.
-- -----------------------------------------------------------------------------
-- Keywords cover four scripts/spellings per product where a common term
-- exists: Sinhala, Singlish (romanized Sinhala), Tamil, Tamilish (romanized
-- Tamil) — e.g. Milk: කිරි / kiri / பால் / paal.
UPDATE products SET search_keywords = v.keywords
FROM (VALUES
    -- Fresh Produce
    ('40000000-0000-4000-a000-000000000101'::uuid, 'kesel, ambul kesel, කෙසෙල්, අඹුල් කෙසෙල්, வாழைப்பழம், vazhaipazham, banana'),
    ('40000000-0000-4000-a000-000000000102'::uuid, 'thakkali, තක්කාලි, தக்காளி'),
    ('40000000-0000-4000-a000-000000000103'::uuid, 'carrot, kaarot, karat, කැරට්, கேரட்'),
    ('40000000-0000-4000-a000-000000000104'::uuid, 'miris, kola miris, අමු මිරිස්, කොළ මිරිස්, பச்சை மிளகாய், milagai'),
    ('40000000-0000-4000-a000-000000000105'::uuid, 'thambili, තැඹිලි, இளநீர், elaneer, king coconut'),
    -- Dairy & Eggs
    ('40000000-0000-4000-a000-000000000201'::uuid, 'kiri, kiri milk, කිරි, பால், paal, milk'),
    ('40000000-0000-4000-a000-000000000202'::uuid, 'kiri dosi, mee kiri, මී කිරි, தயிர், thayir, curd'),
    ('40000000-0000-4000-a000-000000000203'::uuid, 'biththara, බිත්තර, முட்டை, muttai, eggs'),
    ('40000000-0000-4000-a000-000000000204'::uuid, 'yogurt, yoghurt, යෝගට්, தயிர், thayir'),
    ('40000000-0000-4000-a000-000000000205'::uuid, 'batter, batar, බටර්, வெண்ணெய், vennai, butter'),
    -- Pantry Staples
    ('40000000-0000-4000-a000-000000000301'::uuid, 'haal, rathu haal, හාල්, රතු හාල්, அரிசி, arisi, rice'),
    ('40000000-0000-4000-a000-000000000302'::uuid, 'seeni, සීනි, சர்க்கரை, sarkkarai, sugar'),
    ('40000000-0000-4000-a000-000000000303'::uuid, 'parippu, dhal, mysoor dhal, පරිප්පු, பருப்பு, paruppu, lentils'),
    ('40000000-0000-4000-a000-000000000304'::uuid, 'pol thel, පොල් තෙල්, தேங்காய் எண்ணெய், thengai ennai, coconut oil'),
    ('40000000-0000-4000-a000-000000000305'::uuid, 'kari kudu, curry kudu, කරි කුඩු, மசாலா தூள், masala podi, curry powder')
) AS v(product_id, keywords)
WHERE products.product_id = v.product_id
  AND products.search_keywords IS NULL;
