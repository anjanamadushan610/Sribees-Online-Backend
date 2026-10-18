-- =============================================================================
-- Migration 077: Add raised_by_role column to order_escalations
-- =============================================================================

ALTER TABLE order_escalations ADD COLUMN IF NOT EXISTS raised_by_role VARCHAR(50);
