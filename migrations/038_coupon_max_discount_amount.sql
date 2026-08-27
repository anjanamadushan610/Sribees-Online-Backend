-- =============================================================================
-- Migration 038: Add max_discount_amount to coupons table
--
-- Adds optional max_discount_amount (LKR limit for percentage discounts).
-- =============================================================================

ALTER TABLE coupons ADD COLUMN IF NOT EXISTS max_discount_amount NUMERIC(10, 2) NULL;
