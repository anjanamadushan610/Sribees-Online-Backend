-- =============================================================================
-- Migration 046: Internal order escalation tickets (QA spec B4, Module B)
--
-- support -> branch tickets to intercept an order before dispatch. Raised by
-- support/BM/SA; acknowledged + resolved by the Branch Manager (own branch) or
-- Super Admin.
--
-- SQLAlchemy create_all() also builds this on a fresh DB; this file covers
-- pre-existing databases. Idempotent -- safe to re-run.
-- =============================================================================

CREATE TABLE IF NOT EXISTS order_escalations (
    escalation_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id           UUID NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    branch_id          UUID,
    raised_by_admin_id UUID REFERENCES admins(admin_id) ON DELETE SET NULL,
    raised_by_name     VARCHAR(255) NOT NULL,
    category           VARCHAR(30)  NOT NULL DEFAULT 'other',
    message            TEXT         NOT NULL,
    status             VARCHAR(20)  NOT NULL DEFAULT 'open',
    resolution_note    TEXT,
    handled_by_name    VARCHAR(255),
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    acknowledged_at    TIMESTAMPTZ,
    resolved_at        TIMESTAMPTZ
);

-- Make the PK default real on a DB where create_all() built the table first
-- (the ORM declares the PK default Python-side, leaving no column default).
ALTER TABLE order_escalations ALTER COLUMN escalation_id SET DEFAULT gen_random_uuid();

CREATE INDEX IF NOT EXISTS idx_order_escalations_order  ON order_escalations (order_id);
CREATE INDEX IF NOT EXISTS idx_order_escalations_branch ON order_escalations (branch_id);
CREATE INDEX IF NOT EXISTS idx_order_escalations_status ON order_escalations (status);
