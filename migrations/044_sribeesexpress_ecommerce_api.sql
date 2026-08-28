-- =============================================================================
-- 044_sribeesexpress_ecommerce_api.sql
--
-- Migrate the courier integration from the retired /v1/* contract to
-- SribeesExpress's /api/v1/ecommerce/* contract
-- (docs/SRIBEESEXPRESS_ECOMMERCE_API.md).
--
-- Three shape changes drive everything here:
--   1. Quoting is by `city_id`, not by a structured destination address. The
--      city_id comes from POST /ecommerce/locations/resolve and is stable per
--      address, so it is cached on `addresses` and refreshed only when the
--      address changes (contract section 5a: "cache the result").
--   2. The pickup point is a registered `pickup_location_id` on
--      SribeesExpress's side, one per fulfilling branch (contract section 12).
--   3. Delivery outcome now arrives as structured `handover` / `cod` /
--      `failure_note` blocks on the delivered/failed/returned webhook, which
--      is the only proof-of-delivery we ever receive. Persist all of it.
--
-- Idempotent: safe to re-run on every deploy.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. addresses: cached city_id resolution (contract section 5a)
-- ---------------------------------------------------------------------------
ALTER TABLE addresses
    ADD COLUMN IF NOT EXISTS courier_city_id           INTEGER,
    ADD COLUMN IF NOT EXISTS courier_city_name         VARCHAR(120),
    ADD COLUMN IF NOT EXISTS courier_resolve_reason    VARCHAR(40),
    ADD COLUMN IF NOT EXISTS courier_resolved_at       TIMESTAMPTZ;

COMMENT ON COLUMN addresses.courier_city_id IS
    'SribeesExpress city_id from POST /ecommerce/locations/resolve. NULL = never resolved, or resolved: false (see courier_resolve_reason).';
COMMENT ON COLUMN addresses.courier_resolve_reason IS
    'Populated only when resolution failed: post_office_not_found | not_yet_configured | no_delivery_to_this_area | ambiguous_post_office';

-- ---------------------------------------------------------------------------
-- 2. branches: registered pickup location (contract section 12)
--
-- We are a multi-branch merchant, so every branch that ships is registered
-- once with POST /ecommerce/pickup-locations and referenced by id at booking
-- time. NULL means "not registered yet" -- booking falls back to whatever
-- default origin SribeesExpress holds for our account.
-- ---------------------------------------------------------------------------
ALTER TABLE branches
    ADD COLUMN IF NOT EXISTS courier_pickup_location_id  INTEGER,
    ADD COLUMN IF NOT EXISTS courier_pickup_synced_at    TIMESTAMPTZ;

-- ---------------------------------------------------------------------------
-- 3. orders: delivery outcome + the fields the new booking call returns
--
-- courier_waybill_number (from 031) is kept and still populated, but the new
-- contract confirms shipment_id IS the waybill -- both columns now hold the
-- same value. Kept rather than dropped because GET /orders already returns it
-- and the mobile app reads that field.
-- ---------------------------------------------------------------------------
ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS courier_tracking_url          VARCHAR(500),
    ADD COLUMN IF NOT EXISTS courier_pickup_location_name  VARCHAR(255),
    ADD COLUMN IF NOT EXISTS courier_handover_required     BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS courier_handover_verified     BOOLEAN,
    ADD COLUMN IF NOT EXISTS courier_handover_attempts     INTEGER,
    ADD COLUMN IF NOT EXISTS courier_cod_collected         BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS courier_cod_collected_amount  NUMERIC(12, 2),
    ADD COLUMN IF NOT EXISTS courier_cod_collected_at      TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS courier_failure_note          TEXT;

-- The handover code is now generated for SribeesExpress, which accepts 4-6
-- digits (contract section 7). The 3-char column from migration 030 is below
-- their minimum, so widen it. Existing 3-digit codes stay valid in-place.
ALTER TABLE orders
    ALTER COLUMN delivery_verification_code TYPE VARCHAR(6);

COMMENT ON COLUMN orders.courier_handover_verified IS
    'Prepaid orders only. NULL when no handover code was required. TRUE once SribeesExpress reports the rider entered the matching code.';
COMMENT ON COLUMN orders.courier_cod_collected IS
    'COD orders only. Set from the delivered webhook''s cod block -- delivered alone does NOT mean the cash was collected.';
COMMENT ON COLUMN orders.courier_failure_note IS
    'Rider free text on a failed/returned shipment. Free text by design (contract section 8) -- never branch business logic on its exact wording.';

-- ---------------------------------------------------------------------------
-- 4. Webhook event dedupe (contract section 10.2: event_id is a stable
--    dedupe key). Also the audit trail for what SribeesExpress told us and
--    when -- with no webhook retry on their side (section 10.4) this table is
--    what a missed-event investigation actually reads.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS courier_webhook_events (
    event_id      VARCHAR(64)  PRIMARY KEY,
    event_type    VARCHAR(64)  NOT NULL,
    shipment_id   VARCHAR(64),
    status        VARCHAR(32),
    payload       JSONB        NOT NULL,
    received_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_courier_webhook_events_shipment
    ON courier_webhook_events (shipment_id);

-- ---------------------------------------------------------------------------
-- 5. Reconciliation poll cursor (contract section 7a).
--
-- SribeesExpress has no webhook retry, so GET /ecommerce/shipments
-- ?updated_since=... is the real source of truth. One row, holding the
-- high-water mark of the last successful sweep.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS courier_sync_state (
    id                 SMALLINT     PRIMARY KEY DEFAULT 1,
    last_synced_at     TIMESTAMPTZ,
    last_run_at        TIMESTAMPTZ,
    last_error         TEXT,
    CONSTRAINT courier_sync_state_singleton CHECK (id = 1)
);

INSERT INTO courier_sync_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 6. Indexes for the reconciliation sweep and for open-shipment lookups.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_orders_courier_open_shipments
    ON orders (courier_tracking_status)
    WHERE courier_shipment_id IS NOT NULL
      AND courier_tracking_status NOT IN ('delivered', 'cancelled', 'returned');
