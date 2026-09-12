-- =============================================================================
-- 051_courier_credentials.sql
--
-- Move the SribeesExpress host and merchant keys out of the deployment's
-- environment and into the database, hold BOTH environments at once, and
-- encrypt the keys at rest.
--
-- Why none of this belongs in the environment:
--
--   * The sandbox -> live switch is the change most likely to be made under
--     time pressure. Making it a redeploy is how a launch slips. Holding both
--     sets of credentials and flipping a switch makes it an ops action.
--   * A branch can hold its own SribeesExpress merchant account -- its own
--     key, its own pickup locations, its own COD ledger. One environment
--     variable cannot express "per branch, per environment".
--   * The base URL is not a secret. Anyone holding a key already knows which
--     host it belongs to, so it was never deployment config in the first
--     place.
--
-- Resolution for a call on behalf of a branch:
--
--     environment = branches.courier_environment
--                   -> app_settings 'courier_environment'  (default 'test')
--     api_key     = branches.courier_api_key_<env>
--                   -> app_settings 'courier_api_key_<env>'
--     base_url    = app_settings 'courier_base_url_<env>'
--
-- The base URL is per-environment but never per-branch: sandbox and live are
-- different hosts, but every merchant on a host shares it.
--
-- ENCRYPTION. The key columns and the two app_settings key rows hold Fernet
-- ciphertext written by app/core/crypto.py, prefixed `enc:v1:`. The master key
-- is CREDENTIAL_ENCRYPTION_KEY in the environment -- deliberately NOT in this
-- database, because a master key stored beside the data it protects protects
-- nothing. That one variable replaces every per-branch credential that used to
-- live in deployment config.
--
-- Idempotent: safe to re-run on every deploy.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Per-branch credentials
-- ---------------------------------------------------------------------------
ALTER TABLE branches
    ADD COLUMN IF NOT EXISTS courier_api_key_test     TEXT,
    ADD COLUMN IF NOT EXISTS courier_api_key_live     TEXT,
    ADD COLUMN IF NOT EXISTS courier_environment      VARCHAR(10),
    ADD COLUMN IF NOT EXISTS courier_api_key_set_at   TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS courier_api_key_set_by   VARCHAR(255);

-- An earlier shape of this migration (never released) had a single
-- environment-less key column. Dropping it here rather than leaving a second,
-- silently-unread place for a credential to sit.
ALTER TABLE branches
    DROP COLUMN IF EXISTS courier_api_key;

COMMENT ON COLUMN branches.courier_api_key_test IS
    'This branch''s own SribeesExpress SANDBOX key, Fernet-encrypted (enc:v1:...). NULL = use the account-wide key.';
COMMENT ON COLUMN branches.courier_api_key_live IS
    'This branch''s own SribeesExpress LIVE key, Fernet-encrypted (enc:v1:...). NULL = use the account-wide key.';
COMMENT ON COLUMN branches.courier_environment IS
    'Pins this branch to test | live regardless of the account default. NULL = follow the account. Exists for a staged go-live: one branch books real parcels while the rest stay in the sandbox.';
COMMENT ON COLUMN branches.courier_api_key_set_by IS
    'Email of the Super Admin who last set a key here. The key itself is not recoverable -- who changed it is.';

ALTER TABLE branches
    DROP CONSTRAINT IF EXISTS branches_courier_environment_check;
ALTER TABLE branches
    ADD CONSTRAINT branches_courier_environment_check
    CHECK (courier_environment IS NULL OR courier_environment IN ('test', 'live'));

-- ---------------------------------------------------------------------------
-- 2. Account-wide configuration, in app_settings
--
-- setting_id is generated Python-side by the model (uuid4) and has no DB-side
-- default, so a raw INSERT has to supply one -- the same trap migration 018
-- documents for post_office_directory.
--
-- Seeded is_active FALSE and empty except the sandbox host, which is public
-- and already known. Installing this migration therefore configures nothing on
-- its own: a Super Admin still has to enter a key before any parcel can be
-- booked, which is the correct default for a credential.
-- ---------------------------------------------------------------------------
INSERT INTO app_settings (setting_id, key, value, description, is_active)
VALUES
    (gen_random_uuid(), 'courier_environment', 'test',
     'Which SribeesExpress environment branches book in by default: test | live. Defaults to test so an unconfigured deployment cannot create real parcels.',
     TRUE),
    (gen_random_uuid(), 'courier_base_url_test', 'https://devapiexpress.sribees.com',
     'SribeesExpress SANDBOX origin, WITHOUT the /api/v1/ecommerce path -- the client appends it.',
     TRUE),
    (gen_random_uuid(), 'courier_base_url_live', NULL,
     'SribeesExpress LIVE origin, WITHOUT the /api/v1/ecommerce path. Set before going live.',
     FALSE),
    (gen_random_uuid(), 'courier_api_key_test', NULL,
     'Account-wide SribeesExpress SANDBOX key, Fernet-encrypted (enc:v1:...).',
     FALSE),
    (gen_random_uuid(), 'courier_api_key_live', NULL,
     'Account-wide SribeesExpress LIVE key, Fernet-encrypted (enc:v1:...).',
     FALSE)
ON CONFLICT (key) DO NOTHING;

-- The single-key row from the earlier unreleased shape, if a dev database ran
-- it. Removed rather than migrated: it was environment-less, so there is no
-- correct answer to which of the two columns above it belonged in.
DELETE FROM app_settings WHERE key = 'courier_api_key';
