-- =============================================================================
-- Migration 078: Return Rejected Milestone Timestamp
--
-- Records the exact moment an admin rejects a return claim so the order status
-- timeline can present the rejection step accurately.
-- =============================================================================

ALTER TABLE orders ADD COLUMN IF NOT EXISTS return_rejected_at TIMESTAMPTZ;
