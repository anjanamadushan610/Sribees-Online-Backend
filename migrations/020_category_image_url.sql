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

-- Only top-level categories (parent_category_id IS NULL) may carry an image.
UPDATE categories
   SET image_url = NULL
 WHERE parent_category_id IS NOT NULL
   AND image_url IS NOT NULL;

-- The home screen's query is "top-level, active, ordered by name".
CREATE INDEX IF NOT EXISTS idx_categories_top_level_active
    ON categories (is_active, name)
    WHERE parent_category_id IS NULL;
