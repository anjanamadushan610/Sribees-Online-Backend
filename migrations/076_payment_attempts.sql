-- =============================================================================
-- Migration 076: every online payment attempt, and orders that wait for one.
--
-- An order paid by card or bank transfer used to count as placed the moment
-- it was created: "order placed" went out, the branch saw it, and a payment
-- that then failed left it sitting there with no way to pay again. Now such an
-- order is 'awaiting_payment' (orders.status is a plain VARCHAR, no enum to
-- alter) until the gateway confirms the money, and each try at paying is a row
-- here:
--
--   reference     what WE sent the gateway to identify this try (Marx's
--                 merchantRID). The first try is the order number, as before;
--                 a retry is "<order number>-<n>". Unique, so a return from
--                 the gateway maps to exactly one try.
--   gateway_ref   what the gateway gave back (Marx's trId).
--   status        started -> paid | failed | pending | superseded | expired.
--                 A later try does not make an earlier one irrelevant: the
--                 customer may have paid it after all, so open tries are
--                 still checked until the order settles.
--   raw           the gateway's last answer, for disputes and support.
--
-- Idempotent -- the deploy applies this file again on each release.
-- =============================================================================

CREATE TABLE IF NOT EXISTS payment_attempts (
    attempt_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id       UUID NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    attempt_no     INTEGER NOT NULL,
    method_code    VARCHAR(40) NOT NULL,
    gateway        VARCHAR(30) NOT NULL,
    reference      VARCHAR(80) NOT NULL UNIQUE,
    gateway_ref    VARCHAR(80),
    amount         NUMERIC(10, 2) NOT NULL,
    status         VARCHAR(20) NOT NULL DEFAULT 'started',
    gateway_code   VARCHAR(80),
    raw            JSONB,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at   TIMESTAMPTZ,
    CONSTRAINT uq_payment_attempts_order_no UNIQUE (order_id, attempt_no),
    CONSTRAINT ck_payment_attempts_status CHECK (
        status IN ('started', 'paid', 'failed', 'pending', 'superseded', 'expired')
    )
);

CREATE INDEX IF NOT EXISTS ix_payment_attempts_order ON payment_attempts (order_id);
CREATE INDEX IF NOT EXISTS ix_payment_attempts_open
    ON payment_attempts (created_at)
    WHERE status IN ('started', 'pending');

-- Orders already waiting on Marx when this ships get their first try on
-- record, so the sweep can settle them like any other.
INSERT INTO payment_attempts (order_id, attempt_no, method_code, gateway, reference,
                              gateway_ref, amount, status, created_at)
SELECT o.order_id, 1, o.payment_method, 'marx', o.order_number, o.gateway_tr_id,
       o.total_amount,
       CASE o.payment_status WHEN 'paid' THEN 'paid' WHEN 'failed' THEN 'failed' ELSE 'started' END,
       o.created_at
FROM orders o
WHERE o.payment_method IN ('MARX_OTHER', 'MARX_AMEX', 'MARX_BANK')
  AND o.gateway_tr_id IS NOT NULL
ON CONFLICT DO NOTHING;
