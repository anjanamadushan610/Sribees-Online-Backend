-- =============================================================================
-- Migration 073: who has the app open, on which version, and for how long.
--
-- The owner asked for a Super Admin page showing the customers using the app
-- right now, the version each one runs, and each install's daily usage from
-- the day it was downloaded.
--
-- app_installs: one row per app install, keyed by the install's own id (the
--   mobile app's persistent device_id). installed_at is the phone's own record
--   of when the app was first installed (Android firstInstallTime), so the
--   download date is real even for installs older than this table.
--   user_id is whoever was signed in at the last heartbeat (NULL = guest).
--
-- app_usage_days: seconds in the foreground and number of opens per install
--   per day, the day being Asia/Colombo's. Usage before this release was
--   never measured and is not invented: history starts at the install's
--   first heartbeat.
--
-- Idempotent -- the deploy applies this file again on each release.
-- =============================================================================

CREATE TABLE IF NOT EXISTS app_installs (
    device_id        VARCHAR(80) PRIMARY KEY,
    user_id          UUID REFERENCES users(user_id) ON DELETE SET NULL,
    platform         VARCHAR(10) NOT NULL DEFAULT 'android',
    app_version      VARCHAR(40),
    build_number     VARCHAR(20),
    installed_at     TIMESTAMPTZ,
    first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_foreground    BOOLEAN NOT NULL DEFAULT FALSE,
    CONSTRAINT ck_app_installs_platform CHECK (platform IN ('android', 'ios'))
);

CREATE INDEX IF NOT EXISTS ix_app_installs_last_seen ON app_installs (last_seen_at DESC);
CREATE INDEX IF NOT EXISTS ix_app_installs_user ON app_installs (user_id);

CREATE TABLE IF NOT EXISTS app_usage_days (
    device_id           VARCHAR(80) NOT NULL REFERENCES app_installs(device_id) ON DELETE CASCADE,
    usage_date          DATE NOT NULL,
    user_id             UUID REFERENCES users(user_id) ON DELETE SET NULL,
    foreground_seconds  INTEGER NOT NULL DEFAULT 0,
    opens               INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (device_id, usage_date),
    CONSTRAINT ck_app_usage_days_nonneg CHECK (foreground_seconds >= 0 AND opens >= 0)
);

CREATE INDEX IF NOT EXISTS ix_app_usage_days_date ON app_usage_days (usage_date);
