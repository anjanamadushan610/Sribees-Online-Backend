-- ============================================================================
-- Migration 018: Post Office master directory ("Delivery Zones")
--
-- Source-of-truth catalog of Sri Lankan Post Offices, each tagged with its
-- District and Province. Managed via Settings -> Delivery Zones; the Branch
-- form reads from it to offer coverage-area options.
--
-- Idempotent: safe to re-run on every deploy (IF NOT EXISTS + ON CONFLICT).
-- The table is also created by SQLAlchemy create_all() on startup, so the
-- CREATE TABLE is defensive; the indexes + seed are the meaningful additions.
-- ============================================================================

BEGIN;

-- 1. Master directory table
CREATE TABLE IF NOT EXISTS post_office_directory (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    post_office   VARCHAR(100) NOT NULL UNIQUE,
    district      VARCHAR(100) NOT NULL,
    province      VARCHAR(100) NOT NULL,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE  post_office_directory              IS 'Master list of Post Offices (Delivery Zones), independent of branch coverage';
COMMENT ON COLUMN post_office_directory.post_office  IS 'Post office name — unique across Sri Lanka';

-- 2. Lookup indexes for the cascading Province -> District -> Post Office UI
CREATE INDEX IF NOT EXISTS idx_po_directory_province
    ON post_office_directory (province);
CREATE INDEX IF NOT EXISTS idx_po_directory_district
    ON post_office_directory (district);
CREATE INDEX IF NOT EXISTS idx_po_directory_province_district
    ON post_office_directory (province, district);

-- 3. Auto-update updated_at trigger
CREATE OR REPLACE FUNCTION update_po_directory_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_po_directory_updated_at ON post_office_directory;
CREATE TRIGGER trg_po_directory_updated_at
    BEFORE UPDATE ON post_office_directory
    FOR EACH ROW EXECUTE FUNCTION update_po_directory_updated_at();

-- 4. Seed the Western/Kalutara zone the mobile app currently ships as a
--    placeholder, so the dropdowns have data out of the box. setting the id
--    explicitly because the app model's PK default is Python-side (uuid4).
INSERT INTO post_office_directory (id, post_office, district, province, is_active)
VALUES
    (gen_random_uuid(), 'Welipenna',     'Kalutara', 'Western', TRUE),
    (gen_random_uuid(), 'Matugama',      'Kalutara', 'Western', TRUE),
    (gen_random_uuid(), 'Meegahatenna',  'Kalutara', 'Western', TRUE)
ON CONFLICT (post_office) DO NOTHING;

COMMIT;
