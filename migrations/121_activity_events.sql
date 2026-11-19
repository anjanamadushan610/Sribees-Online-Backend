-- =============================================================================
-- Migration 121: customer activity log (app + web store).
--
-- Owner, 2026-11-19: record what each shopper does from the first app open,
-- guest or signed in -- screens, products opened, image swipes, searches,
-- cart changes, scroll depth -- and keep it forever. Super Admin reads it as
-- one timeline per customer (docs/plans/customer-activity-log.md).
--
-- device_id: the install id the heartbeat uses ("guest_..." app, "web_..."
--   browser). Not a foreign key: events may arrive before the first heartbeat.
-- user_id: from the token at send time; NULL = guest. Guest events show on a
--   customer's timeline through the devices that customer signed in on.
-- props: small per-event details, checked by the API (no free-form blobs).
-- occurred_at: the phone's time, clamped by the server; received_at: ours.
--
-- No age-based delete (owner). Rows of a closed account are deleted with it.
-- Idempotent -- the deploy applies this file again on each release.
-- =============================================================================

CREATE TABLE IF NOT EXISTS activity_events (
    id           BIGSERIAL PRIMARY KEY,
    device_id    VARCHAR(80)  NOT NULL,
    user_id      UUID         NULL REFERENCES users(user_id) ON DELETE CASCADE,
    platform     VARCHAR(10)  NOT NULL,
    app_version  VARCHAR(40)  NULL,
    session_id   VARCHAR(40)  NULL,
    event        VARCHAR(30)  NOT NULL,
    screen       VARCHAR(40)  NULL,
    product_id   UUID         NULL,
    category_id  UUID         NULL,
    props        JSONB        NOT NULL DEFAULT '{}'::jsonb,
    occurred_at  TIMESTAMPTZ  NOT NULL,
    received_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT ck_activity_events_platform CHECK (platform IN ('android', 'ios', 'web'))
);

CREATE INDEX IF NOT EXISTS ix_activity_events_user_time
    ON activity_events (user_id, occurred_at DESC) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_activity_events_device_time
    ON activity_events (device_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_activity_events_product
    ON activity_events (product_id, occurred_at DESC) WHERE product_id IS NOT NULL;
-- Cheap time-range scans over a table that only grows.
CREATE INDEX IF NOT EXISTS brin_activity_events_received
    ON activity_events USING BRIN (received_at);
