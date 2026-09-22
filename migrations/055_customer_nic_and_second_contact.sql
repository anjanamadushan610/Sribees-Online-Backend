-- =============================================================================
-- Migration 055: the NIC becomes the customer's identity, and every order
-- carries two numbers the courier can call.
--
-- Until now a customer WAS their mobile number: `users.phone` was unique and
-- was the thing sign-up, login and support all keyed off. That made a number
-- impossible to change and a second number impossible to have. The NIC takes
-- that role -- unique, and write-once once set (enforced in
-- AuthService.update_profile, not by a trigger, so support can still correct
-- one) -- and the phone goes back to being what it actually is: a way to
-- reach someone.
--
-- `nic` is NULLABLE on purpose, exactly as `email` is. Phone-first sign-up
-- creates the row at OTP time and the profile step fills it in seconds later,
-- so a row legitimately has no NIC for that moment. "Mandatory" belongs at
-- the profile gate, which can tell a half-finished sign-up from a bad one;
-- a NOT NULL here could only reject the OTP itself.
--
-- SQLAlchemy create_all() adds these on a fresh DB. Every statement below is
-- idempotent: the deploy applies this file again on each release.
-- =============================================================================

ALTER TABLE users ADD COLUMN IF NOT EXISTS nic             VARCHAR(20);
ALTER TABLE users ADD COLUMN IF NOT EXISTS alternate_phone VARCHAR(20);

-- One account per NIC. Partial, so the accounts still mid-sign-up (NIC NULL)
-- do not collide with each other.
CREATE UNIQUE INDEX IF NOT EXISTS ix_users_nic_unique
    ON users (nic)
    WHERE nic IS NOT NULL;

-- REMOVED 2026-09-22: a synthetic-NIC backfill used to sit here.
--
-- It rested on "production does not use this database", which was not true --
-- this file is applied to the production database by every deploy. On
-- 2026-09-21 it stamped 89 real customers with invented NICs
-- (199000000001 upward), and because the NIC is write-once
-- (AuthService.update_profile) those customers can no longer enter their real
-- one; the app answers "Contact support if it needs correcting".
--
-- It also broke every deploy after the first. ROW_NUMBER() restarts at 1 on
-- each run while only selecting the NIC-less rows, so the next customer to
-- sign up and stop before the profile step was handed 199000000001 again --
-- a number already taken. Deploys failed on the unique index from then on:
--   ERROR: duplicate key value violates unique constraint "ix_users_nic_unique"
--
-- Nothing replaces it. A NULL nic is a supported state -- it is exactly what
-- a phone-first sign-up has between OTP and the profile step -- and the
-- profile gate is what asks for the real number. Inventing a government ID
-- for someone is not a migration's job.

-- -----------------------------------------------------------------------------
-- Orders: who to call about THIS parcel, frozen at checkout.
--
-- The profile's numbers can change afterwards; the number the customer
-- confirmed when they placed the order must not. Nullable in the column and
-- required by CreateOrderRequest, so orders placed before this still load.
-- -----------------------------------------------------------------------------
ALTER TABLE orders ADD COLUMN IF NOT EXISTS contact_phone   VARCHAR(20);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS alternate_phone VARCHAR(20);

-- Existing orders keep the number their delivery address carried.
UPDATE orders
SET    contact_phone = addresses.phone
FROM   addresses
WHERE  orders.delivery_address_id = addresses.address_id
  AND  orders.contact_phone IS NULL
  AND  addresses.phone IS NOT NULL;
