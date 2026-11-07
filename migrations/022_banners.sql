-- =============================================================================
-- 022_banners.sql — Home-screen promotional banners, curated per branch.
--
-- Replaces the three slides that were hardcoded in the Flutter app, so branch
-- managers can run their own promotions without an app release.
--
-- branch_id NULL = platform-wide (Super Admin), shown in every branch.
-- Idempotent: safe to re-run on every deploy.
-- =============================================================================

CREATE TABLE IF NOT EXISTS banners (
    banner_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title       VARCHAR(120) NOT NULL,
    subtitle    VARCHAR(200),
    image_url   VARCHAR(500),
    link_type   VARCHAR(20),
    target_id   UUID,
    branch_id   UUID REFERENCES branches(branch_id) ON DELETE CASCADE,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- The storefront's only read pattern: active banners for one branch (plus the
-- platform-wide ones), in display order.
CREATE INDEX IF NOT EXISTS idx_banners_branch_active_order
    ON banners (branch_id, is_active, sort_order);

-- -----------------------------------------------------------------------------
-- Seed banners REMOVED (2026-11-07, owner's decision).
--
-- This used to insert three placeholder slides ("Fresh Farm Produce",
-- "20% Off Greens", "Earn 10% Cash Back") with fixed ids and ON CONFLICT DO
-- NOTHING. Because every deploy replays every migration and nothing guarded
-- it, deleting them from the admin panel only lasted until the next deploy.
-- Banners are made in the admin panel only.
-- -----------------------------------------------------------------------------
