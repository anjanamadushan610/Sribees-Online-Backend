-- =============================================================================
-- 093 - Finance outbox: the event feed Sribees Finance reads
-- =============================================================================
--
-- Sribees Finance (a separate app with its own database) keeps the books. It
-- learns about money and stock here, from rows written IN THE SAME TRANSACTION
-- as the change they describe: if an order update commits, its event exists;
-- if it rolls back, so does the event. Finance pulls them in `seq` order
-- through GET /internal/finance/events and records each event_id it has
-- posted, so a replay never counts twice.
--
-- wallet_transactions.reason names WHY a wallet moved (refund of a cancel,
-- cashback, commission, ...), which decides the ledger accounts. NULL on rows
-- written before this migration.
--
-- Nothing is written until FINANCE_OUTBOX_ENABLED is true (ships dark).
-- REPLAYED ON EVERY DEPLOY: every statement is idempotent.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS finance_outbox (
    seq          BIGSERIAL PRIMARY KEY,
    event_id     UUID        NOT NULL UNIQUE,
    event_type   VARCHAR(40) NOT NULL,
    aggregate_id UUID,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload      JSONB       NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_finance_outbox_aggregate ON finance_outbox (aggregate_id);

ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS reason VARCHAR(40);

COMMIT;
