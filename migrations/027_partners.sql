-- =============================================================================
-- 027_partners.sql — Professional referral team (partners), separate from admins.
--
-- Partners are a DISTINCT identity store, not a role on `admins`. That is the
-- whole security point: an agent must not be able to reach an admin endpoint
-- even if a role check somewhere is wrong, and a partner token must be
-- meaningless to /admin/*. Sharing the admins table would make every future
-- admin route one missing role check away from exposure.
--
-- Two commission levels:
--   Partner A recruits Partner B  (B.parent_partner_id = A)
--   B refers a customer           (users.referred_by_partner_id = B)
--   That customer orders          -> B earns level 1, A earns level 2
--
-- Idempotent: safe to re-run on every deploy.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Partners
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS partners (
    partner_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    email             VARCHAR(255) NOT NULL,
    password_hash     VARCHAR(255) NOT NULL,
    full_name         VARCHAR(150) NOT NULL,
    phone             VARCHAR(20),

    -- The code this partner hands out. Looked up in its own table, so it does
    -- not have to be unique against customer codes — but redemption checks
    -- partners first, and both columns carry their own unique index.
    referral_code     VARCHAR(12),

    -- Who recruited this partner. WRITE ONCE — moving it would rewrite who is
    -- owed level-2 commission on history already recorded.
    parent_partner_id UUID REFERENCES partners(partner_id) ON DELETE SET NULL,

    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    last_login        TIMESTAMPTZ,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    updated_at        TIMESTAMPTZ DEFAULT NOW(),

    -- A partner cannot be their own upline.
    CONSTRAINT chk_partner_no_self_parent
        CHECK (parent_partner_id IS NULL OR parent_partner_id <> partner_id)
);

-- Login is by email, so it must be unique or authentication is ambiguous.
CREATE UNIQUE INDEX IF NOT EXISTS uq_partners_email ON partners (LOWER(email));

CREATE UNIQUE INDEX IF NOT EXISTS uq_partners_referral_code
    ON partners (referral_code) WHERE referral_code IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_partners_parent
    ON partners (parent_partner_id) WHERE parent_partner_id IS NOT NULL;

-- -----------------------------------------------------------------------------
-- 2. A customer can be referred by a partner instead of another customer
-- -----------------------------------------------------------------------------
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS referred_by_partner_id UUID
        REFERENCES partners(partner_id) ON DELETE SET NULL;

COMMENT ON COLUMN users.referred_by_partner_id IS
    'Professional partner who signed this customer up. WRITE ONCE. Mutually exclusive with referred_by_user_id.';

CREATE INDEX IF NOT EXISTS idx_users_referred_by_partner
    ON users (referred_by_partner_id) WHERE referred_by_partner_id IS NOT NULL;

-- One referrer, not two. Attribution that could point at both is unauditable.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_referral_single_source'
    ) THEN
        ALTER TABLE users
            ADD CONSTRAINT chk_referral_single_source
            CHECK (referred_by_user_id IS NULL OR referred_by_partner_id IS NULL);
    END IF;
END $$;

-- -----------------------------------------------------------------------------
-- 3. Commissions can be owed to a partner as well as a customer
-- -----------------------------------------------------------------------------
ALTER TABLE referral_commissions
    ADD COLUMN IF NOT EXISTS beneficiary_partner_id UUID
        REFERENCES partners(partner_id) ON DELETE CASCADE;

-- A partner-owed row has no beneficiary_user_id, so that column can no longer
-- be NOT NULL.
ALTER TABLE referral_commissions
    ALTER COLUMN beneficiary_user_id DROP NOT NULL;

-- Exactly one beneficiary. Neither, or both, is a bug we would rather fail on
-- than pay out.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_commission_single_beneficiary'
    ) THEN
        ALTER TABLE referral_commissions
            ADD CONSTRAINT chk_commission_single_beneficiary
            CHECK (
                (beneficiary_user_id IS NOT NULL)::int
              + (beneficiary_partner_id IS NOT NULL)::int = 1
            );
    END IF;
END $$;

-- The old table-level UNIQUE cannot express "per beneficiary of either kind",
-- because NULLs in a composite unique never collide. Replace it with one
-- partial unique index per beneficiary type — that keeps the idempotency
-- guarantee (no double payout for the same order) for both.
ALTER TABLE referral_commissions
    DROP CONSTRAINT IF EXISTS uq_referral_commission_order_beneficiary_level;

CREATE UNIQUE INDEX IF NOT EXISTS uq_referral_comm_order_user_level
    ON referral_commissions (order_id, beneficiary_user_id, level)
    WHERE beneficiary_user_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_referral_comm_order_partner_level
    ON referral_commissions (order_id, beneficiary_partner_id, level)
    WHERE beneficiary_partner_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_referral_comm_partner_status
    ON referral_commissions (beneficiary_partner_id, status)
    WHERE beneficiary_partner_id IS NOT NULL;
