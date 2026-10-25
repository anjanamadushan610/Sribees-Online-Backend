-- Meaning-based product lookup for the shopping assistant (SEMANTIC_SEARCH_ENABLED).
--
-- A plain REAL[] rather than pgvector: production Postgres has no vector
-- extension, and with under a few thousand products each API worker keeps the
-- vectors in memory and compares them there (app/services/semantic_search.py).
-- search_embedding_key is a hash of the embedded text + model + size, so a
-- renamed product, a new keyword or a model change re-embeds only that row.
-- Both columns are NULL until the embedding worker fills them; nothing else
-- reads them. Safe to run any number of times.

ALTER TABLE products ADD COLUMN IF NOT EXISTS search_embedding REAL[];
ALTER TABLE products ADD COLUMN IF NOT EXISTS search_embedding_key VARCHAR(64);

COMMENT ON COLUMN products.search_embedding IS
    'Normalised Gemini embedding of the product names, category and search keywords. Filled by the embedding worker; never set by hand.';
COMMENT ON COLUMN products.search_embedding_key IS
    'sha256 of the embedded text, model and dimensions; differs from the current value when the row must be re-embedded.';
