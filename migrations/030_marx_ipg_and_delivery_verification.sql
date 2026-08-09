-- =============================================================================
-- 030_marx_ipg_and_delivery_verification.sql
--
-- Marx IPG payment-gateway state on orders, plus the delivery handover
-- verification code (Module: Payments). A non-COD order gets a 3-digit code
-- once payment is confirmed; the customer reads it to the delivery rider at
-- handover as an anti-fraud check (see docs/COURIER_API.md section 15).
--
-- Idempotent: safe to re-run on every deploy.
-- =============================================================================

ALTER TABLE orders
    -- Marx IPG gateway state. merchantRID is orders.order_number (already
    -- unique/stable) -- no separate column needed for it.
    ADD COLUMN IF NOT EXISTS gateway_tr_id        VARCHAR(50),
    ADD COLUMN IF NOT EXISTS gateway_status        VARCHAR(20),
    ADD COLUMN IF NOT EXISTS gateway_response_code VARCHAR(50),
    ADD COLUMN IF NOT EXISTS gateway_confirmed_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS gateway_raw_response  JSONB,

    -- Guards deferred cashback crediting (gateway orders only) against
    -- double-crediting if the PAID transition is ever observed twice.
    ADD COLUMN IF NOT EXISTS cashback_credited_at  TIMESTAMPTZ,

    -- Delivery handover verification code (non-COD orders only).
    ADD COLUMN IF NOT EXISTS delivery_verification_code         VARCHAR(3),
    ADD COLUMN IF NOT EXISTS delivery_verification_status       VARCHAR(20) DEFAULT 'not_required',
    ADD COLUMN IF NOT EXISTS delivery_verification_attempts     SMALLINT DEFAULT 0,
    ADD COLUMN IF NOT EXISTS delivery_verification_confirmed_at TIMESTAMPTZ;

COMMENT ON COLUMN orders.delivery_verification_status IS
    'not_required (COD or not yet paid) | pending (code generated, awaiting handover) | confirmed | failed (too many wrong attempts)';

CREATE INDEX IF NOT EXISTS idx_orders_gateway_tr_id
    ON orders (gateway_tr_id) WHERE gateway_tr_id IS NOT NULL;
