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
-- SQLAlchemy create_all() adds these on a fresh DB. Idempotent -- safe to re-run.
-- =============================================================================

ALTER TABLE users ADD COLUMN IF NOT EXISTS nic             VARCHAR(20);
ALTER TABLE users ADD COLUMN IF NOT EXISTS alternate_phone VARCHAR(20);

-- One account per NIC. Partial, so the accounts still mid-sign-up (NIC NULL)
-- do not collide with each other.
CREATE UNIQUE INDEX IF NOT EXISTS ix_users_nic_unique
    ON users (nic)
    WHERE nic IS NOT NULL;

-- Development seed accounts predate the column and would otherwise be stuck
-- at the profile gate on next launch. These are synthetic, correctly-shaped
-- 12-digit NICs; production does not use this database.
WITH seeded AS (
    SELECT user_id,
           '1990' || LPAD((ROW_NUMBER() OVER (ORDER BY created_at, user_id))::text, 8, '0') AS nic
    FROM users
    WHERE nic IS NULL
)
UPDATE users
SET    nic = seeded.nic
FROM   seeded
WHERE  users.user_id = seeded.user_id;

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
