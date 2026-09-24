-- =============================================================================
-- 063_search_misses.sql — Searches that found nothing, for the admin inbox.
--
-- A shopper who searches "kottu mee" and gets an empty screen is telling us a
-- word the catalog doesn't know. Each such search lands here, grouped by its
-- folded words (so "Seeni" and "සීනි" are one row), and an admin attaches it
-- to the right product as a search keyword with one click — no AI involved.
--
-- No shopper identity is stored. `shoppers` counts distinct shoppers per day;
-- the de-duplication happens in Redis before the row is touched.
--
-- create_all() also creates this table from app/models/search_miss.py; this
-- file makes the schema explicit and is what the deploy replays.
-- Idempotent: safe to re-run on every deploy.
-- =============================================================================

CREATE TABLE IF NOT EXISTS search_misses (
    query_key           VARCHAR(200) PRIMARY KEY,
    sample_query        TEXT         NOT NULL,
    shoppers            INTEGER      NOT NULL DEFAULT 1,
    first_seen          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_seen           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_branch_id      UUID         REFERENCES branches (branch_id) ON DELETE SET NULL,
    status              VARCHAR(16)  NOT NULL DEFAULT 'open',
    resolved_product_id UUID         REFERENCES products (product_id) ON DELETE SET NULL,
    resolved_by         UUID         REFERENCES admins (admin_id) ON DELETE SET NULL,
    resolved_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_search_misses_inbox
    ON search_misses (status, last_seen);

COMMENT ON TABLE search_misses IS
    'Zero-result customer searches, grouped by folded query words. No shopper identity.';
