-- Additive only. Existing shopping wallets and historical paid commissions stay intact.
BEGIN;
ALTER TABLE partners ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(user_id) ON DELETE RESTRICT;
ALTER TABLE partners ADD COLUMN IF NOT EXISTS role VARCHAR(20);
UPDATE partners SET role = CASE WHEN parent_partner_id IS NULL THEN 'master_partner' ELSE 'partner' END WHERE role IS NULL;
-- Fresh-schema legacy seed rows may have inherited the ORM default.
UPDATE partners SET role = 'partner' WHERE parent_partner_id IS NOT NULL AND role = 'master_partner';
ALTER TABLE partners ALTER COLUMN role SET DEFAULT 'master_partner';
ALTER TABLE partners ALTER COLUMN role SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_partner_linked_user ON partners(user_id) WHERE user_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS partner_applications (
 application_id UUID PRIMARY KEY, user_id UUID NOT NULL UNIQUE REFERENCES users(user_id) ON DELETE RESTRICT,
 password_hash VARCHAR(255), status VARCHAR(20) NOT NULL DEFAULT 'pending', decision_by UUID,
 decision_note VARCHAR(500), created_at TIMESTAMPTZ DEFAULT NOW(), decided_at TIMESTAMPTZ,
 CHECK(status IN ('pending','approved','rejected'))
);
CREATE TABLE IF NOT EXISTS partner_wallets (
 partner_id UUID PRIMARY KEY REFERENCES partners(partner_id) ON DELETE RESTRICT,
 balance NUMERIC(14,2) NOT NULL DEFAULT 0, updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS partner_wallet_entries (
 entry_id UUID PRIMARY KEY, partner_id UUID NOT NULL REFERENCES partner_wallets(partner_id) ON DELETE RESTRICT,
 reference VARCHAR(160) NOT NULL, kind VARCHAR(40) NOT NULL, amount NUMERIC(14,2) NOT NULL,
 balance_after NUMERIC(14,2) NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW(),
 CONSTRAINT uq_partner_wallet_reference UNIQUE(partner_id, reference)
);
CREATE TABLE IF NOT EXISTS partner_withdrawals (
 withdrawal_id UUID PRIMARY KEY, partner_id UUID NOT NULL REFERENCES partners(partner_id) ON DELETE RESTRICT,
 request_id UUID NOT NULL, amount NUMERIC(14,2) NOT NULL CHECK(amount > 0), status VARCHAR(20) NOT NULL DEFAULT 'pending',
 bank_name VARCHAR(120) NOT NULL, branch_name VARCHAR(120) NOT NULL, account_name VARCHAR(150) NOT NULL,
 account_number VARCHAR(40) NOT NULL, decision_by UUID, payment_reference VARCHAR(160), decision_note TEXT,
 created_at TIMESTAMPTZ DEFAULT NOW(), decided_at TIMESTAMPTZ,
 CONSTRAINT uq_partner_withdrawal_request UNIQUE(partner_id, request_id), CHECK(status IN ('pending','paid','rejected'))
);
CREATE INDEX IF NOT EXISTS ix_partner_wallet_entries_partner ON partner_wallet_entries(partner_id);
CREATE INDEX IF NOT EXISTS ix_partner_withdrawals_partner ON partner_withdrawals(partner_id);

COMMIT;
