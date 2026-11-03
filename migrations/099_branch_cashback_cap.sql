-- 099: cap on a branch's cashback override (owner, 2026-11-03).
--
-- Only a Branch Manager (own branch) or a Super Admin may set a branch's
-- cashback, and never above this %. Enforced in app/services/branch_cashback.py;
-- the code falls back to 30 if the row is missing or malformed.

BEGIN;

INSERT INTO app_settings (setting_id, key, value, description, is_active)
VALUES
    (gen_random_uuid(), 'cashback_branch_max_percent', '30',
     'Highest cashback % a branch may set on a product (Branch Manager and Super Admin).', TRUE)
ON CONFLICT (key) DO NOTHING;

COMMIT;
