-- =============================================================================
-- Migration 104: customer number (shown as SB-47215)
--
-- The profile screen used to show "SR" + the first six characters of the
-- user's UUID: not guaranteed unique, and nothing staff could look up. The
-- owner's rule (2026-11-07): SB- and five digits, made by an algorithm from
-- the date the customer registered -- NOT handed out in order -- and never
-- the same for two people.
--
-- The algorithm (sribees_customer_no_candidate): md5 of the registration
-- moment (created_at to the microsecond, in UTC) and an attempt counter,
-- folded into 10000..99999 so the number always has five digits. Attempt 0
-- is tried first; if that number is already someone's, attempt 1, 2, ... --
-- so two people who registered in the same microsecond still differ. A
-- unique index is the final guarantee, and the assigning trigger takes an
-- advisory lock so two sign-ups at once cannot both claim the same number.
--
-- Capacity: 90,000 numbers. Long before that, widen the range (six digits);
-- the trigger raises rather than loop forever if the range is ever full.
--
-- Safe to replay on every deploy: only rows still without a number are
-- numbered (oldest registration first), functions/trigger are CREATE OR
-- REPLACE, the index is IF NOT EXISTS. A number, once given, never changes.
-- =============================================================================

BEGIN;

ALTER TABLE users ADD COLUMN IF NOT EXISTS customer_no INTEGER;

CREATE UNIQUE INDEX IF NOT EXISTS ux_users_customer_no ON users (customer_no);

CREATE OR REPLACE FUNCTION sribees_customer_no_candidate(registered timestamptz, attempt integer)
RETURNS integer
LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT 10000 + ((('x' || substr(md5(
               to_char(registered AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US')
               || ':' || attempt::text), 1, 8))::bit(32)::bigint) % 90000)::integer
$$;

CREATE OR REPLACE FUNCTION sribees_assign_customer_no(registered timestamptz)
RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    attempt   integer := 0;
    candidate integer;
BEGIN
    -- One assignment at a time, so two concurrent sign-ups cannot both see
    -- the same free number. Held only until the transaction ends.
    PERFORM pg_advisory_xact_lock(hashtext('sribees_customer_no'));
    LOOP
        candidate := sribees_customer_no_candidate(registered, attempt);
        EXIT WHEN NOT EXISTS (SELECT 1 FROM users WHERE customer_no = candidate);
        attempt := attempt + 1;
        IF attempt > 100000 THEN
            RAISE EXCEPTION 'no free five-digit customer number left: widen the range';
        END IF;
    END LOOP;
    RETURN candidate;
END
$$;

CREATE OR REPLACE FUNCTION sribees_users_customer_no()
RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.customer_no IS NULL THEN
        NEW.customer_no := sribees_assign_customer_no(COALESCE(NEW.created_at, now()));
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS trg_users_customer_no ON users;
CREATE TRIGGER trg_users_customer_no
    BEFORE INSERT ON users
    FOR EACH ROW EXECUTE FUNCTION sribees_users_customer_no();

-- Number the accounts that have none yet, oldest registration first, so an
-- older account wins a clash with a newer one.
DO $$
DECLARE
    r record;
BEGIN
    FOR r IN SELECT user_id, created_at FROM users
              WHERE customer_no IS NULL
              ORDER BY created_at, user_id
    LOOP
        UPDATE users
           SET customer_no = sribees_assign_customer_no(COALESCE(r.created_at, now()))
         WHERE user_id = r.user_id;
    END LOOP;
END
$$;

COMMIT;
