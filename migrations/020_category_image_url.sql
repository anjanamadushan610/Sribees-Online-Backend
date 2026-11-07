-- Migration 020: Category image
--
-- `categories.image_url` exists on the SQLAlchemy model but has never had a
-- migration. Base.metadata.create_all() creates missing TABLES but never ALTERs
-- an existing one, so any database whose `categories` table predates the column
-- is missing it — and the mobile home screen is about to select it.
--
-- Images are a TOP-LEVEL category concept: the home screen renders the category
-- tiles from them, and sub-categories are shown as text under their parent. The
-- API rejects an image on a sub-category; this backfill makes the existing data
-- agree with that rule rather than leaving orphaned images no screen displays.
--
-- Safe to re-run: IF NOT EXISTS + an idempotent UPDATE.

ALTER TABLE categories
    ADD COLUMN IF NOT EXISTS image_url VARCHAR(500);

-- Data write REMOVED 2026-11-07 (owner's decision): every deploy replays every
-- migration, so this UPDATE (it cleared every sub-category image) ran again on each release and could change live
-- data. Schema statements in this file are unchanged.

-- The home screen's query is "top-level, active, ordered by name".
CREATE INDEX IF NOT EXISTS idx_categories_top_level_active
    ON categories (is_active, name)
    WHERE parent_category_id IS NULL;
