-- =============================================================================
-- Migration 032: Assign a branch to the "network-wide" demo admins
--
-- 026_seed_full_dataset.sql created marketing@/inventory@/support@sribeesonline.lk
-- with branch_id = NULL, and ADMIN_CREDENTIALS.md documented them as
-- "Network-wide". Neither matches how the server actually works:
-- app/core/dependencies.py's inject_branch_filter rejects every non-super-admin
-- request with no branch_id ("Your admin account is not assigned to a branch.")
-- — by design, only super_admin is network-wide. Every Marketing Manager,
-- Inventory Manager and Customer Support endpoint (marketing, banners,
-- inventory, orders, customers) is branch-scoped, so these three seeded
-- accounts could log in but could not use a single one of their own screens.
--
-- Fix: give them a real branch, same as every other seeded admin.
--
-- marketing@ and inventory@ go to Colombo Main, matching the other
-- Colombo-based demo accounts (manager.colombo, staff1.colombo) — their
-- screens (banners/quick-sale/coupons, branch inventory) don't depend on
-- order volume.
--
-- support@ goes to Negombo Beach instead: 021/025/026's demo order seed only
-- ever assigned orders to Negombo Beach and Kurunegala Hub (every other
-- branch has zero), and Customer Support's whole dashboard is an order
-- queue — parked on Colombo Main it would always render empty. Negombo has
-- a live mix of pending/confirmed/processing/shipped orders to actually work.
-- ADMIN_CREDENTIALS.md is corrected alongside this.
-- =============================================================================

UPDATE admins a
SET branch_id = b.branch_id, updated_at = NOW()
FROM branches b
WHERE b.code = 'CMB'
  AND a.email IN ('marketing@sribeesonline.lk', 'inventory@sribeesonline.lk')
  AND a.branch_id IS NULL;

UPDATE admins a
SET branch_id = b.branch_id, updated_at = NOW()
FROM branches b
WHERE b.code = 'NGB'
  AND a.email = 'support@sribeesonline.lk'
  AND a.branch_id IS NULL;
