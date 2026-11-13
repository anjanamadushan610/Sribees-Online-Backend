-- =============================================================================
-- Migration 117: the web store (www.sribeesonline.com) on the App Users page.
--
-- The owner asked to see who uses the website, not only the phone app. The
-- web store now sends the same heartbeat as the app with platform 'web'
-- while its tab is on screen, so app_installs takes a third platform.
--
-- browser: "Chrome on Windows" and the like, read by the server from the
--   User-Agent of a web heartbeat (a browser has no app version to show).
--   NULL for phone installs.
--
-- Idempotent -- the deploy applies this file again on each release.
-- =============================================================================

ALTER TABLE app_installs DROP CONSTRAINT IF EXISTS ck_app_installs_platform;
ALTER TABLE app_installs
    ADD CONSTRAINT ck_app_installs_platform CHECK (platform IN ('android', 'ios', 'web'));

ALTER TABLE app_installs ADD COLUMN IF NOT EXISTS browser VARCHAR(60);

-- The page filters by App / Web and sorts by last seen.
CREATE INDEX IF NOT EXISTS ix_app_installs_platform_seen ON app_installs (platform, last_seen_at DESC);
