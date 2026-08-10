-- Migration 034: phone-first registration
--
-- Lets an account be created from a verified phone number alone (no email or
-- password yet — both are now optional and filled in later if the user adds
-- them). Phone becomes a unique identity key, same role email already plays.
--
-- Schema is created by SQLAlchemy Base.metadata.create_all() on fresh DBs; run
-- this against pre-existing databases. Safe to re-run.

ALTER TABLE users ALTER COLUMN email DROP NOT NULL;
ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL;

-- A named-constraint collision raises duplicate_table (42P07), not
-- duplicate_object (42710) — checking pg_constraint directly sidesteps having
-- to guess the right SQLSTATE to catch.
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'users_phone_unique'
    ) THEN
        ALTER TABLE users ADD CONSTRAINT users_phone_unique UNIQUE (phone);
    END IF;
END $$;
