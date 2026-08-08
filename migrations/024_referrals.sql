-- =============================================================================
-- 024_referrals.sql — Referral codes and commission ledger.
--
-- A referrer earns a percentage of what the people they invited spend.
--
-- Two constraints here are load-bearing and deliberate:
--
--   1. uq_referral_commission_order_beneficiary_level — the idempotency guard.
--      Accrual can run twice (retried status transition, replayed job) without
--      ever paying a referrer twice for the same order.
--
--   2. chk_referral_no_self — a user cannot be their own referrer. The service
--      checks this too, but a database that permits it will eventually contain
--      it, and self-referral is the cheapest fraud there is.
--
-- Idempotent: safe to re-run on every deploy.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Referral fields on users
-- -----------------------------------------------------------------------------
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS referral_code VARCHAR(12),
    ADD COLUMN IF NOT EXISTS referred_by_user_id UUID REFERENCES users(user_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS referred_at TIMESTAMPTZ;

COMMENT ON COLUMN users.referral_code IS
    'This user''s own invite code. Generated on first request, never reused.';
COMMENT ON COLUMN users.referred_by_user_id IS
    'Who invited this user. WRITE ONCE — updating it breaks ledger auditability.';

-- A code must be globally unique or redemption is ambiguous.
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_referral_code
    ON users (referral_code) WHERE referral_code IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_users_referred_by
    ON users (referred_by_user_id) WHERE referred_by_user_id IS NOT NULL;

-- Self-referral is the cheapest fraud available, so it is refused at the
-- storage layer as well as in the service.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_referral_no_self'
    ) THEN
        ALTER TABLE users
            ADD CONSTRAINT chk_referral_no_self
            CHECK (referred_by_user_id IS NULL OR referred_by_user_id <> user_id);
    END IF;
END $$;

-- -----------------------------------------------------------------------------
-- 2. Commission ledger
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS referral_commissions (
    commission_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    order_id              UUID NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    order_number          VARCHAR(50),

    beneficiary_user_id   UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    source_user_id        UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,

    -- 1 = direct app invite. 2 = professional referral team's second tier.
    -- Stored rather than inferred so adding tier 2 later needs no backfill.
    level                 SMALLINT NOT NULL DEFAULT 1,

    -- Snapshotted at order time: changing the platform rate must not silently
    -- rewrite what people have already earned.
    rate_percentage       DOUBLE PRECISION NOT NULL,

    -- Audit only. Never returned to the beneficiary — a referrer does not get
    -- to see their friend's basket value.
    order_subtotal        NUMERIC(10,2) NOT NULL,

    amount                NUMERIC(10,2) NOT NULL,

    -- pending -> claimable (on delivery) -> paid, or -> reversed
    status                VARCHAR(20) NOT NULL DEFAULT 'pending',

    claimed_at            TIMESTAMPTZ,
    wallet_transaction_id UUID REFERENCES wallet_transactions(transaction_id) ON DELETE SET NULL,

    created_at            TIMESTAMPTZ DEFAULT NOW(),
    updated_at            TIMESTAMPTZ DEFAULT NOW(),

    CONSTRAINT uq_referral_commission_order_beneficiary_level
        UNIQUE (order_id, beneficiary_user_id, level),

    -- Earning commission on your own order is self-dealing, not a referral.
    CONSTRAINT chk_referral_commission_not_self
        CHECK (beneficiary_user_id <> source_user_id)
);

CREATE INDEX IF NOT EXISTS idx_referral_comm_beneficiary_status
    ON referral_commissions (beneficiary_user_id, status);

CREATE INDEX IF NOT EXISTS idx_referral_comm_beneficiary_source
    ON referral_commissions (beneficiary_user_id, source_user_id);

CREATE INDEX IF NOT EXISTS idx_referral_comm_order
    ON referral_commissions (order_id);
