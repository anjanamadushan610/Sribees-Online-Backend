-- =============================================================================
-- 052_postal_city_delivery_lifecycle.sql
--
-- SribeesExpress prices every delivery between two postal cities and holds a
-- confirmed price for the fulfilment window; SRIBEESonline stops pricing
-- delivery itself and books a parcel when the branch marks it ready for pickup.
-- See docs/SRIBEESEXPRESS_ECOMMERCE_API.md (2026-09-13 changes) and
-- docs/DELIVERY_LIFECYCLE.md.
--
--   1. Postal city ids. SribeesExpress retired its city ids on 2026-09-13 and
--      keys quotes on a postal city id picked from GET /ecommerce/postal-cities.
--      post_office_directory and addresses get a column for it. The old
--      courier_city_id / courier_city_name / courier_resolve_reason /
--      courier_resolved_at columns (050) are left in place, unread: 050 is
--      replayed on every deploy and would only add them back.
--
--   2. Branch outlets and webhook secrets. A branch booking under the shared
--      account key is registered as an outlet of that account
--      (courier_outlet_id); the pickup-location ids 050 stored are gone with the
--      pickup-location API and are not migrated. Each SribeesExpress account
--      (the account key, or a branch with its own) has its own webhook signing
--      secret, stored Fernet-encrypted like the keys.
--
--   3. The agreed delivery price on the order, and when it was marked ready
--      for pickup.
--
--   4. No flat delivery fee. The admin-settable fee and its seeded default are
--      deleted: the delivery charge is the courier's quote, and a leftover row
--      is a price someone could believe is still applied. 026 re-seeds
--      `delivery_fee` / `free_delivery_threshold` on every replay (ON CONFLICT
--      DO NOTHING against a deleted row inserts it again), so this deletes them
--      again each time; nothing reads either key.
--
-- NAMES. The domain calls a post office a *postal city* from this release on,
-- but the physical tables and columns keep their post_office names: renaming
-- them would break the replay of 018/026/050 on the next deploy. See
-- app/models/branch.py's module docstring.
--
-- Backward compatible with the running release: only nullable columns are
-- added and only settings rows nothing requires are deleted.
--
-- Idempotent: safe to re-run on every deploy.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Postal city ids
-- ---------------------------------------------------------------------------
ALTER TABLE post_office_directory
    ADD COLUMN IF NOT EXISTS courier_postal_city_id INTEGER;

COMMENT ON COLUMN post_office_directory.courier_postal_city_id IS
    'SribeesExpress postal_city_id from the last GET /ecommerce/postal-cities sync (matched by name and district). Quotes are keyed on it. NULL = SribeesExpress does not list this postal city.';

ALTER TABLE addresses
    ADD COLUMN IF NOT EXISTS courier_postal_city_id INTEGER;

COMMENT ON COLUMN addresses.courier_postal_city_id IS
    'SribeesExpress postal_city_id for this address, matched from post_office_directory when the address is saved. NULL = delivery cannot be priced here.';

-- The retired scheme's ids must not be mistaken for postal city ids by anyone
-- reading the table by hand.
COMMENT ON COLUMN addresses.courier_city_id IS
    'RETIRED (052): a SribeesExpress city id from the pre-2026-09-13 contract. Not read. See courier_postal_city_id.';
COMMENT ON COLUMN post_office_directory.courier_city_id IS
    'RETIRED (052): a SribeesExpress city id from the pre-2026-09-13 contract. Not read. See courier_postal_city_id.';

-- ---------------------------------------------------------------------------
-- 2. Branch outlets and webhook secrets
-- ---------------------------------------------------------------------------
ALTER TABLE branches
    ADD COLUMN IF NOT EXISTS courier_outlet_id            INTEGER,
    ADD COLUMN IF NOT EXISTS courier_outlet_synced_at     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS courier_webhook_secret_test  TEXT,
    ADD COLUMN IF NOT EXISTS courier_webhook_secret_live  TEXT;

COMMENT ON COLUMN branches.courier_outlet_id IS
    'SribeesExpress outlet id for this branch under the ACCOUNT key (POST /ecommerce/outlets). Only a branch without its own key needs one. NULL = not registered.';
COMMENT ON COLUMN branches.courier_webhook_secret_test IS
    'Webhook signing secret issued to this branch''s own SANDBOX account, Fernet-encrypted (enc:v1:...). Never returned by the API.';
COMMENT ON COLUMN branches.courier_webhook_secret_live IS
    'Webhook signing secret issued to this branch''s own LIVE account, Fernet-encrypted (enc:v1:...). Never returned by the API.';
COMMENT ON COLUMN branches.courier_pickup_location_id IS
    'RETIRED (052): a SribeesExpress pickup-location id; that API became outlets. Not read. See courier_outlet_id.';

-- ---------------------------------------------------------------------------
-- 3. Orders: the agreed delivery price, and ready-for-pickup
-- ---------------------------------------------------------------------------
ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS courier_quote_amount        NUMERIC(10, 2),
    ADD COLUMN IF NOT EXISTS courier_quote_confirmed_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS ready_for_pickup_at         TIMESTAMPTZ;

COMMENT ON COLUMN orders.courier_quote_amount IS
    'Delivery charge SribeesExpress quoted and held for this order. Equals shipping_amount at checkout; differs only if booking had to re-quote after the hold lapsed.';
COMMENT ON COLUMN orders.courier_quote_confirmed_at IS
    'When the checkout quote was confirmed (POST /ecommerce/rates/{id}/confirm), holding its price for the fulfilment window.';
COMMENT ON COLUMN orders.ready_for_pickup_at IS
    'When the branch marked the parcel packed and ready for the courier -- the moment it was booked with SribeesExpress.';

-- ---------------------------------------------------------------------------
-- 4. No flat delivery fee
-- ---------------------------------------------------------------------------
DELETE FROM app_settings
WHERE key IN ('flat_delivery_fee', 'delivery_fee', 'free_delivery_threshold');
