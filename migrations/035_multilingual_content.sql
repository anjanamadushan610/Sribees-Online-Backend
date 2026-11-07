-- Migration 035: multilingual product & category content
--
-- Customers pick a language (si/ta/en) in onboarding or Settings; catalog
-- content must follow that choice, not just the app chrome. Three languages
-- are a fixed product decision, so the translations live in flat columns on
-- the row rather than a join table — every product/category read is already
-- branch-filtered and paginated, and adding a per-language join to those
-- queries buys nothing at three locales.
--
-- The existing `name`/`description`/`short_description` columns stay as the
-- ENGLISH values and remain NOT NULL where they already were: they are the
-- fallback whenever a translation is missing, so a half-translated catalog
-- degrades to English instead of showing blanks.
--
-- Schema is created by SQLAlchemy Base.metadata.create_all() on fresh DBs; run
-- this against pre-existing databases. Safe to re-run.

-- ---------------------------------------------------------------------------
-- 1. Columns
-- ---------------------------------------------------------------------------

ALTER TABLE products
    ADD COLUMN IF NOT EXISTS name_si VARCHAR(255),
    ADD COLUMN IF NOT EXISTS name_ta VARCHAR(255),
    ADD COLUMN IF NOT EXISTS description_si TEXT,
    ADD COLUMN IF NOT EXISTS description_ta TEXT,
    ADD COLUMN IF NOT EXISTS short_description_si VARCHAR(500),
    ADD COLUMN IF NOT EXISTS short_description_ta VARCHAR(500);

ALTER TABLE categories
    ADD COLUMN IF NOT EXISTS name_si VARCHAR(100),
    ADD COLUMN IF NOT EXISTS name_ta VARCHAR(100),
    ADD COLUMN IF NOT EXISTS description_si TEXT,
    ADD COLUMN IF NOT EXISTS description_ta TEXT;

COMMENT ON COLUMN products.name_si IS
    'Sinhala product name. NULL falls back to products.name (English).';
COMMENT ON COLUMN products.name_ta IS
    'Tamil product name. NULL falls back to products.name (English).';
COMMENT ON COLUMN categories.name_si IS
    'Sinhala category name. NULL falls back to categories.name (English).';
COMMENT ON COLUMN categories.name_ta IS
    'Tamil category name. NULL falls back to categories.name (English).';

-- ---------------------------------------------------------------------------
-- Demo seed REMOVED (2026-11-07, owner's decision).
--
-- This file used to insert demo Sinhala/Tamil names for the demo catalogue (the columns above stay). Every deploy replays every migration,
-- so test branches and test admin accounts kept coming back on any database
-- not marked sribees.demo_seed = 'off'. Production was already marked; the
-- owner asked for the seed data to be gone for good, so the inserts are
-- deleted rather than switched off. The file stays (empty) so the migration
-- numbering and the deploy's replay list do not change.
--
-- Real branches, staff and catalogue are created from the admin dashboard.
-- ---------------------------------------------------------------------------
