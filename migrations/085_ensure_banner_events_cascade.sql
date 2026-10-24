-- =============================================================================
-- 085_ensure_banner_events_cascade.sql
-- Ensure banner_events foreign key constraint has ON DELETE CASCADE.
-- Idempotent: safe to re-run on every deploy.
-- =============================================================================

DO $$
DECLARE
    r RECORD;
BEGIN
    -- Drop any existing foreign key constraint on banner_events.banner_id
    FOR r IN (
        SELECT tc.constraint_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_name = 'banner_events'
          AND kcu.column_name = 'banner_id'
    ) LOOP
        EXECUTE 'ALTER TABLE banner_events DROP CONSTRAINT ' || quote_ident(r.constraint_name);
    END LOOP;

    -- Add the foreign key constraint with ON DELETE CASCADE explicitly
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
