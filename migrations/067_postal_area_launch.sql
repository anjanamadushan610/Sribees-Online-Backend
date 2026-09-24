-- =============================================================================
-- 067 - A postal area is switched on by itself, not by a branch mapping
-- =============================================================================
--
-- Service Areas (066) made "active" the branch mapping's flag, so switching a
-- postal area on meant choosing its branch there too. The owner wants the two
-- jobs apart:
--
--   Settings > Service Areas   switches a postal area on (is_launched)
--   Branches > coverage        offers only switched-on postal areas, and is
--                              where each one is given its branch
--
-- A customer is served where all of it holds: province and district
-- unlocked, postal area launched, and an active branch mapping for it
-- (location_rollout_service.served_mapping_clause).
--
-- REPLAYED ON EVERY DEPLOY. The backfill runs only in the deploy that adds the
-- column, so a postal area an admin later switches off is never switched back
-- on by a replay. It launches exactly what customers could pick before this
-- migration (every active mapping), adding a directory row first for any
-- mapped name the directory did not have, so nothing live goes dark.
-- =============================================================================

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'post_office_directory' AND column_name = 'is_launched'
    ) THEN
        ALTER TABLE post_office_directory
            ADD COLUMN is_launched BOOLEAN NOT NULL DEFAULT FALSE;

        INSERT INTO post_office_directory (id, post_office, district, province, is_active)
        SELECT gen_random_uuid(), m.post_office, m.district,
               COALESCE(NULLIF(m.province, ''), d.province, ''), TRUE
          FROM post_office_branch_mapping m
          LEFT JOIN location_districts d ON lower(d.name) = lower(m.district)
         WHERE m.district IS NOT NULL
           AND NOT EXISTS (
                 SELECT 1 FROM post_office_directory x
                  WHERE lower(x.post_office) = lower(m.post_office))
        ON CONFLICT (post_office) DO NOTHING;

        UPDATE post_office_directory x
           SET is_launched = TRUE
         WHERE EXISTS (
                 SELECT 1 FROM post_office_branch_mapping m
                  WHERE lower(m.post_office) = lower(x.post_office)
                    AND m.is_active);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_po_directory_launched
    ON post_office_directory (is_launched);

COMMENT ON COLUMN post_office_directory.is_launched IS
    'Switched on in Service Areas: offered to the branch form, and to customers once a branch serves it.';

COMMIT;
