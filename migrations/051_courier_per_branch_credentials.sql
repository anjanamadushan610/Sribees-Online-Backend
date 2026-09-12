-- =============================================================================
-- 051_courier_per_branch_credentials.sql
--
-- Let each branch hold its own SribeesExpress merchant key.
--
-- Until now the key lived only in COURIER_BASE_URL/COURIER_API_KEY on the
-- server, which meant two things we do not want:
--
--   1. Changing it -- including the sandbox -> live switch at go-live --
--      needed a redeploy, so the one action most likely to be taken under
--      time pressure was the one that required an engineer.
--   2. Every branch booked against one merchant account. That is fine while
--      SRIBEES is one account, but a branch that signs with SribeesExpress in
--      its own name has its own key, its own pickup locations and its own COD
--      ledger, and there was nowhere to put that.
--
-- Resolution order for any courier call made on behalf of a branch:
--
--      branches.courier_api_key            (this branch's own account)
--   -> app_settings 'courier_api_key'      (the account-wide default, UI-editable)
--   -> settings.courier_api_key            (the env var, unchanged)
--
-- Account-level operations that are not about one branch (coverage sync, the
-- webhook registration, the reconciliation sweep) always use the account-wide
-- key -- a branch key would answer for the wrong account.
--
-- SECURITY NOTE: this column holds a live credential in plaintext, exactly as
-- app_settings already does for every other secret it carries. It is readable
-- by anything with database access. The API never returns it -- only a mask --
-- and only a Super Admin can set it. Encrypting secrets at rest is a
-- project-wide change (there is no KMS or key-derivation story here yet) and
-- is deliberately NOT invented in this migration; it is tracked as its own
-- piece of work.
--
-- Idempotent: safe to re-run on every deploy.
-- =============================================================================

ALTER TABLE branches
    ADD COLUMN IF NOT EXISTS courier_api_key          TEXT,
    ADD COLUMN IF NOT EXISTS courier_api_key_set_at   TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS courier_api_key_set_by   VARCHAR(255);

COMMENT ON COLUMN branches.courier_api_key IS
    'This branch''s own SribeesExpress merchant key (sk_test_... / sk_live_...). NULL = use the account-wide key. Plaintext; never returned by the API, only masked.';
COMMENT ON COLUMN branches.courier_api_key_set_at IS
    'When this branch key was last set, so an operator can tell a stale key from one nobody has touched.';
COMMENT ON COLUMN branches.courier_api_key_set_by IS
    'Email of the Super Admin who last set it. The key itself is not auditable after the fact -- who changed it is.';

-- The account-wide default lives in app_settings alongside the other
-- UI-editable platform settings. Seeded inactive and empty: an empty value
-- falls through to the environment variable, so installing this migration
-- changes nothing about how an existing deployment books parcels.
-- setting_id is generated Python-side by the model (uuid4) and has no DB-side
-- default, so a raw INSERT has to supply one -- the same trap migration 018
-- documents for post_office_directory.
INSERT INTO app_settings (setting_id, key, value, description, is_active)
VALUES (
    gen_random_uuid(),
    'courier_api_key',
    NULL,
    'Account-wide SribeesExpress merchant key. Empty = fall back to the COURIER_API_KEY environment variable. Overridden per branch by branches.courier_api_key.',
    FALSE
)
ON CONFLICT (key) DO NOTHING;
