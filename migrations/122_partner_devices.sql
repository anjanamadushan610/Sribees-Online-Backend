-- =============================================================================
-- Migration 122: phones signed in to the SRIBEES Partner app.
--
-- Owner, 2026-11-20: the Partner app tells a partner, by push, when
--   * a new customer joins with their invite code,
--   * referral earnings become claimable (return window closed),
--   * a withdrawal is paid or rejected.
-- Customer pushes use user_devices; partners are a separate identity store
-- (partners table), so their phones get their own table.
--
-- token: the FCM registration token. UNIQUE -- one phone belongs to whoever
--   signed in on it last (re-registering moves the row to that partner).
-- locale: en / si / ta, the language the app was in at registration, so the
--   push is written in the partner's language.
-- is_active: false after sign-out or when FCM says the token is dead.
--
-- Idempotent -- the deploy applies this file again on each release.
-- =============================================================================

CREATE TABLE IF NOT EXISTS partner_devices (
    device_id     UUID         PRIMARY KEY,
    partner_id    UUID         NOT NULL REFERENCES partners(partner_id) ON DELETE CASCADE,
    token         VARCHAR(512) NOT NULL,
    platform      VARCHAR(10)  NOT NULL DEFAULT 'android',
    locale        VARCHAR(5)   NOT NULL DEFAULT 'en',
    app_version   VARCHAR(40)  NULL,
    is_active     BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_partner_devices_token UNIQUE (token),
    CONSTRAINT ck_partner_devices_platform CHECK (platform IN ('android', 'ios')),
    CONSTRAINT ck_partner_devices_locale CHECK (locale IN ('en', 'si', 'ta'))
);

CREATE INDEX IF NOT EXISTS ix_partner_devices_partner_active
    ON partner_devices (partner_id) WHERE is_active;
