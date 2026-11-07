-- =============================================================================
-- Migration 104: customer number (shown as SB-00001)
--
-- The profile screen used to show "SR" + the first six characters of the
-- user's UUID: not guaranteed unique, and nothing staff could look up. The
-- owner asked (2026-11-07) for SB- and five digits, given by sign-up date.
--
-- users.customer_no comes from a sequence, so concurrent sign-ups can never
-- share a number and a deleted account's number is never handed out again.
-- Accounts that exist when this lands are numbered oldest first
-- (created_at, then user_id to break a tie). The app formats it with at least
-- five digits; past 99,999 it simply grows to six.
--
-- Safe to replay on every deploy: only rows still without a number are
-- numbered, the default and index are IF NOT EXISTS / idempotent, and the
-- sequence is only ever moved forward.
-- =============================================================================

BEGIN;

CREATE SEQUENCE IF NOT EXISTS users_customer_no_seq AS INTEGER START 1;

ALTER TABLE users ADD COLUMN IF NOT EXISTS customer_no INTEGER;

-- Never behind the highest number already handed out (a database restored
-- from a dump, or rows written before the default existed).
SELECT setval('users_customer_no_seq',
              GREATEST((SELECT COALESCE(MAX(customer_no), 0) FROM users), 1),
              (SELECT MAX(customer_no) IS NOT NULL FROM users));

-- Number the accounts that have none yet, oldest sign-up first.
WITH ordered AS (
    SELECT user_id, ROW_NUMBER() OVER (ORDER BY created_at, user_id) AS rn
      FROM users
     WHERE customer_no IS NULL
), base AS (
    SELECT COALESCE(MAX(customer_no), 0) AS n FROM users
)
UPDATE users u
   SET customer_no = base.n + ordered.rn
  FROM ordered, base
 WHERE u.user_id = ordered.user_id;

SELECT setval('users_customer_no_seq',
              GREATEST((SELECT COALESCE(MAX(customer_no), 0) FROM users), 1),
              (SELECT MAX(customer_no) IS NOT NULL FROM users));

ALTER TABLE users ALTER COLUMN customer_no SET DEFAULT nextval('users_customer_no_seq');
ALTER SEQUENCE users_customer_no_seq OWNED BY users.customer_no;

CREATE UNIQUE INDEX IF NOT EXISTS ux_users_customer_no ON users (customer_no);

COMMIT;
