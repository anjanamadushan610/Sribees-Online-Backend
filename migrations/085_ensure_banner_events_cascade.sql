-- =============================================================================
-- 085_ensure_banner_events_cascade.sql
-- Ensure banner_events foreign key constraint has ON DELETE CASCADE.
-- Idempotent: safe to re-run on every deploy.
-- =============================================================================

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.table_constraints
        WHERE constraint_name = 'banner_events_banner_id_fkey'
          AND table_name = 'banner_events'
    ) THEN
        ALTER TABLE banner_events DROP CONSTRAINT banner_events_banner_id_fkey;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM information_schema.tables
        WHERE table_name = 'banner_events'
    ) THEN
        ALTER TABLE banner_events
            ADD CONSTRAINT banner_events_banner_id_fkey
            FOREIGN KEY (banner_id) REFERENCES banners(banner_id) ON DELETE CASCADE;
    END IF;
END $$;
