-- Free delivery: a branch's offer to everyone it delivers to, counted from the
-- moment it is switched on, and grants to chosen customers. The courier is still
-- paid; orders record the amount waived.
BEGIN;
CREATE TABLE IF NOT EXISTS free_delivery_branch_offers (
 branch_id UUID PRIMARY KEY REFERENCES branches(branch_id) ON DELETE CASCADE,
 enabled BOOLEAN NOT NULL DEFAULT FALSE,
 months INTEGER NOT NULL DEFAULT 3,
 min_order NUMERIC(10,2) NOT NULL DEFAULT 0,
 enabled_at TIMESTAMPTZ,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 CHECK (months BETWEEN 1 AND 24), CHECK (min_order >= 0)
);
CREATE TABLE IF NOT EXISTS free_delivery_grants (
 grant_id UUID PRIMARY KEY,
 user_id UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
 branch_id UUID REFERENCES branches(branch_id) ON DELETE CASCADE,
 starts_at TIMESTAMPTZ NOT NULL,
 ends_at TIMESTAMPTZ NOT NULL,
 min_order NUMERIC(10,2) NOT NULL DEFAULT 0,
 note VARCHAR(200),
 granted_by UUID,
 revoked_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 CHECK (ends_at > starts_at), CHECK (min_order >= 0)
);
CREATE INDEX IF NOT EXISTS ix_free_delivery_grants_user ON free_delivery_grants (user_id);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS free_delivery_source VARCHAR(10);
COMMIT;
