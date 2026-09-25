-- =============================================================================
-- Migration 071: notifications speak the customer's language, customers choose
-- what they hear about, and marketing can send campaigns.
--
-- 1. users.preferred_language -- the language the app was last used in
--    ('en' | 'si' | 'ta'). In-app notification rows are written in it.
-- 2. push_tokens.language -- the language of the install behind each token.
--    A push is rendered per device, so a family sharing one account on a
--    Sinhala phone and an English tablet each read their own.
-- 3. users.notify_offers / notify_promotions / notify_general -- the three
--    marketing switches in the app. Default ON. Order, payment, refund and
--    wallet updates are not marketing and ignore them.
-- 4. notification_campaigns -- a push a marketing manager composes once in
--    three languages and the server delivers in the background.
--
-- Idempotent -- the deploy applies this file again on each release.
-- =============================================================================

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS preferred_language VARCHAR(5);
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS notify_offers BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS notify_promotions BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS notify_general BOOLEAN NOT NULL DEFAULT TRUE;

ALTER TABLE push_tokens
    ADD COLUMN IF NOT EXISTS language VARCHAR(5);

CREATE TABLE IF NOT EXISTS notification_campaigns (
    campaign_id      UUID PRIMARY KEY,
    category         VARCHAR(20)  NOT NULL,
    title            JSONB        NOT NULL,
    message          JSONB        NOT NULL,
    image_url        VARCHAR(500),
    link_type        VARCHAR(20),
    link_value       VARCHAR(100),
    -- NULL = every customer. Only a Super Admin may send one of those.
    branch_id        UUID REFERENCES branches(branch_id) ON DELETE SET NULL,
    status           VARCHAR(20)  NOT NULL DEFAULT 'scheduled',
    scheduled_at     TIMESTAMPTZ  NOT NULL,
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    cancelled_at     TIMESTAMPTZ,
    recipients       INTEGER      NOT NULL DEFAULT 0,
    devices          INTEGER      NOT NULL DEFAULT 0,
    delivered        INTEGER      NOT NULL DEFAULT 0,
    failed           INTEGER      NOT NULL DEFAULT 0,
    opened           INTEGER      NOT NULL DEFAULT 0,
    error            TEXT,
    created_by       UUID REFERENCES admins(admin_id) ON DELETE SET NULL,
    created_by_name  VARCHAR(255),
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_notification_campaigns_category
        CHECK (category IN ('offers', 'promotions', 'general')),
    CONSTRAINT ck_notification_campaigns_status
        CHECK (status IN ('scheduled', 'sending', 'sent', 'failed', 'cancelled')),
    CONSTRAINT ck_notification_campaigns_link
        CHECK (link_type IS NULL OR link_type IN ('product', 'offers'))
);

-- The worker's "what is due" query.
CREATE INDEX IF NOT EXISTS ix_notification_campaigns_due
    ON notification_campaigns (status, scheduled_at);
CREATE INDEX IF NOT EXISTS ix_notification_campaigns_branch
    ON notification_campaigns (branch_id, created_at DESC);

-- "Did this customer already open campaign X" -- the open counter must count
-- a person once, however many times they tap.
CREATE INDEX IF NOT EXISTS ix_notifications_campaign
    ON notifications ((data ->> 'campaign_id'))
    WHERE data ? 'campaign_id';
